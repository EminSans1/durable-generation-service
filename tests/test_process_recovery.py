"""Kill a real OS process after acceptance; recover its durable outbox lease."""

import asyncio
import os
import subprocess
import sys

import httpx
from sqlalchemy import func, select, text, update

from app.models import Outbox, ProviderJob
from app.worker import dispatch_once
from tests.test_finance import assert_balance, payment
from tests.test_providers import configure
from tests.test_transitions import task_view


async def test_real_process_crash_after_accept(client, application, settings, providers):
    await payment(client, settings)
    await configure(providers, settings, "alpha", "success")
    await task_view(client)
    # Child process persists both provider acceptance and outbox claim, then dies with
    # no graceful shutdown. Its ASGI provider executes the exact production /tasks route.
    code = """
import asyncio, os
import httpx
from app.config import get_settings
from app.db import connect
from app.provider import create_provider
from app.worker import claim

async def crash():
    settings = get_settings()
    engine, sessions = connect(settings)
    job = await claim(sessions, settings)
    assert job is not None
    app = create_provider(settings, background=False)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url='http://alpha'
    ) as client:
        response = await client.post('/tasks', json=job['payload'], headers={
            'X-API-Key':settings.provider_api_key, 'Idempotency-Key':str(job['attempt_id'])})
        assert response.status_code == 202, response.text
        os._exit(23)
asyncio.run(crash())
"""
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        env={**os.environ, "DATABASE_URL": settings.database_url, "PROVIDER_NAME": "alpha"},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **options,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
    assert process.returncode == 23, (stdout.decode(), stderr.decode())
    async with application.state.sessions() as session, session.begin():
        row = await session.scalar(select(Outbox))
        assert not row.done and row.deliveries == 1
        await session.execute(
            update(Outbox).values(lease_until=text("clock_timestamp() - interval '1 second'"))
        )
    clients, _ = providers

    async def handler(request):
        return await clients["alpha"].post(
            "/tasks", content=request.content, headers=request.headers
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        assert await dispatch_once(application.state.sessions, settings, transport)
    async with application.state.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ProviderJob)) == 1
        row = await session.scalar(select(Outbox))
        assert row.done and row.deliveries == 2
    await assert_balance(application, 98)
