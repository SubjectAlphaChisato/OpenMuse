from __future__ import annotations

from typing import Annotated, AsyncIterator, TypeVar

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opa.api.auth import SESSION_COOKIE, check_csrf, resolve_session
from opa.core.clock import Clock
from opa.core.errors import NotFound, Unauthorized
from opa.core.settings import Settings
from opa.db.models import Owner, Session


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_clock(request: Request) -> Clock:
    return request.app.state.clock


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        async with session.begin():
            yield session


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
ClockDep = Annotated[Clock, Depends(get_clock)]
DbDep = Annotated[AsyncSession, Depends(get_db)]


async def current_session(request: Request, db: DbDep, clock: ClockDep) -> Session:
    sess = await resolve_session(db, clock, request.cookies.get(SESSION_COOKIE))
    if sess is None:
        raise Unauthorized("sign in required")
    check_csrf(request, sess)
    return sess


async def current_owner(db: DbDep, sess: Annotated[Session, Depends(current_session)]) -> Owner:
    owner = await db.get(Owner, sess.owner_id)
    if owner is None:
        raise Unauthorized("sign in required")
    return owner


OwnerDep = Annotated[Owner, Depends(current_owner)]
SessionDep = Annotated[Session, Depends(current_session)]

T = TypeVar("T")


async def get_owned(db: AsyncSession, model: type[T], obj_id: str, owner: Owner) -> T:
    """Load an owner-scoped row or 404. Other owners' rows are indistinguishable from missing."""
    obj = await db.scalar(select(model).where(model.id == obj_id, model.owner_id == owner.id))  # type: ignore[attr-defined]
    if obj is None or getattr(obj, "deleted_at", None) is not None:
        raise NotFound(f"{model.__name__.lower()} not found")
    return obj
