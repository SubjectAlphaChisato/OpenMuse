"""Skill harness: what the model sees and may call, built from the skill library.

Each task starts with a short catalog of skills (name, one line, status) and a few core tools:
ask_user, note, skill__load, connections__list, and connections__request. Loading a skill adds its
instructions and tools for the rest of the task (kept in the checkpoint). A call to a tool of a skill
that was not loaded loads that skill implicitly, because skills organize context, not permission:
what a tool may do is decided by account access, the broker, and approvals, exactly as before.
A skill the owner turned off is hidden from the model and its tools are refused.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Any

from opa.connectors import registry
from opa.skills.library import Skill, SkillLibrary, load_library

CORE_TOOLS = ("ask_user", "note", "skill__load", "connections__list", "connections__request")
READY = "ready"


@lru_cache(maxsize=4)
def library_for(extra_dir: str | None) -> SkillLibrary:
    """The deployment's skills, checked against the tools that actually exist."""
    from opa.broker import ops
    from opa.worker.browser_tools import ACTIONS
    from opa.worker.output_tools import TOOLS

    known = {t.wire_name for t in ops.TOOLS.values()} | {t["name"] for t in TOOLS} | set(ACTIONS)
    known -= set(CORE_TOOLS)
    caps = {c.id for p in registry.PROVIDERS.values() for c in p.capabilities}
    return load_library(extra_dir, known_tools=known, capabilities=caps)


@dataclass
class SkillStatus:
    name: str
    title: str
    description: str
    summary: str
    status: str  # ready | not_connected | off | needs_reconnect | unavailable | disabled
    detail: str
    requires: str | None
    builtin: bool
    enabled: bool
    tools: list[str]
    skills: list[str]


def _leaf_status(skill: Skill, settings: Any, conns: list[Any], browser_ready: bool) -> tuple[str, str]:
    if skill.name == "browser" and not browser_ready:
        return "unavailable", "the browser service is not configured"
    cap = skill.requires
    if not cap:
        return READY, ""
    providers = [pid for pid, spec in registry.PROVIDERS.items() if cap in spec.capability_ids and (spec.oauth or settings.mode == "demo")]
    mine = [c for c in conns if c.provider in providers and c.status != "disconnected"]
    usable = [c for c in mine if c.status == "connected" and registry.full_modes(c.provider, c.modes).get(cap, "off") != "off"]
    if usable:
        modes = {"read_only": "read only", "read_write": "read and propose changes"}
        return READY, "; ".join(f"{c.account_label} ({modes[registry.full_modes(c.provider, c.modes)[cap]]})" for c in usable)
    stale = [c for c in mine if c.status == "reauth_required"]
    if stale:
        return "needs_reconnect", f"{stale[0].account_label} needs to be reconnected"
    if mine:
        return "off", f"turned off for {', '.join(c.account_label for c in mine)}"
    connectable = [p for p in providers if registry.ready(settings, p) is None]
    if connectable:
        return "not_connected", "connect " + " or ".join(registry.SHORT.get(p, p) for p in connectable)
    return "unavailable", (registry.ready(settings, providers[0]) if providers else "no provider offers this") or "not configured"


ORDER = ["gmail", "calendar", "drive", "notion", "github", "files", "memory", "automations", "browser"]


def _order(s: Skill) -> tuple[int, int, str]:
    group = 0 if s.requires else 1 if s.tools else 2 if s.builtin else 3
    return (group, ORDER.index(s.name) if s.name in ORDER else len(ORDER), s.title.lower())


def statuses(lib: SkillLibrary, settings: Any, conns: list[Any], disabled: set[str], browser_ready: bool) -> list[SkillStatus]:
    leaf = {n: _leaf_status(s, settings, conns, browser_ready) for n, s in lib.skills.items()}
    out = []
    for name, s in sorted(lib.skills.items(), key=lambda kv: _order(kv[1])):
        status, detail = leaf[name]
        for dep in lib.closure(name)[:-1]:  # a workflow skill is ready only when everything it uses is
            if status == READY and leaf[dep.name][0] != READY:
                status, detail = leaf[dep.name][0], f"needs {dep.title}: {leaf[dep.name][1]}"
        enabled = name not in disabled
        out.append(SkillStatus(name=name, title=s.title, description=s.description, summary=s.summary, status=status if enabled else "disabled",
                               detail=detail if enabled else "turned off by the owner", requires=s.requires, builtin=s.builtin,
                               enabled=enabled, tools=list(s.tools), skills=list(s.skills)))
    return out


def disabled_skills(owner: Any) -> set[str]:
    return set((owner.preferences or {}).get("disabled_skills", []))


class TaskSkills:
    """The harness for one task run."""

    def __init__(self, lib: SkillLibrary, catalog: list[SkillStatus], all_defs: list[dict[str, Any]], loaded: list[str] | None = None) -> None:
        self.lib = lib
        self.catalog = {s.name: s for s in catalog if s.enabled}
        self.disabled = {s.name for s in catalog if not s.enabled}
        self.defs = {d["name"]: d for d in all_defs}
        self.loaded: list[str] = [n for n in (loaded or []) if n in self.catalog]

    # ---- what the model sees
    def system_block(self) -> str:
        lines = ["## Skills",
                 "Your capabilities are skills. Before using one, call skill__load with its name to get its instructions and tools; "
                 "load only what the request needs. Skills marked other than ready need something from the owner first.",
                 "Account access is the owner's decision. To connect an account, change what you may do with it, or disconnect it, "
                 "call connections__request; the owner confirms it in the chat. Never send the owner to a settings screen instead, "
                 "and never ask for more access than the task needs."]
        for s in self.catalog.values():
            state = s.status.replace("_", " ") + (f": {s.detail}" if s.detail and s.status != READY else "")
            lines.append(f"- {s.name} ({state}): {s.description}")
        return "\n".join(lines)

    def load_def(self) -> dict[str, Any]:
        return {"name": "skill__load",
                "description": "Load a skill: returns its instructions and makes its tools available for the rest of this task.",
                "input_schema": {"type": "object", "properties": {"name": {"type": "string", "enum": sorted(self.catalog)}},
                                 "required": ["name"], "additionalProperties": False}}

    def offered(self) -> list[dict[str, Any]]:
        names = [n for n in CORE_TOOLS if n in self.defs]
        for skill in self.loaded:
            names += [t for t in self.lib.skills[skill].tools if t in self.defs and t not in names]
        return [self.load_def() if n == "skill__load" else self.defs[n] for n in ["skill__load", *[n for n in names if n != "skill__load"]]]

    # ---- calls
    def load(self, name: str) -> dict[str, Any]:
        if name not in self.catalog:
            reason = "the owner turned this skill off" if name in self.disabled else "no such skill"
            return {"ok": False, "content": json.dumps({"status": "failed", "data": None, "error": {"code": "skill_unavailable", "message": reason}})}
        parts = self.lib.closure(name)
        for s in parts:
            if s.name not in self.loaded and s.name in self.catalog:
                self.loaded.append(s.name)
        tools = [t for s in parts for t in s.tools if t in self.defs]
        missing = [t for s in parts for t in s.tools if t not in self.defs]
        st = self.catalog[name]
        data: dict[str, Any] = {"skill": name, "status": st.status, "detail": st.detail,
                                "instructions": [{"skill": s.name, "title": s.title, "text": s.instructions} for s in parts],
                                "tools_available": tools}
        if missing:
            data["tools_not_available"] = missing
        if st.status != READY:
            data["next"] = ("This skill needs the owner first (" + (st.detail or st.status) + "). If the task needs it, call "
                            "connections__request so the owner can confirm in the chat, tell them, and end your turn.")
        return {"ok": True, "content": json.dumps({"status": "succeeded", "data": data, "error": None}, ensure_ascii=False)}

    def admit(self, tool_name: str) -> str | None:
        """Before a call: None if it may proceed (loading its skill if needed), otherwise why not."""
        if tool_name in CORE_TOOLS:
            return None
        home = self.lib.home.get(tool_name)
        if home is None:
            return None  # not a skill tool; the dispatcher reports unknown tools
        if home in self.disabled:
            return f"the owner turned off the {self.lib.skills[home].title} skill"
        if tool_name not in self.defs:
            st = self.catalog.get(home)
            title = self.lib.skills[home].title
            if st and st.status == READY:
                return (f"{tool_name} is not available with the current {title} access ({st.detail}); "
                        "if the owner wants it, call connections__request to ask for 'read_write'")
            return f"{title} is not ready ({st.detail if st else 'unavailable'}); use connections__request if the owner wants it"
        if home not in self.loaded:
            self.loaded.append(home)
        return None


def as_dict(s: SkillStatus) -> dict[str, Any]:
    return asdict(s)
