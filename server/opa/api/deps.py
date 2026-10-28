from __future__ import annotations

from typing import Annotated, AsyncIterator, TypeVar

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opa.api.auth import SESSION_COOKIE, check_csrf, resolve_session
from opa.core.clock import Clock
from opa.core.errors import Forbidden, NotFound, Unauthorized
from opa.core.settings import Settings
from opa.db.models import User, Session


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


async def current_owner(db: DbDep, sess: Annotated[Session, Depends(current_session)]) -> User:
    owner = await db.get(User, sess.owner_id)
    if owner is None or owner.status != "active":  # a suspended or deactivated account loses access on its next request
        raise Unauthorized("sign in required")
    return owner


def require_roles(*roles: str):
    async def dep(user: Annotated[User, Depends(current_owner)]) -> User:
        if user.role not in roles:
            raise Forbidden(f"this needs the {' or '.join(roles)} role")
        return user
    return dep


OwnerDep = Annotated[User, Depends(current_owner)]
SessionDep = Annotated[Session, Depends(current_session)]
AdminDep = Annotated[User, Depends(require_roles("admin"))]
StaffDep = Annotated[User, Depends(require_roles("admin", "advisor"))]  # CRM: admins and advisors; never partner users

T = TypeVar("T")


async def get_owned(db: AsyncSession, model: type[T], obj_id: str, owner: User) -> T:
    """Load an owner-scoped row or 404. Other owners' rows are indistinguishable from missing."""
    obj = await db.scalar(select(model).where(model.id == obj_id, model.owner_id == owner.id))  # type: ignore[attr-defined]
    if obj is None or getattr(obj, "deleted_at", None) is not None:
        raise NotFound(f"{model.__name__.lower()} not found")
    return obj
