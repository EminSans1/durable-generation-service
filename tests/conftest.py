import os

import httpx
import pytest
from sqlalchemy import text

from app.api import create_app
from app.config import Settings


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
