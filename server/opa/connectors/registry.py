"""Provider registry: which services exist, what each can do, and how to sign in (CON-01).

The API, broker, permission checks, and UI all read this instead of hard-coding providers.
A capability's modes are off / read only, plus "read and propose changes" only when write tools exist.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from opa.broker.google import scopes as gscopes


@dataclass(frozen=True)
class Capability:
    id: str
    label: str
    writable: bool = False  # true only when approval-gated write tools exist

    @property
    def modes(self) -> tuple[str, ...]:
        return ("off", "read_only", "read_write") if self.writable else ("off", "read_only")


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    capabilities: tuple[Capability, ...]
    scoped: bool = False  # uses OAuth scopes that must match capability modes (Google)
    pkce: bool = True
    oauth: bool = True  # False for the built-in demo account
    setup: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def capability(self, cap: str) -> Capability | None:
        return next((c for c in self.capabilities if c.id == cap), None)

    @property
    def capability_ids(self) -> list[str]:
        return [c.id for c in self.capabilities]


GMAIL = Capability("gmail", "Gmail", writable=True)
CALENDAR = Capability("calendar", "Calendar", writable=True)
DRIVE = Capability("drive", "Drive and Docs")
NOTION = Capability("notion", "Notion pages and databases")
GITHUB = Capability("github", "GitHub repositories")

PROVIDERS: dict[str, Provider] = {
    "google": Provider("google", "Google (Gmail, Calendar, Drive)", (GMAIL, CALENDAR, DRIVE), scoped=True,
                       setup="set OPA_GOOGLE_CLIENT_ID and OPA_GOOGLE_CLIENT_SECRET (see README, Connect Google)"),
    "notion": Provider("notion", "Notion", (NOTION,), pkce=False,
                       setup="set OPA_NOTION_CLIENT_ID and OPA_NOTION_CLIENT_SECRET for a public Notion integration (see docs/providers/notion.md)"),
    "github": Provider("github", "GitHub", (GITHUB,),
                       setup="set OPA_GITHUB_CLIENT_ID and OPA_GITHUB_CLIENT_SECRET for a read-only GitHub App (see docs/providers/github.md)"),
    "fixture": Provider("fixture", "Demo account (sample mail, calendar, Drive, Notion, and GitHub; no real provider)",
                        (GMAIL, CALENDAR, DRIVE, NOTION, GITHUB), oauth=False),
}
OAUTH_PROVIDERS = tuple(p for p, spec in PROVIDERS.items() if spec.oauth)
SHORT = {"google": "Google", "notion": "Notion", "github": "GitHub", "fixture": "Demo"}


def get(provider: str) -> Provider | None:
    return PROVIDERS.get(provider)


def client_id(settings, provider: str) -> str | None:
    return getattr(settings, f"{provider}_client_id", None)


def client_secret(settings, provider: str) -> str | None:
    return getattr(settings, f"{provider}_client_secret", None)


def ready(settings, provider: str) -> str | None:
    """None when the provider can be connected now, otherwise the reason."""
    spec = PROVIDERS.get(provider)
    if spec is None:
        return "unknown provider"
    if not spec.oauth:
        return None if settings.mode == "demo" else "the demo account is available in demo mode only"
    if settings.mode != "live":
        return f"{spec.label} is available in live mode only; demo mode never contacts providers"
    if not client_id(settings, provider):
        return spec.setup
    return None


def redirect_uri(settings, provider: str) -> str:
    if provider == "google":
        return settings.google_redirect_uri
    return f"{settings.public_base_url.rstrip('/')}/api/oauth/{provider}/callback"


def scopes_for_modes(provider: str, modes: dict[str, str]) -> list[str]:
    return gscopes.scopes_for_modes(modes) if PROVIDERS[provider].scoped else []


def allowed_mode(provider: str, cap: str, wanted: str, granted: list[str]) -> str:
    """Clamp a requested mode to what the capability supports and, for scoped providers, what was granted."""
    capability = PROVIDERS[provider].capability(cap)
    if capability is None:
        return "off"
    if wanted not in capability.modes:
        wanted = "read_only"
    return gscopes.allowed_mode(cap, wanted, granted) if PROVIDERS[provider].scoped else wanted


def missing_scopes(provider: str, cap: str, mode: str, granted: list[str]) -> list[str]:
    return gscopes.missing_for_mode(cap, mode, granted) if PROVIDERS[provider].scoped else []


def full_modes(provider: str, modes: dict[str, str]) -> dict[str, str]:
    """Every capability of the provider, in declaration order; missing ones are off."""
    spec = PROVIDERS.get(provider)
    if spec is None:
        return dict(modes)
    return {c: modes.get(c, "off") for c in spec.capability_ids}


def describe(provider: str) -> list[dict[str, Any]]:
    spec = PROVIDERS[provider]
    return [{"id": c.id, "label": c.label, "modes": list(c.modes)} for c in spec.capabilities]
