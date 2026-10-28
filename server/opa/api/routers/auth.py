from __future__ import annotations

from fastapi import APIRouter, Request, Response
from sqlalchemy import delete

from opa import __version__
from opa.api.auth import SESSION_COOKIE, login, record_failed_attempt
from opa.core.errors import Unauthorized
from opa.api.deps import ClockDep, DbDep, OwnerDep, SessionDep, SettingsDep
from opa.api.schemas import LoginIn, MeOut, PreferencesIn
from opa.core.errors import Invalid
from opa.db.models import Session
from opa.worker.model import setup_problems

router = APIRouter(prefix="/api", tags=["auth"])


@router.post("/auth/login", status_code=204)
async def post_login(body: LoginIn, request: Request, response: Response, db: DbDep, settings: SettingsDep, clock: ClockDep) -> Response:
    ip = request.client.host if request.client else "unknown"
    try:
        sess, raw = await login(db, settings, clock, body.email, body.password, ip)
    except Unauthorized:
        await record_failed_attempt(request.app.state.sessionmaker, clock, body.email, ip)
        raise
    response.set_cookie(
        SESSION_COOKIE, raw, httponly=True, secure=settings.secure_cookies, samesite="lax", path="/",
        max_age=settings.session_ttl_hours * 3600,
    )
    response.status_code = 204
    return response


@router.post("/auth/logout", status_code=204)
async def post_logout(response: Response, db: DbDep, sess: SessionDep) -> Response:
    await db.execute(delete(Session).where(Session.id == sess.id))
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.status_code = 204
    return response


@router.get("/me", response_model=MeOut)
async def get_me(owner: OwnerDep, sess: SessionDep, settings: SettingsDep) -> MeOut:
    return MeOut(
        id=owner.id, email=owner.email, name=owner.name, role=owner.role, time_zone=owner.time_zone, assistant_name=owner.assistant_name,
        response_style=owner.response_style, mode=settings.mode, setup_required=setup_problems(settings),
        csrf_token=sess.csrf_secret, approval_mode=owner.preferences.get("approval_mode", "ask_writes"), version=__version__,
    )


@router.patch("/me", response_model=MeOut)
async def patch_me(body: PreferencesIn, owner: OwnerDep, sess: SessionDep, settings: SettingsDep) -> MeOut:
    if body.time_zone is not None:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(body.time_zone)
        except (ZoneInfoNotFoundError, ValueError):
            raise Invalid("time_zone must be an IANA zone such as America/Los_Angeles") from None
        owner.time_zone = body.time_zone
    if body.approval_mode is not None:
        owner.preferences = {**owner.preferences, "approval_mode": body.approval_mode}
    if body.assistant_name is not None:
        owner.assistant_name = body.assistant_name
    if body.response_style is not None:
        owner.response_style = body.response_style
    return await get_me(owner, sess, settings)
