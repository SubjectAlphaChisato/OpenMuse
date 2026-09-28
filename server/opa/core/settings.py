from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Deployment configuration. Every value can be set via environment or .env."""

    model_config = SettingsConfigDict(env_prefix="OPA_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://postgres@localhost:5432/opa"
    mode: Literal["demo", "live"] = "demo"
    model_provider: Literal["scripted", "anthropic"] = "scripted"
    model_id: str = "claude-opus-5"
    model_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    anthropic_api_key: str | None = None
    scripted_model_path: str | None = None

    bind_host: str = "127.0.0.1"
    bind_port: int = 8080
    secure_cookies: bool = False
    session_ttl_hours: int = 24 * 14
    login_max_attempts: int = 5
    login_window_minutes: int = 15

    artifact_dir: str = "./data/artifacts"
    web_dist_dir: str | None = None  # serve the built React app from the API when set

    lease_seconds: int = 60
    heartbeat_seconds: int = 10
    recovery_sweep_seconds: int = 30
    task_max_model_calls: int = 30
    task_max_tool_dispatches: int = 50
    task_max_active_seconds: int = 600
    tool_timeout_seconds: int = 120

    memory_context_chars: int = 8000

    # M2: broker, approvals, egress
    internal_token: str = ""  # shared bearer for internal RPC; required by `opa broker`
    broker_url: str = "http://127.0.0.1:8081"
    broker_bind_port: int = 8081
    broker_database_url: str | None = None  # opa_broker role; defaults to database_url in dev
    broker_kek: str | None = None  # base64 32-byte key-encryption key; broker only
    approval_ttl_hours: int = 24
    model_proxy: str | None = None  # e.g. http://egress-proxy:3128; worker's only route to the model

    # M3: Google. The api needs only the client ID; the secret is read by the broker alone.
    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_redirect_uri: str = "http://127.0.0.1:8080/api/oauth/google/callback"
    google_oauth_testing: bool = True  # unverified apps in Testing mode get 7-day refresh tokens
    google_auth_url: str = "https://accounts.google.com/o/oauth2/v2/auth"
    google_token_url: str = "https://oauth2.googleapis.com/token"
    google_revoke_url: str = "https://oauth2.googleapis.com/revoke"
    google_userinfo_url: str = "https://openidconnect.googleapis.com/v1/userinfo"
    gmail_api_base: str = "https://gmail.googleapis.com/gmail/v1"
    calendar_api_base: str = "https://www.googleapis.com/calendar/v3"
    oauth_state_ttl_minutes: int = 10

    @property
    def sync_database_url(self) -> str:
        return self.database_url.replace("+asyncpg", "+psycopg")


@lru_cache
def get_settings() -> Settings:
    return Settings()
