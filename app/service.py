"""State changes: user -> task -> attempt -> outbox; never do network I/O here."""

import logging
import uuid
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import Attempt, Ledger, Leg, Model, Outbox, Task, User
from app.schemas import GenerateRequest, PaymentEvent, ProviderEvent

log = logging.getLogger(__name__)
OPEN_ATTEMPTS = ("queued", "submitted")


async def db_now(session: AsyncSession) -> datetime:
    return await session.scalar(select(func.clock_timestamp()))


async def lock_user(session: AsyncSession, user_id: int) -> User:
    user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
    if user is None:
        raise HTTPException(404, "User not found")
    return user


async def create_attempt(
    session: AsyncSession, task: Task, leg: Leg, settings: Settings
) -> Attempt:
    now = await db_now(session)
    attempt = Attempt(
        id=uuid.uuid4(),
        task_id=task.id,
        leg_position=leg.position,
        provider=leg.provider,
        started_at=now,
        deadline=now + timedelta(seconds=settings.provider_timeout),
    )
    task.current_leg = leg.position
    session.add(attempt)
    await session.flush()
    session.add(Outbox(attempt_id=attempt.id))
    return attempt


async def generate(session: AsyncSession, data: GenerateRequest, settings: Settings) -> Task:
    user = await lock_user(session, data.user_id)
    model = await session.get(Model, data.model_slug)
    if model is None:
        raise HTTPException(404, "Model not found")
    leg = await session.scalar(
        select(Leg)
        .where(Leg.model_slug == model.slug, Leg.max_ref_images >= len(data.ref_images))
        .order_by(Leg.position)
        .limit(1)
    )
    if leg is None:
        raise HTTPException(400, "No eligible provider legs")
    active = await session.scalar(
        select(func.count())
        .select_from(Task)
        .where(Task.user_id == user.id, Task.status == "active")
    )
    if active >= 3:
        raise HTTPException(429, "Three active tasks already exist")
    if user.balance < model.cost:
        raise HTTPException(402, "Insufficient tokens")
    task = Task(
        id=uuid.uuid4(),
        user_id=user.id,
        model_slug=model.slug,
        cost=model.cost,
        prompt=data.prompt,
        ref_images=[str(url) for url in data.ref_images],
        current_leg=leg.position,
    )
    session.add(task)
    await session.flush()
    session.add(Ledger(user_id=user.id, kind="debit", amount=-task.cost, task_id=task.id))
    await create_attempt(session, task, leg, settings)
    await session.flush()
    log.info("task_created task_id=%s user_id=%s", task.id, user.id)
    return task


async def credit_payment(session: AsyncSession, data: PaymentEvent) -> bool:
    if data.status != "completed":
        return False
    await lock_user(session, data.user_id)
    inserted = await session.scalar(
        insert(Ledger)
        .values(
            user_id=data.user_id, kind="payment", amount=data.tokens, payment_id=data.payment_id
        )
        .on_conflict_do_nothing(index_elements=[Ledger.payment_id])
        .returning(Ledger.id)
    )
    if inserted is None:
        existing = await session.scalar(select(Ledger).where(Ledger.payment_id == data.payment_id))
        if existing.user_id != data.user_id or existing.amount != data.tokens:
            raise HTTPException(409, "payment_id already used with a different payload")
    return inserted is not None


async def locked_attempt(
    session: AsyncSession, attempt_id: uuid.UUID
) -> tuple[Task, Attempt] | None:
    identity = (
        await session.execute(
            select(Attempt.task_id, Task.user_id)
            .join(Task, Task.id == Attempt.task_id)
            .where(Attempt.id == attempt_id)
        )
    ).first()
    if identity is None:
        return None
    await lock_user(session, identity.user_id)
    task = await session.scalar(select(Task).where(Task.id == identity.task_id).with_for_update())
    attempt = await session.scalar(
        select(Attempt).where(Attempt.id == attempt_id).with_for_update()
    )
    return task, attempt


async def finish_locked(
    session: AsyncSession,
    task: Task,
    attempt: Attempt,
    settings: Settings,
    *,
    error: str | None = None,
    result_url: str | None = None,
) -> None:
    """Caller holds ordered locks. Both watchdog and all provider outcomes enter here."""
    now = await db_now(session)
    attempt.finished_at = now
    attempt.error_type = error
    attempt.status = "failed" if error else "completed"
    await session.execute(update(Outbox).where(Outbox.attempt_id == attempt.id).values(done=True))
    if error is None:
        task.status, task.result_url, task.finished_at = "completed", result_url, now
    else:
        task.moderation_seen = task.moderation_seen or error == "moderation"
        next_leg = None
        if error != "payload":
            query = select(Leg).where(
                Leg.model_slug == task.model_slug,
                Leg.position > attempt.leg_position,
                Leg.max_ref_images >= len(task.ref_images),
            )
            if task.moderation_seen:
                query = query.where(Leg.skip_on_moderation.is_(False))
            next_leg = await session.scalar(query.order_by(Leg.position).limit(1))
        if next_leg is not None:
            await create_attempt(session, task, next_leg, settings)
        else:
            task.status, task.finished_at = "failed", now
            # UNIQUE(task_id, kind) is the final DB guard in addition to the row locks.
            await session.execute(
                insert(Ledger)
                .values(user_id=task.user_id, task_id=task.id, kind="refund", amount=task.cost)
                .on_conflict_do_nothing(index_elements=[Ledger.task_id, Ledger.kind])
            )
    await session.flush()
    log.info(
        "attempt_finished task_id=%s attempt_id=%s error=%s task_status=%s",
        task.id,
        attempt.id,
        error,
        task.status,
    )


def is_current(task: Task, attempt: Attempt) -> bool:
    return (
        task.status == "active"
        and task.current_leg == attempt.leg_position
        and attempt.status in OPEN_ATTEMPTS
    )


async def provider_result(
    session: AsyncSession,
    name: str,
    event: ProviderEvent,
    settings: Settings,
) -> bool:
    pair = await locked_attempt(session, event.attempt_id)
    if pair is None:
        raise HTTPException(404, "Attempt not found")
    task, attempt = pair
    if attempt.provider != name:
        raise HTTPException(400, "Provider does not own attempt")
    if attempt.external_id is not None and attempt.external_id != event.external_id:
        raise HTTPException(409, "external_id does not match attempt")
    if not is_current(task, attempt):
        return False
    # Enforce the deadline even if watchdog has not yet polled this row.
    if attempt.deadline <= await db_now(session):
        await finish_locked(session, task, attempt, settings, error="timeout")
        return False
    attempt.external_id = event.external_id
    await finish_locked(
        session,
        task,
        attempt,
        settings,
        error=event.error_type,
        result_url=str(event.result_url) if event.result_url else None,
    )
    return True


async def expire_attempt(session: AsyncSession, attempt_id: uuid.UUID, settings: Settings) -> bool:
    pair = await locked_attempt(session, attempt_id)
    if pair is None:
        return False
    task, attempt = pair
    if not is_current(task, attempt) or attempt.deadline > await db_now(session):
        return False
    await finish_locked(session, task, attempt, settings, error="timeout")
    return True
