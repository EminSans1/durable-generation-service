"""Durable outbox dispatcher and watchdog. Multiple processes may run this module."""

import asyncio
import logging
import signal
import uuid
from datetime import timedelta

import httpx
from sqlalchemy import or_, select

from app.config import Settings, configure_logging, get_settings
from app.db import SessionFactory, connect
from app.models import Attempt, Outbox, Task
from app.service import (
    OPEN_ATTEMPTS,
    db_now,
    expire_attempt,
    finish_locked,
    is_current,
    locked_attempt,
)

log = logging.getLogger(__name__)


async def claim(sessions: SessionFactory, settings: Settings) -> dict | None:
    async with sessions() as session, session.begin():
        now = await db_now(session)
        row = await session.scalar(
            select(Outbox)
            .where(
                Outbox.done.is_(False), or_(Outbox.lease_until.is_(None), Outbox.lease_until <= now)
            )
            .order_by(Outbox.attempt_id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return None
        token = uuid.uuid4()
        row.lease_token, row.lease_until = token, now + timedelta(seconds=settings.dispatch_lease)
        row.deliveries += 1
        attempt = await session.get(Attempt, row.attempt_id)
        task = await session.get(Task, attempt.task_id)
        return {
            "attempt_id": attempt.id,
            "token": token,
            "provider": attempt.provider,
            "payload": {
                "attempt_id": str(attempt.id),
                "prompt": task.prompt,
                "model_slug": task.model_slug,
                "ref_images": task.ref_images,
            },
        }


async def settle(
    sessions: SessionFactory,
    settings: Settings,
    job: dict,
    *,
    error: str | None = None,
    external_id: str | None = None,
) -> None:
    async with sessions() as session, session.begin():
        pair = await locked_attempt(session, job["attempt_id"])
        if pair is None:
            return
        task, attempt = pair
        outbox = await session.scalar(
            select(Outbox).where(Outbox.attempt_id == attempt.id).with_for_update()
        )
        # Fencing: a previous owner must not settle a re-claimed delivery.
        if outbox.done or outbox.lease_token != job["token"]:
            return
        if not is_current(task, attempt):
            outbox.done = True
            return
        if attempt.deadline <= await db_now(session):
            error = "timeout"
        if error:
            await finish_locked(session, task, attempt, settings, error=error)
        else:
            attempt.status, attempt.external_id = "submitted", external_id
            outbox.done = True


async def dispatch_once(
    sessions: SessionFactory, settings: Settings, client: httpx.AsyncClient
) -> bool:
    job = await claim(sessions, settings)
    if job is None:
        return False
    # A crash after claim, after HTTP, or before settle leaves a recoverable lease.
    # Provider persists Idempotency-Key before responding, so retries reuse one job.
    async with sessions() as session:
        attempt = await session.get(Attempt, job["attempt_id"])
        live = attempt.status in OPEN_ATTEMPTS and attempt.deadline > await db_now(session)
    if not live:
        await settle(sessions, settings, job, error="timeout")
        return True
    error, external_id = None, None
    try:
        response = await client.post(
            settings.provider_url(job["provider"]) + "/tasks",
            json=job["payload"],
            headers={
                "Idempotency-Key": str(job["attempt_id"]),
                "X-API-Key": settings.provider_api_key,
            },
            timeout=settings.http_timeout,
        )
        if response.status_code >= 500:
            error = "http_5xx"
        elif response.status_code != 202:
            error = "payload" if 400 <= response.status_code < 500 else "technical"
        else:
            external_id = response.json()["external_id"]
            if not isinstance(external_id, str) or not 1 <= len(external_id) <= 128:
                raise ValueError("Invalid external_id")
    except httpx.TimeoutException:
        error = "send_timeout"
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        log.warning("provider_transport_error attempt_id=%s", job["attempt_id"], exc_info=True)
        error = "technical"
    await settle(sessions, settings, job, error=error, external_id=external_id)
    return True


async def watchdog_once(sessions: SessionFactory, settings: Settings) -> int:
    async with sessions() as session:
        now = await db_now(session)
        ids = list(
            (
                await session.scalars(
                    select(Attempt.id)
                    .where(Attempt.status.in_(OPEN_ATTEMPTS), Attempt.deadline <= now)
                    .order_by(Attempt.deadline)
                    .limit(100)
                )
            ).all()
        )
    count = 0
    for attempt_id in ids:
        async with sessions() as session, session.begin():
            count += await expire_attempt(session, attempt_id, settings)
    return count


async def dispatch_loop(sessions: SessionFactory, settings: Settings, stop: asyncio.Event) -> None:
    async with httpx.AsyncClient() as client:
        while not stop.is_set():
            try:
                worked = await dispatch_once(sessions, settings, client)
                if worked:
                    continue
            except Exception:
                # A transient DB failure leaves leases/state durable for the next poll.
                log.exception("dispatch_iteration_failed")
            await asyncio.sleep(settings.worker_poll)


async def watchdog_loop(sessions: SessionFactory, settings: Settings, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await watchdog_once(sessions, settings)
        except Exception:
            log.exception("watchdog_iteration_failed")
        await asyncio.sleep(settings.worker_poll)


async def run() -> None:
    settings = get_settings()
    configure_logging(settings)
    engine, sessions = connect(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows uses the default KeyboardInterrupt path.
            pass
    try:
        await asyncio.gather(
            dispatch_loop(sessions, settings, stop), watchdog_loop(sessions, settings, stop)
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
