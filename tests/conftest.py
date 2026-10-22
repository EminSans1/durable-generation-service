import os
from contextlib import AsyncExitStack

import httpx
import pytest
from sqlalchemy import text

from app.api import create_app
from app.config import Settings
from app.provider import create_provider


@pytest.fixture
def settings():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url or not url.rsplit("/", 1)[-1].startswith("generation_test"):
        pytest.fail("TEST_DATABASE_URL must point to a dedicated generation_test* PostgreSQL DB")
    return Settings(database_url=url)


@pytest.fixture
async def application(settings):
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with app.state.sessions() as session, session.begin():
            await session.execute(
                text(
                    "TRUNCATE provider_jobs, provider_settings, outbox, ledger, attempts, tasks, "
                    "users RESTART IDENTITY CASCADE"
                )
            )
            await session.execute(text("INSERT INTO users (id) VALUES (1), (2)"))
        yield app


@pytest.fixture
async def client(application):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        yield client


@pytest.fixture
async def providers(application, settings):
    async with AsyncExitStack() as stack:
        clients = {}
        configs = {}
        for name in ("alpha", "beta"):
            config = settings.model_copy(update={"provider_name": name})
            app = create_provider(config, background=False)
            await stack.enter_async_context(app.router.lifespan_context(app))
            clients[name] = await stack.enter_async_context(
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://{name}")
            )
            configs[name] = config
        yield clients, configs
