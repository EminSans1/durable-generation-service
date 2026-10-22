import asyncio
import uuid

import httpx
import pytest
from sqlalchemy import func, select, text, update

from app.models import Outbox, ProviderJob
from app.provider import create_provider, deliver_once
from app.worker import claim, dispatch_once
from tests.test_finance import assert_balance, payment
from tests.test_transitions import task_view


async def configure(providers, settings, name, scenario):
    clients, _ = providers
    response = await clients[name].post(
        "/admin/scenario",
        json={"scenario": scenario, "delay": 0},
        headers={"X-API-Key": settings.provider_api_key},
    )
    assert response.status_code == 200


async def send_to_provider(application, settings, providers):
    clients, _ = providers

    async def handler(request):
        return await clients[request.url.host].post(
            "/tasks", content=request.content, headers=request.headers
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await dispatch_once(application.state.sessions, settings, client)


@pytest.mark.parametrize(
    "scenario",
    [
        "success",
        "technical",
        "moderation",
        "payload",
        "duplicate",
        "parallel",
        "no_webhook",
        "late",
        "http503",
    ],
)
async def test_provider_scenarios(client, application, settings, providers, scenario):
    await payment(client, settings)
    await configure(providers, settings, "alpha", scenario)
    task = await task_view(client)
    await send_to_provider(application, settings, providers)
    _, configs = providers
    delivered = await deliver_once(application.state.sessions, configs["alpha"], client)
    final = (await client.get("/tasks/" + task["task_id"])).json()
    if scenario in {"success", "duplicate", "parallel"}:
        assert delivered and final["status"] == "completed"
    elif scenario in {"technical", "moderation", "http503"}:
        assert len(final["attempts"]) == 2 and final["status"] == "active"
    elif scenario == "payload":
        assert final["status"] == "failed"
    else:
        assert not delivered and final["status"] == "active"
    await assert_balance(application, 100 if scenario == "payload" else 98)


async def test_full_http_fallback(client, application, settings, providers):
    await payment(client, settings)
    await configure(providers, settings, "alpha", "technical")
    await configure(providers, settings, "beta", "success")
    task = await task_view(client)
    _, configs = providers
    for provider in ("alpha", "beta"):
        assert await send_to_provider(application, settings, providers)
        assert await deliver_once(application.state.sessions, configs[provider], client)
    final = (await client.get("/tasks/" + task["task_id"])).json()
    assert final["status"] == "completed"
    assert [a["provider"] for a in final["attempts"]] == ["alpha", "beta"]
    await assert_balance(application, 98)


async def test_provider_idempotency_concurrent_and_after_restart(application, settings, providers):
    clients, configs = providers
    await configure(providers, settings, "alpha", "success")
    attempt_id = str(uuid.uuid4())
    headers = {"X-API-Key": settings.provider_api_key, "Idempotency-Key": attempt_id}
    payload = {
        "attempt_id": attempt_id,
        "prompt": "test",
        "model_slug": "image-fast",
        "ref_images": [],
    }
    responses = await asyncio.gather(
        *(clients["alpha"].post("/tasks", json=payload, headers=headers) for _ in range(20))
    )
    assert all(r.status_code == 202 and r.json()["external_id"] == attempt_id for r in responses)
    restarted = create_provider(configs["alpha"], background=False)
    async with (
        restarted.router.lifespan_context(restarted),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted), base_url="http://alpha"
        ) as new_client,
    ):
        assert (await new_client.post("/tasks", json=payload, headers=headers)).status_code == 202
        assert (
            await new_client.post("/tasks", json={**payload, "prompt": "changed"}, headers=headers)
        ).status_code == 409
    async with application.state.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ProviderJob)) == 1


async def test_crash_after_provider_accept_reuses_logical_attempt(
    client, application, settings, providers
):
    await payment(client, settings)
    await configure(providers, settings, "alpha", "success")
    task = await task_view(client)
    job = await claim(application.state.sessions, settings)
    clients, configs = providers
    assert (
        await clients["alpha"].post(
            "/tasks",
            json=job["payload"],
            headers={
                "X-API-Key": settings.provider_api_key,
                "Idempotency-Key": str(job["attempt_id"]),
            },
        )
    ).status_code == 202
    # Simulate a process dying after provider COMMIT but before saving the HTTP response.
    async with application.state.sessions() as session, session.begin():
        await session.execute(
            update(Outbox).values(lease_until=text("clock_timestamp() - interval '1 second'"))
        )
    assert await send_to_provider(application, settings, providers)
    async with application.state.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ProviderJob)) == 1
        row = await session.get(Outbox, job["attempt_id"])
        assert row.deliveries == 2 and row.done
    await deliver_once(application.state.sessions, configs["alpha"], client)
    assert (await client.get("/tasks/" + task["task_id"])).json()["status"] == "completed"
    await assert_balance(application, 98)


async def test_provider_callback_retried_after_failure(client, application, settings, providers):
    await payment(client, settings)
    await configure(providers, settings, "alpha", "success")
    task = await task_view(client)
    await send_to_provider(application, settings, providers)
    _, configs = providers
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(503))
    ) as bad:
        await deliver_once(application.state.sessions, configs["alpha"], bad)
    async with application.state.sessions() as session, session.begin():
        row = await session.get(
            ProviderJob, ("alpha", uuid.UUID(task["attempts"][0]["attempt_id"]))
        )
        assert not row.delivered
        row.lease_until = await session.scalar(select(func.clock_timestamp()))
    assert await deliver_once(application.state.sessions, configs["alpha"], client)
    assert (await client.get("/tasks/" + task["task_id"])).json()["status"] == "completed"


async def test_provider_admin_requires_auth(providers):
    clients, _ = providers
    assert (
        await clients["alpha"].post("/admin/scenario", json={"scenario": "success"})
    ).status_code == 401
