import asyncio
import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.models import Ledger, Task, User
from app.schemas import ProviderEvent
from app.service import provider_result
from tests.test_finance import assert_balance, generate, payment, signature
from tests.test_transitions import count_kind, task_view


@pytest.mark.parametrize(
    "path,body",
    [
        (
            "/webhooks/payments",
            {"payment_id": "x", "user_id": 1, "tokens": 100, "status": "completed"},
        ),
        (
            "/webhooks/providers/alpha",
            {
                "attempt_id": str(uuid.uuid4()),
                "external_id": "x",
                "status": "completed",
                "result_url": "https://example.com",
            },
        ),
    ],
)
async def test_invalid_hmac(client, application, path, body):
    raw, sig = signature(body, "wrong-secret")
    assert (await client.post(path, content=raw, headers={"X-Signature": sig})).status_code == 401
    assert (await client.post(path, content=raw)).status_code == 401
    await assert_balance(application, 0)


async def test_hmac_uses_raw_bytes(client, application, settings):
    body = {"payment_id": "x", "user_id": 1, "tokens": 100, "status": "completed"}
    raw, sig = signature(body, settings.payment_secret)
    assert (
        await client.post("/webhooks/payments", content=raw + b" ", headers={"X-Signature": sig})
    ).status_code == 401
    await assert_balance(application, 0)


@pytest.mark.parametrize("status", ["failed", "pending"])
async def test_unsuccessful_payment_is_not_credited(client, application, settings, status):
    raw, sig = signature(
        dict(payment_id="p1", user_id=1, tokens=100, status=status), settings.payment_secret
    )
    response = await client.post("/webhooks/payments", content=raw, headers={"X-Signature": sig})
    assert response.json() == {"applied": False}
    await assert_balance(application, 0)
    assert (await payment(client, settings)).json() == {"applied": True}


async def test_conflicting_payment_payload(client, application, settings):
    await payment(client, settings)
    assert (await payment(client, settings, tokens=200)).status_code == 409
    assert (await payment(client, settings, user_id=2)).status_code == 409
    await assert_balance(application, 100)
    assert await count_kind(application, "payment") == 1


async def test_concurrent_debits_cannot_overdraw(client, application, settings):
    await payment(client, settings, tokens=3)
    responses = await asyncio.gather(*(generate(client) for _ in range(10)))
    assert sum(r.status_code == 202 for r in responses) == 1
    assert sum(r.status_code == 402 for r in responses) == 9
    await assert_balance(application, 1)
    assert await count_kind(application, "debit") == 1


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE users SET balance=500 WHERE id=1",
        "INSERT INTO users(id,balance) VALUES (3,500)",
        "UPDATE ledger SET amount=500 WHERE user_id=1",
        "DELETE FROM ledger WHERE user_id=1",
        "INSERT INTO ledger(user_id,kind,amount,payment_id) VALUES(1,'payment',10,'p1')",
    ],
)
async def test_database_guards(client, application, settings, sql):
    await payment(client, settings)
    with pytest.raises(DBAPIError):
        async with application.state.sessions() as session, session.begin():
            await session.execute(text(sql))
    await assert_balance(application, 100)


async def test_transaction_rollback_on_webhook_crash(client, application, settings):
    await payment(client, settings)
    task = await task_view(client, ref_images=["https://example.com/ref"] * 5)
    attempt_id = uuid.UUID(task["attempts"][0]["attempt_id"])
    event = ProviderEvent(
        attempt_id=attempt_id, external_id=str(attempt_id), status="failed", error_type="technical"
    )
    with pytest.raises(RuntimeError, match="crash before commit"):
        async with application.state.sessions() as session, session.begin():
            await provider_result(session, "beta", event, settings)
            raise RuntimeError("crash before commit")
    assert (await client.get("/tasks/" + task["task_id"])).json()["status"] == "active"
    assert await count_kind(application, "refund") == 0
    await assert_balance(application, 98)
    async with application.state.sessions() as session, session.begin():
        assert await provider_result(session, "beta", event, settings)
    await assert_balance(application, 100)
    assert await count_kind(application, "refund") == 1


async def test_unknown_user_and_model(client, application, settings):
    assert (await generate(client, user_id=999)).status_code == 404
    assert (await generate(client, model_slug="missing")).status_code == 404
    assert (await payment(client, settings, user_id=999)).status_code == 404
    assert (await client.get("/tasks/" + str(uuid.uuid4()))).status_code == 404
    assert (await client.get("/users/999/balance")).status_code == 404
    await assert_balance(application, 0)


async def test_balance_api_returns_ledger(client, settings):
    await payment(client, settings)
    task = await task_view(client)
    balance = (await client.get("/users/1/balance")).json()
    assert balance["balance"] == 98
    assert [op["type"] for op in balance["operations"]] == ["debit", "payment"]
    assert balance["operations"][0]["task_id"] == task["task_id"]
    assert balance["operations"][1]["payment_id"] == "p1"
    assert all(op["created_at"] for op in balance["operations"])


async def test_all_users_balance_sum_invariant(client, application, settings):
    await asyncio.gather(
        payment(client, settings), payment(client, settings, user_id=2, payment_id="p2")
    )
    await task_view(client)
    async with application.state.sessions() as session:
        rows = (
            await session.execute(
                select(User.id, User.balance, func.sum(Ledger.amount))
                .join(Ledger, Ledger.user_id == User.id)
                .group_by(User.id)
            )
        ).all()
        assert len(rows) == 2
        assert all(balance == total for _, balance, total in rows)
        assert await session.scalar(select(func.count()).select_from(Task)) == 1
