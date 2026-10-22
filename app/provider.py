"""Both fake providers use this app, selected by PROVIDER_NAME.

Acceptance and webhook delivery are durable PostgreSQL operations, including
deduplication after process restart. No FastAPI BackgroundTasks store work.
"""

import asyncio
import contextlib
import hmac
import json
import logging
import random
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_, select, text, update
from sqlalchemy.dialects.postgresql import insert

from app.config import Settings, configure_logging, get_settings
from app.db import SessionFactory, connect
from app.models import ProviderJob, ProviderSetting
from app.security import sign
from app.service import db_now

log = logging.getLogger(__name__)
Scenario = Literal[
    "random",
    "success",
    "technical",
    "moderation",
    "payload",
    "http503",
    "timeout",
    "duplicate",
    "parallel",
    "no_webhook",
    "late",
]


class ScenarioRequest(BaseModel):
    scenario: Scenario
    delay: float | None = Field(default=None, ge=0, le=3600)


class ProviderRequest(BaseModel):
    attempt_id: uuid.UUID
    prompt: str
    model_slug: str
    ref_images: list[str]


async def deliver_once(
    sessions: SessionFactory, settings: Settings, client: httpx.AsyncClient
) -> bool:
    async with sessions() as session, session.begin():
        now = await db_now(session)
        job = await session.scalar(
            select(ProviderJob)
            .where(
                ProviderJob.provider == settings.provider_name,
                ProviderJob.delivered.is_(False),
                ProviderJob.scenario != "no_webhook",
                ProviderJob.due_at <= now,
                or_(ProviderJob.lease_until.is_(None), ProviderJob.lease_until <= now),
            )
            .order_by(ProviderJob.due_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if job is None:
            return False
        token = uuid.uuid4()
        job.lease_token, job.lease_until = token, now + timedelta(seconds=settings.dispatch_lease)
        job.deliveries += 1
        attempt_id, scenario = job.attempt_id, job.scenario
    result = {
        "attempt_id": str(attempt_id),
        "external_id": str(attempt_id),
        "status": "failed" if scenario in {"technical", "moderation", "payload"} else "completed",
    }
    if result["status"] == "failed":
        result["error_type"] = scenario
    else:
        result["result_url"] = f"https://results.example/{settings.provider_name}/{attempt_id}"
    raw = json.dumps(result, separators=(",", ":")).encode()

    async def send():
        response = await client.post(
            f"{settings.callback_base_url}/webhooks/providers/{settings.provider_name}",
            content=raw,
            headers={
                "X-Signature": sign(raw, settings.secret(settings.provider_name)),
                "Content-Type": "application/json",
            },
            timeout=settings.http_timeout,
        )
        response.raise_for_status()

    try:
        if scenario == "parallel":
            await asyncio.gather(send(), send())
        else:
            await send()
            if scenario == "duplicate":
                await send()
    except httpx.HTTPError:
        log.warning(
            "callback_delivery_failed provider=%s attempt_id=%s",
            settings.provider_name,
            attempt_id,
            exc_info=True,
        )
        # Retry after lease expiry, including after a provider process crash.
        return True
    async with sessions() as session, session.begin():
        await session.execute(
            update(ProviderJob)
            .where(
                ProviderJob.provider == settings.provider_name,
                ProviderJob.attempt_id == attempt_id,
                ProviderJob.lease_token == token,
            )
            .values(delivered=True)
        )
    return True


async def delivery_loop(sessions: SessionFactory, settings: Settings) -> None:
    async with httpx.AsyncClient() as client:
        while True:
            try:
                if await deliver_once(sessions, settings, client):
                    continue
            except Exception:
                log.exception("provider_delivery_iteration_failed")
            await asyncio.sleep(settings.worker_poll)


def create_provider(settings: Settings | None = None, *, background: bool = True) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine, sessions = connect(settings)
        app.state.sessions = sessions
        worker = asyncio.create_task(delivery_loop(sessions, settings)) if background else None
        try:
            yield
        finally:
            if worker:
                worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker
            await engine.dispose()

    app = FastAPI(title=f"Fake {settings.provider_name}", lifespan=lifespan)

    async def authenticate(x_api_key: str | None = Header(default=None)):
        if x_api_key is None or not hmac.compare_digest(x_api_key, settings.provider_api_key):
            raise HTTPException(401, "Invalid X-API-Key")

    @app.get("/health")
    async def health(request: Request):
        async with request.app.state.sessions() as session:
            await session.execute(text("SELECT version_num FROM alembic_version"))
        return {"status": "ok", "provider": settings.provider_name}

    @app.post("/admin/scenario", dependencies=[Depends(authenticate)])
    async def configure(data: ScenarioRequest, request: Request):
        async with request.app.state.sessions() as session, session.begin():
            await session.execute(
                insert(ProviderSetting)
                .values(name=settings.provider_name, **data.model_dump())
                .on_conflict_do_update(
                    index_elements=[ProviderSetting.name], set_=data.model_dump()
                )
            )
        return data

    @app.post("/tasks", status_code=202, dependencies=[Depends(authenticate)])
    async def accept(
        data: ProviderRequest, request: Request, idempotency_key: str | None = Header(default=None)
    ):
        if idempotency_key != str(data.attempt_id):
            raise HTTPException(400, "Idempotency-Key must equal attempt_id")
        payload = data.model_dump(mode="json")
        async with request.app.state.sessions() as session, session.begin():
            existing = await session.get(ProviderJob, (settings.provider_name, data.attempt_id))
            if existing:
                if existing.payload != payload:
                    raise HTTPException(409, "Idempotency key reused with different payload")
                return {"external_id": str(data.attempt_id)}
            config = await session.get(ProviderSetting, settings.provider_name)
            scenario = config.scenario if config else "random"
            if scenario == "http503":
                raise HTTPException(503, "Simulated unavailable provider")
            if scenario == "random":
                scenario = random.choice(["success", "technical", "moderation", "payload"])
            delay = (
                config.delay
                if config and config.delay is not None
                else random.uniform(settings.provider_delay_min, settings.provider_delay_max)
            )
            if scenario == "late":
                delay = max(delay, settings.provider_timeout + 2)
            now = await db_now(session)
            await session.execute(
                insert(ProviderJob)
                .values(
                    provider=settings.provider_name,
                    attempt_id=data.attempt_id,
                    payload=payload,
                    scenario=scenario,
                    due_at=now + timedelta(seconds=delay),
                )
                .on_conflict_do_nothing(
                    index_elements=[ProviderJob.provider, ProviderJob.attempt_id]
                )
            )
            # Another process may have inserted the same key while we read settings.
            stored = await session.get(ProviderJob, (settings.provider_name, data.attempt_id))
            if stored.payload != payload:
                raise HTTPException(409, "Idempotency key reused with different payload")
        if scenario == "timeout":
            # The job is already committed: deliberately lose the acceptance response.
            await asyncio.sleep(settings.http_timeout + 2)
        return {"external_id": str(data.attempt_id)}

    return app


app = create_provider()
