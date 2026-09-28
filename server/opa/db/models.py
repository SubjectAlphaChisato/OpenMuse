from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from opa.core.ids import new_id

TASK_STATES = (
    "queued",
    "running",
    "waiting_approval",
    "waiting_user",
    "blocked",
    "succeeded",
    "failed",
    "cancelled",
)
TERMINAL_STATES = ("succeeded", "failed", "cancelled")

# Allowed transitions per spec section 5.
TASK_TRANSITIONS: dict[str, set[str]] = {
    "queued": {"running", "cancelled"},
    "running": {"waiting_approval", "waiting_user", "blocked", "succeeded", "failed", "cancelled", "queued"},
    "waiting_approval": {"queued", "blocked", "cancelled"},
    "waiting_user": {"queued", "cancelled"},
    "blocked": {"queued", "cancelled"},
    "succeeded": set(),
    "failed": set(),
    "cancelled": set(),
}


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, list[str]: ARRAY(String)}


def ts_col(**kw) -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, **kw)


class Owner(Base):
    __tablename__ = "owners"
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    time_zone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    assistant_name: Mapped[str] = mapped_column(String(64), default="Assistant", nullable=False)
    response_style: Mapped[str] = mapped_column(String(32), default="concise", nullable=False)
    preferences: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = ts_col()
    updated_at: Mapped[datetime] = ts_col(onupdate=func.now())


class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    csrf_secret: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = ts_col()


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    at: Mapped[datetime] = ts_col()


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = ts_col()
    updated_at: Mapped[datetime] = ts_col(onupdate=func.now())


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "seq", name="uq_messages_conversation_seq"),
        UniqueConstraint("owner_id", "client_request_id", name="uq_messages_client_request"),
    )
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), nullable=False)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user | assistant | system
    content: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(26), index=True)
    artifact_ids: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    client_request_id: Mapped[str | None] = mapped_column(String(128))
    content_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = ts_col()


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), nullable=False, index=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    intent: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(24), default="queued", nullable=False, index=True)
    plan: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    limits: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    blocker_code: Mapped[str | None] = mapped_column(String(64))
    blocker_detail: Mapped[str | None] = mapped_column(Text)
    retry_of: Mapped[str | None] = mapped_column(String(26))
    injected_memory_ids: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    active_ms: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    current_step: Mapped[str | None] = mapped_column(Text)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = ts_col()
    updated_at: Mapped[datetime] = ts_col(onupdate=func.now())


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), unique=True, nullable=False)
    ready_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


Index("ix_jobs_ready", Job.ready_at, Job.lease_expires_at)


class Event(Base):
    """Transactional outbox. Also the SSE replay log."""

    __tablename__ = "events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(26))
    action_id: Mapped[str | None] = mapped_column(String(26))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = ts_col()


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), nullable=False, index=True)
    event_id: Mapped[int | None] = mapped_column(BigInteger)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(26))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = ts_col()


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    owner_id: Mapped[str | None] = mapped_column(String(26), index=True)
    actor: Mapped[str] = mapped_column(String(32), nullable=False)  # owner | worker | scheduler | broker
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_id: Mapped[str | None] = mapped_column(String(64))
    sanitized: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = ts_col()


# ---------------------------------------------------------------- M2: connections, ledger, approvals

ACTION_STATES = (
    "awaiting_approval", "approved", "dispatching", "succeeded", "failed", "unknown",
    "denied", "expired", "cancelled",
)
APPROVAL_STATES = ("pending", "approved", "denied", "expired", "invalidated", "cancelled")


class Connection(Base):
    """One provider account. Per-capability access modes live in `modes` (e.g. gmail/calendar)."""

    __tablename__ = "connections"
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)  # google | fixture
    account_label: Mapped[str] = mapped_column(String(320), nullable=False)
    provider_account_id: Mapped[str | None] = mapped_column(String(320))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="connected")
    modes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    granted_scopes: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    credential_ref: Mapped[str | None] = mapped_column(String(26))
    last_health_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = ts_col()
    updated_at: Mapped[datetime] = ts_col(onupdate=func.now())


class OAuthState(Base):
    """Single-use, session-bound OAuth authorization request (CON-01). Holds no tokens."""

    __tablename__ = "oauth_states"
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_id)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), nullable=False)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    code_verifier: Mapped[str] = mapped_column(String(128), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    target_connection_id: Mapped[str | None] = mapped_column(String(26))
    requested_modes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = ts_col()


class Action(Base):
    """Immutable proposed external operation plus its dispatch record (spec section 5)."""

    __tablename__ = "actions"
    __table_args__ = (UniqueConstraint("task_id", "tool_use_id", name="uq_actions_task_tool_use"),)
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    tool_use_id: Mapped[str] = mapped_column(String(128), nullable=False)
    connection_id: Mapped[str] = mapped_column(ForeignKey("connections.id", ondelete="CASCADE"), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(96), nullable=False)
    tool_version: Mapped[str] = mapped_column(String(16), nullable=False)
    effect: Mapped[str] = mapped_column(String(8), nullable=False)
    canonical_args: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    args_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    preview: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    state: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    provider_idempotency_key: Mapped[str | None] = mapped_column(String(128))
    provider_refs: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    resolution: Mapped[str | None] = mapped_column(String(32))  # how an unknown outcome was resolved
    dispatch_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatch_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token_at_dispatch: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = ts_col()
    updated_at: Mapped[datetime] = ts_col(onupdate=func.now())


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), nullable=False, index=True)
    action_id: Mapped[str] = mapped_column(ForeignKey("actions.id", ondelete="CASCADE"), nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    args_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = ts_col()


# One effective approval per action (spec 5, step 2).
Index(
    "uq_approvals_one_effective",
    Approval.action_id,
    unique=True,
    postgresql_where=Approval.state.in_(("pending", "approved")),
)


# ---------------------------------------------------------------- broker schema (opa_broker role only)


class BrokerCredential(Base):
    __tablename__ = "credentials"
    __table_args__ = {"schema": "broker"}
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_id)
    connection_id: Mapped[str] = mapped_column(String(26), nullable=False, unique=True)
    dek_wrapped: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    secret_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    access_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refresh_lock_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = ts_col()
    updated_at: Mapped[datetime] = ts_col(onupdate=func.now())


class DispatchClaim(Base):
    __tablename__ = "dispatch_claims"
    __table_args__ = {"schema": "broker"}
    action_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    claimed_at: Mapped[datetime] = ts_col()
    worker_lease_token: Mapped[str] = mapped_column(String(64), nullable=False)


class FixtureState(Base):
    """Persistent mailbox/calendar for the deterministic fixture provider (demo mode and tests)."""

    __tablename__ = "fixture_state"
    __table_args__ = {"schema": "broker"}
    connection_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    faults: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    updated_at: Mapped[datetime] = ts_col(onupdate=func.now())


# ---------------------------------------------------------------- M4: memory (MEM-01) and artifacts (ART-01)

MEMORY_KINDS = ("user_statement", "inference")
ALWAYS_INJECTED_TAGS = ("style", "identity")


class Memory(Base):
    """One remembered item. `scope` is 'global' or a conversation ID. Forgetting deletes the row,
    which also removes its full-text index entry (MEM-01)."""

    __tablename__ = "memories"
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), nullable=False, index=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    scope: Mapped[str] = mapped_column(String(26), nullable=False, default="global")
    source_ref: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    tsv: Mapped[Any] = mapped_column(TSVECTOR, Computed("to_tsvector('english', text)", persisted=True))
    created_at: Mapped[datetime] = ts_col()
    updated_at: Mapped[datetime] = ts_col()


Index("ix_memories_tsv", Memory.tsv, postgresql_using="gin")


class Artifact(Base):
    """Stable file identity. Bytes live in immutable, content-addressed versions (ART-01)."""

    __tablename__ = "artifacts"
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), nullable=False, index=True)
    task_id: Mapped[str | None] = mapped_column(String(26), index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    mime: Mapped[str] = mapped_column(String(64), nullable=False)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)  # upload | generated | owner
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = ts_col()
    updated_at: Mapped[datetime] = ts_col()


class ArtifactVersion(Base):
    __tablename__ = "artifact_versions"
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.id", ondelete="CASCADE"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    storage_key: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_refs: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    created_by_task_id: Mapped[str | None] = mapped_column(String(26))
    note: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)  # e.g. CSV escaping, extraction status
    extracted_text: Mapped[str | None] = mapped_column(Text)  # PDFs only; text types are read from their bytes
    created_at: Mapped[datetime] = ts_col()
