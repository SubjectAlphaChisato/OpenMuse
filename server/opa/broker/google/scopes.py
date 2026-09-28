"""Google OAuth scopes (GMAIL-01, CAL-01). Full URIs, recorded here as the spec requires.

Read scopes are requested at first connect; write scopes are requested incrementally only when the
owner switches a capability to "read and propose changes".
"""
from __future__ import annotations

IDENTITY = ["openid", "email"]
GMAIL_READ = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SEND = "https://www.googleapis.com/auth/gmail.send"
CAL_READ = "https://www.googleapis.com/auth/calendar.readonly"
CAL_WRITE = "https://www.googleapis.com/auth/calendar.events"

READ = {"gmail": GMAIL_READ, "calendar": CAL_READ}
WRITE = {"gmail": GMAIL_SEND, "calendar": CAL_WRITE}

# Scopes that also satisfy a narrower requirement.
IMPLIES = {CAL_WRITE: {CAL_READ}}


def scopes_for_modes(modes: dict[str, str]) -> list[str]:
    out = list(IDENTITY)
    for cap, mode in modes.items():
        if mode in ("read_only", "read_write"):
            out.append(READ[cap])
        if mode == "read_write":
            out.append(WRITE[cap])
    return sorted(set(out), key=out.index)


def effective(granted: list[str] | set[str]) -> set[str]:
    eff = set(granted)
    for s in list(eff):
        eff |= IMPLIES.get(s, set())
    return eff


def allowed_mode(cap: str, wanted: str, granted: list[str]) -> str:
    """Clamp a requested mode to what Google actually granted (granular consent may drop scopes)."""
    eff = effective(granted)
    if wanted == "read_write" and READ[cap] in eff and WRITE[cap] in eff:
        return "read_write"
    if wanted in ("read_only", "read_write") and READ[cap] in eff:
        return "read_only"
    return "off"


def missing_for_mode(cap: str, mode: str, granted: list[str]) -> list[str]:
    need = [s for s in scopes_for_modes({cap: mode}) if s not in IDENTITY]
    eff = effective(granted)
    return [s for s in need if s not in eff]
