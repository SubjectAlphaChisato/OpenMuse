from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from opa.connectors.requests import ConnectionRequestOut


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str


class ErrorResponse(BaseModel):
    error: ErrorBody


class LoginIn(BaseModel):
    email: str
    password: str


class MeOut(BaseModel):
    id: str
    email: str
    name: str = ""
    role: str = "admin"  # CRM: admin | advisor | partner
    time_zone: str
    assistant_name: str
    response_style: str
    mode: str
    setup_required: list[str]
    csrf_token: str
    approval_mode: str
    version: str


class PreferencesIn(BaseModel):
    approval_mode: Literal["ask_writes", "always_ask"] | None = None
    assistant_name: str | None = Field(default=None, min_length=1, max_length=64)
    response_style: Literal["concise", "detailed", "casual", "formal"] | None = None
    time_zone: str | None = None


class ConversationIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class ConversationOut(BaseModel):
    id: str
    title: str
    is_default: bool
    created_at: datetime


class MessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=20000)
    client_request_id: str = Field(min_length=1, max_length=128)
    task_id: str | None = None  # reply to a task waiting for the user, instead of creating a new task


class MessageOut(BaseModel):
    id: str
    seq: int
    role: str
    content: str
    task_id: str | None
    artifact_ids: list[str]
    created_at: datetime
    connection_request: ConnectionRequestOut | None = None  # v0.2: an account change the assistant asked for, shown as a card


class MessageAccepted(BaseModel):
    message_id: str
    task_id: str
    created: bool


class TaskOut(BaseModel):
    id: str
    conversation_id: str
    intent: str
    state: str
    plan: dict[str, Any]
    current_step: str | None
    blocker_code: str | None
    blocker_detail: str | None
    retry_of: str | None
    usage: dict[str, Any]
    active_ms: int
    version: int
    last_activity_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    outputs: dict[str, Any]


class TaskList(BaseModel):
    items: list[TaskOut]
    next_cursor: str | None


class NotificationOut(BaseModel):
    id: str
    type: str
    title: str
    body: str
    task_id: str | None
    read_at: datetime | None
    created_at: datetime


class RetryOut(BaseModel):
    task_id: str
