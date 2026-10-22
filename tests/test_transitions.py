import asyncio
import uuid

import httpx
import pytest
from sqlalchemy import func, select, text, update

from app.models import Attempt, Ledger, Outbox
from app.worker import claim, dispatch_once, settle, watchdog_once
from tests.test_finance import assert_balance, generate, payment, signature


async def task_view(client, **kwargs):
    response = await generate(client, **kwargs)
    assert response.status_code == 202, response.text
    return (await client.get("/tasks/" + response.json()["task_id"])).json()


async def hook(client, settings, attempt, *, error=None, url="https://example.com/result"):
    payload = {
        "attempt_id": attempt["attempt_id"],
        "external_id": attempt["attempt_id"],
        "status": "failed" if error else "completed",
    }
    payload["error_type" if error else "result_url"] = error if error else url
    raw, sig = signature(payload, settings.secret(attempt["provider"]))
    return await client.post(
        "/webhooks/providers/" + attempt["provider"], content=raw, headers={"X-Signature": sig}
    )


async def force_deadline(application, attempt_id):
    async with application.state.sessions() as session, session.begin():
        await session.execute(
            update(Attempt)
            .where(Attempt.id == uuid.UUID(attempt_id))
            .values(deadline=text("clock_timestamp() - interval '1 second'"))
        )


async def count_kind(application, kind):
    async with application.state.sessions() as session:
        return await session.scalar(
            select(func.count()).select_from(Ledger).where(Ledger.kind == kind)
        )


async def test_concurrent_refunds_last_leg(client, application, settings):
    await payment(client, settings)
    task = await task_view(client, ref_images=["https://example.com/ref"] * 5)
    attempt = task["attempts"][0]
    assert attempt["provider"] == "beta"
    responses = await asyncio.gather(
        *(hook(client, settings, attempt, error="technical") for _ in range(10))
    )
    assert all(r.status_code == 200 for r in responses)
    final = (await client.get("/tasks/" + task["task_id"])).json()
    assert final["status"] == "failed"
    assert len(final["attempts"]) == 1
    assert await count_kind(application, "refund") == 1
    await assert_balance(application, 100)


async def test_provider_fallback(client, application, settings):
    await payment(client, settings)
    task = await task_view(client)
    assert (await hook(client, settings, task["attempts"][0], error="technical")).json()["applied"]
    task = (await client.get("/tasks/" + task["task_id"])).json()
    assert [a["provider"] for a in task["attempts"]] == ["alpha", "beta"]
    assert (await hook(client, settings, task["attempts"][1])).json()["applied"]
    final = (await client.get("/tasks/" + task["task_id"])).json()
    assert final["status"] == "completed"
    assert all(a["finished_at"] for a in final["attempts"])
    assert await count_kind(application, "debit") == 1
    assert await count_kind(application, "refund") == 0
    await assert_balance(application, 98)


async def test_late_completed_cannot_overwrite_next_result(client, application, settings):
    await payment(client, settings)
    task = await task_view(client)
    old = task["attempts"][0]
    await force_deadline(application, old["attempt_id"])
    assert await watchdog_once(application.state.sessions, settings) == 1
    task = (await client.get("/tasks/" + task["task_id"])).json()
    await hook(client, settings, task["attempts"][1], url="https://example.com/new")
    assert (await hook(client, settings, old, url="https://example.com/old")).json() == {
        "applied": False
    }
    final = (await client.get("/tasks/" + task["task_id"])).json()
    assert final["status"] == "completed" and final["result_url"] == "https://example.com/new"
    assert final["attempts"][0]["error_type"] == "timeout"
    assert await count_kind(application, "refund") == 0
    await assert_balance(application, 98)


async def test_late_completed_after_refund(client, application, settings):
    await payment(client, settings)
    task = await task_view(client, ref_images=["https://example.com/ref"] * 5)
    attempt = task["attempts"][0]
    await force_deadline(application, attempt["attempt_id"])
    results = await asyncio.gather(
        *(watchdog_once(application.state.sessions, settings) for _ in range(4))
    )
    assert sum(results) == 1
    assert (await hook(client, settings, attempt)).json() == {"applied": False}
    assert (await client.get("/tasks/" + task["task_id"])).json()["status"] == "failed"
    assert await count_kind(application, "refund") == 1
    await assert_balance(application, 100)


@pytest.mark.parametrize(
    "error,model,expected_providers",
    [
        ("moderation", "video-long", ["beta"]),
        ("moderation", "image-fast", ["alpha", "beta"]),
        ("payload", "image-fast", ["alpha"]),
        ("technical", "video-long", ["beta", "alpha"]),
    ],
)
async def test_fallback_rules(client, application, settings, error, model, expected_providers):
    await payment(client, settings)
    task = await task_view(client, model_slug=model)
    await hook(client, settings, task["attempts"][0], error=error)
    final = (await client.get("/tasks/" + task["task_id"])).json()
    assert [a["provider"] for a in final["attempts"]] == expected_providers
    assert final["status"] == ("failed" if len(expected_providers) == 1 else "active")
    await assert_balance(
        application, 100 if len(expected_providers) == 1 else (75 if model == "video-long" else 98)
    )


@pytest.mark.parametrize(
    "failure,error",
    [(503, "http_5xx"), ("timeout", "send_timeout"), (400, "payload"), ("bad_json", "technical")],
)
async def test_dispatch_failures(client, application, settings, failure, error):
    await payment(client, settings)
    task = await task_view(client)

    async def handler(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("accepted but response lost", request=request)
        if failure == "bad_json":
            return httpx.Response(202, json={})
        return httpx.Response(failure)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        assert await dispatch_once(application.state.sessions, settings, transport)
    final = (await client.get("/tasks/" + task["task_id"])).json()
    assert final["attempts"][0]["error_type"] == error
    assert len(final["attempts"]) == (1 if error == "payload" else 2)
    await assert_balance(application, 100 if error == "payload" else 98)


async def test_no_webhook_watchdog(client, application, settings):
    await payment(client, settings)
    task = await task_view(client)
    attempt = task["attempts"][0]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(202, json={"external_id": attempt["attempt_id"]})
        )
    ) as transport:
        await dispatch_once(application.state.sessions, settings, transport)
    assert await watchdog_once(application.state.sessions, settings) == 0
    await force_deadline(application, attempt["attempt_id"])
    assert await watchdog_once(application.state.sessions, settings) == 1
    final = (await client.get("/tasks/" + task["task_id"])).json()
    assert final["attempts"][0]["error_type"] == "timeout" and len(final["attempts"]) == 2


async def test_crash_lease_recovery_and_fencing(client, application, settings):
    await payment(client, settings)
    task = await task_view(client)
    stale_job = await claim(application.state.sessions, settings)
    assert await claim(application.state.sessions, settings) is None
    async with application.state.sessions() as session, session.begin():
        await session.execute(
            update(Outbox).values(lease_until=text("clock_timestamp() - interval '1 second'"))
        )
    recovered = await claim(application.state.sessions, settings)
    assert recovered["attempt_id"] == stale_job["attempt_id"]
    assert recovered["token"] != stale_job["token"]
    await settle(application.state.sessions, settings, stale_job, error="technical")
    assert len((await client.get("/tasks/" + task["task_id"])).json()["attempts"]) == 1
    await settle(
        application.state.sessions, settings, recovered, external_id=str(recovered["attempt_id"])
    )
    async with application.state.sessions() as session:
        outbox = await session.get(Outbox, recovered["attempt_id"])
        assert outbox.done and outbox.deliveries == 2
    await assert_balance(application, 98)


async def test_parallel_dispatch_claim_only_once(client, application, settings):
    await payment(client, settings)
    await task_view(client)
    jobs = await asyncio.gather(*(claim(application.state.sessions, settings) for _ in range(4)))
    assert sum(job is not None for job in jobs) == 1


@pytest.mark.parametrize("expired", [False, True])
async def test_completed_watchdog_race(client, application, settings, expired):
    await payment(client, settings)
    task = await task_view(client, ref_images=["https://example.com/ref"] * 5)
    attempt = task["attempts"][0]
    if expired:
        await force_deadline(application, attempt["attempt_id"])
    await asyncio.gather(
        hook(client, settings, attempt),
        *(watchdog_once(application.state.sessions, settings) for _ in range(4)),
    )
    final = (await client.get("/tasks/" + task["task_id"])).json()
    assert final["status"] == ("failed" if expired else "completed")
    assert len(final["attempts"]) == 1
    assert await count_kind(application, "refund") == (1 if expired else 0)
    await assert_balance(application, 100 if expired else 98)


async def test_webhook_before_dispatch_ack(client, application, settings):
    await payment(client, settings)
    task = await task_view(client)
    job = await claim(application.state.sessions, settings)
    await hook(client, settings, task["attempts"][0])
    await settle(application.state.sessions, settings, job, error="send_timeout")
    assert (await client.get("/tasks/" + task["task_id"])).json()["status"] == "completed"
    await assert_balance(application, 98)
