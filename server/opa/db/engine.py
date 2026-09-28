from __future__ import annotations

from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from opa.core.settings import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    return create_async_engine(get_settings().database_url, pool_pre_ping=True)


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


def reset_engine_cache() -> None:
    """Used by tests after changing OPA_DATABASE_URL."""
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    get_settings.cache_clear()
