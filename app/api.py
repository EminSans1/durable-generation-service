import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy import select, text

from app import service
from app.config import Settings, configure_logging, get_settings
from app.db import connect
from app.models import Attempt, Ledger, Task, User
from app.schemas import GenerateRequest, PaymentEvent, ProviderEvent
from app.security import verify


async def signed_event(request: Request, schema, secret: str):
    raw = await request.body()
    verify(raw, request.headers.get("X-Signature"), secret)
    try:
        return schema.model_validate_json(raw)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine, sessions = connect(settings)
        app.state.sessions = sessions
        try:
            yield
        finally:
            await engine.dispose()

    app = FastAPI(title="Durable Generation Service", version="1.0.0", lifespan=lifespan)

    @app.get("/health")
    async def health(request: Request):
        async with request.app.state.sessions() as session:
            await session.execute(text("SELECT 1"))
            await session.execute(text("SELECT version_num FROM alembic_version"))
        return {"status": "ok", "pid": os.getpid()}

    @app.post("/generate", status_code=202)
    async def generate(data: GenerateRequest, request: Request):
        async with request.app.state.sessions() as session, session.begin():
            task = await service.generate(session, data, settings)
            task_id = task.id
        return {"task_id": task_id}

    @app.post("/webhooks/payments")
    async def payment(request: Request):
        data = await signed_event(request, PaymentEvent, settings.payment_secret)
        async with request.app.state.sessions() as session, session.begin():
            applied = await service.credit_payment(session, data)
        return {"applied": applied}

    @app.post("/webhooks/providers/{name}")
    async def provider(name: str, request: Request):
        if name not in {"alpha", "beta"}:
            raise HTTPException(404, "Provider not found")
        data = await signed_event(request, ProviderEvent, settings.secret(name))
        async with request.app.state.sessions() as session, session.begin():
            applied = await service.provider_result(session, name, data, settings)
        return {"applied": applied}

    @app.get("/tasks/{task_id}")
    async def get_task(task_id: uuid.UUID, request: Request):
        async with request.app.state.sessions() as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            task = await session.get(Task, task_id)
            if task is None:
                raise HTTPException(404, "Task not found")
            attempts = list(
                (
                    await session.scalars(
                        select(Attempt)
                        .where(Attempt.task_id == task_id)
                        .order_by(Attempt.leg_position)
                    )
                ).all()
            )
            return {
                "task_id": task.id,
                "status": task.status,
                "model_slug": task.model_slug,
                "current_provider": attempts[-1].provider,
                "current_leg": task.current_leg,
                "result_url": task.result_url,
                "created_at": task.created_at,
                "finished_at": task.finished_at,
                "attempts": [
                    {
                        "attempt_id": a.id,
                        "provider": a.provider,
                        "leg": a.leg_position,
                        "status": a.status,
                        "external_id": a.external_id,
                        "error_type": a.error_type,
                        "started_at": a.started_at,
                        "finished_at": a.finished_at,
                        "deadline": a.deadline,
                    }
                    for a in attempts
                ],
            }

    @app.get("/users/{user_id}/balance")
    async def get_balance(user_id: int, request: Request):
        async with request.app.state.sessions() as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            user = await session.get(User, user_id)
            if user is None:
                raise HTTPException(404, "User not found")
            entries = (
                await session.scalars(
                    select(Ledger)
                    .where(Ledger.user_id == user_id)
                    .order_by(Ledger.id.desc())
                    .limit(50)
                )
            ).all()
            return {
                "user_id": user.id,
                "balance": user.balance,
                "operations": [
                    {
                        "id": entry.id,
                        "type": entry.kind,
                        "amount": entry.amount,
                        "task_id": entry.task_id,
                        "payment_id": entry.payment_id,
                        "created_at": entry.created_at,
                    }
                    for entry in entries
                ],
            }

    return app


app = create_app()
