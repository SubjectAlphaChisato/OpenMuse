from __future__ import annotations

import hmac
import secrets
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Request
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from opa.core.canonical import sha256_hex
from opa.core.clock import Clock
from opa.core.errors import Throttled, Unauthorized
from opa.core.settings import Settings
from opa.db.models import LoginAttempt, User, Session

SESSION_COOKIE = "opa_session"
CSRF_HEADER = "x-csrf-token"

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False


async def _throttle(db: AsyncSession, key: str, settings: Settings, clock: Clock) -> None:
    window_start = clock.now() - timedelta(minutes=settings.login_window_minutes)
    await db.execute(delete(LoginAttempt).where(LoginAttempt.at < window_start))
    count = await db.scalar(
        select(func.count()).select_from(LoginAttempt).where(LoginAttempt.key == key, LoginAttempt.at >= window_start)
    )
    if (count or 0) >= settings.login_max_attempts:
        raise Throttled("too many login attempts; try again later")


async def login(
    db: AsyncSession, settings: Settings, clock: Clock, email: str, password: str, client_ip: str
) -> tuple[Session, str]:
    email = email.strip().lower()
    await _throttle(db, f"email:{email}", settings, clock)
    await _throttle(db, f"ip:{client_ip}", settings, clock)
    owner = await db.scalar(select(User).where(User.email == email))
    ok = owner is not None and verify_password(owner.password_hash, password)
    if not ok:
        # The caller records the failed attempt in its own committed transaction,
        # because raising here rolls back the request transaction.
        raise Unauthorized("invalid credentials")
    if owner.status != "active":  # CRM: suspended, deactivated, and not-yet-accepted accounts cannot sign in
        raise Unauthorized(f"this account is {owner.status}")
    owner.last_login_at = clock.now()
    raw = secrets.token_urlsafe(32)
    sess = Session(
        owner_id=owner.id,
        token_hash=sha256_hex(raw),
        csrf_secret=secrets.token_urlsafe(32),
        expires_at=clock.now() + timedelta(hours=settings.session_ttl_hours),
    )
    db.add(sess)
    await db.flush()
    return sess, raw


async def resolve_session(db: AsyncSession, clock: Clock, raw_token: str | None) -> Session | None:
    if not raw_token:
        return None
    sess = await db.scalar(select(Session).where(Session.token_hash == sha256_hex(raw_token)))
    if sess is None or sess.expires_at <= clock.now():
        return None
    return sess


def check_csrf(request: Request, sess: Session) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    supplied = request.headers.get(CSRF_HEADER, "")
    if not hmac.compare_digest(supplied, sess.csrf_secret):
        raise Unauthorized("missing or invalid CSRF token", code="csrf_failed", status=403)


async def record_failed_attempt(sessionmaker, clock: Clock, email: str, client_ip: str) -> None:
    async with sessionmaker() as db, db.begin():
        db.add(LoginAttempt(key=f"email:{email.strip().lower()}", at=clock.now()))
        db.add(LoginAttempt(key=f"ip:{client_ip}", at=clock.now()))
