import asyncio
import hashlib
import hmac
import json

from sqlalchemy import func, select

from app.models import Ledger, Task, User


def signature(body, secret):
    raw = json.dumps(body, separators=(",", ":")).encode()
    return raw, hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


async def payment(client, settings, tokens=100, payment_id="p1", user_id=1):
    raw, sig = signature(
        dict(payment_id=payment_id, user_id=user_id, tokens=tokens, status="completed"),
        settings.payment_secret,
    )
    return await client.post("/webhooks/payments", content=raw, headers={"X-Signature": sig})


async def generate(client, **kwargs):
    return await client.post(
        "/generate",
        json={
            "user_id": 1,
            "model_slug": "image-fast",
            "prompt": "landscape",
            "ref_images": [],
            **kwargs,
        },
    )


async def assert_balance(application, expected):
    async with application.state.sessions() as session:
        balance = await session.scalar(select(User.balance).where(User.id == 1))
        total = await session.scalar(
            select(func.coalesce(func.sum(Ledger.amount), 0)).where(Ledger.user_id == 1)
        )
        assert balance == total == expected


async def test_concurrent_payments(client, application, settings):
    responses = await asyncio.gather(*(payment(client, settings) for _ in range(20)))
    assert all(r.status_code == 200 for r in responses)
    await assert_balance(application, 100)
    async with application.state.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Ledger)) == 1


async def test_concurrent_debits_and_task_limit(client, application, settings):
    await payment(client, settings, tokens=100)
    responses = await asyncio.gather(*(generate(client) for _ in range(12)))
    assert sum(r.status_code == 202 for r in responses) == 3
    assert sum(r.status_code == 429 for r in responses) == 9
    await assert_balance(application, 94)
    async with application.state.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Task)) == 3


async def test_insufficient_balance(client, application):
    assert (await generate(client)).status_code == 402
    await assert_balance(application, 0)


async def test_no_eligible_legs_before_debit(client, application, settings):
    await payment(client, settings)
    assert (await generate(client, ref_images=["https://example.com/a"] * 11)).status_code == 400
    await assert_balance(application, 100)
