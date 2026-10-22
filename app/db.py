from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings

SessionFactory = async_sessionmaker[AsyncSession]


def connect(settings: Settings) -> tuple[AsyncEngine, SessionFactory]:
    engine = create_async_engine(
        settings.database_url, pool_pre_ping=True, pool_size=10, max_overflow=20
    )
    return engine, async_sessionmaker(engine, expire_on_commit=False)
