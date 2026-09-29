"""Connector SDK contract (spec section 6). Shared by fixture and real adapters."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

AccessMode = Literal["off", "read_only", "read_write"]
Effect = Literal["read", "write"]
RetrySafety = Literal["safe_read", "provider_idempotent", "unsafe"]

ERROR_CODES = (
    "auth_required",
    "permission_denied",
    "approval_required",
    "rate_limited",
    "not_found",
    "conflict",
    "invalid_input",
    "provider_unavailable",
    "unknown_outcome",
)


class ToolDefinition(BaseModel):
    name: str  # namespaced, e.g. "gmail.send_message"
    version: str
    capability: str  # which connection mode governs this tool: "gmail" | "calendar"
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    effect: Effect
    required_scopes: list[str] = Field(default_factory=list)
    retry_safety: RetrySafety
    sends_data_externally: bool
    side_effects: str = ""  # shown on the approval card

    @property
    def wire_name(self) -> str:
        """Claude tool names may not contain dots."""
        return self.name.replace(".", "__")


class ToolRequest(BaseModel):
    task_id: str
    action_id: str
    connection_id: str
    tool_name: str
    tool_version: str
    arguments: dict[str, Any]
    approval_id: Optional[str] = None  # lookup only; not proof of authorization


class ToolError(BaseModel):
    code: str
    message: str
    retry_after_seconds: Optional[int] = None


class ToolResult(BaseModel):
    status: Literal["succeeded", "failed", "unknown"]
    data: Optional[Any] = None
    provider_references: list[str] = Field(default_factory=list)
    source_references: list[str] = Field(default_factory=list)
    error: Optional[ToolError] = None

    @classmethod
    def fail(cls, code: str, message: str, **kw: Any) -> "ToolResult":
        return cls(status="failed", error=ToolError(code=code, message=message, **kw))


class ConnectorError(Exception):
    """Raised by adapters and the broker; maps to a stable error code."""

    def __init__(self, code: str, message: str, retry_after_seconds: int | None = None) -> None:
        assert code in ERROR_CODES, code
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after_seconds = retry_after_seconds


class SyncTokenExpired(Exception):
    """The provider no longer accepts the incremental sync token; a full resync is required (Google 410)."""


class OutcomeUnknown(Exception):
    """Raised by an adapter when a write may or may not have reached the provider."""
