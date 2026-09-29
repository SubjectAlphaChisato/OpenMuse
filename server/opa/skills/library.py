"""Skill library: every capability the assistant has is a skill described by a SKILL.md file.

A skill is a folder with a SKILL.md: YAML front matter, then Markdown instructions for the model.

    ---
    name: gmail                   # id; must match the folder name
    title: Gmail
    description: One line the model sees before loading the skill: what it is for and when to use it.
    summary: Optional one line for the owner (Skills screen); defaults to the description.
    requires: gmail               # optional: the account capability the skill needs (see connectors/registry.py)
    tools: [gmail__search_messages, gmail__send_message]   # built-in skills only: the tools this skill offers
    skills: [gmail, calendar]     # optional: other skills loaded with this one (workflow skills)
    ---
    Instructions...

Built-in skills live in opa/skills/builtin. The deployment owner may add instruction-only skills in
OPA_SKILLS_DIR: they can combine built-in skills through `skills:` but cannot declare tools, so a
skill file can never add code, new tools, or permissions. Loading a skill only puts its instructions
and tools in front of the model; what a tool may do is still decided by account access and approvals.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

BUILTIN_DIR = Path(__file__).parent / "builtin"
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
MAX_DESCRIPTION = 300
MAX_BODY = 20_000


class SkillError(ValueError):
    pass


@dataclass(frozen=True)
class Skill:
    name: str
    title: str
    description: str
    instructions: str
    summary: str = ""
    tools: tuple[str, ...] = ()
    requires: str | None = None
    skills: tuple[str, ...] = ()
    builtin: bool = True
    path: str = ""


@dataclass
class SkillLibrary:
    skills: dict[str, Skill] = field(default_factory=dict)
    home: dict[str, str] = field(default_factory=dict)  # tool name -> the built-in skill that offers it

    def get(self, name: str) -> Skill | None:
        return self.skills.get(name)

    def closure(self, name: str) -> list[Skill]:
        """The skill and every skill it pulls in, each once, dependencies first."""
        seen: list[Skill] = []

        def walk(n: str) -> None:
            s = self.skills[n]
            for dep in s.skills:
                walk(dep)
            if s not in seen:
                seen.append(s)
        walk(name)
        return seen


def parse(text: str, path: str = "") -> tuple[dict, str]:
    if not text.startswith("---\n"):
        raise SkillError(f"{path}: SKILL.md must start with '---' front matter")
    try:
        _, head, body = text.split("---\n", 2)
    except ValueError:
        raise SkillError(f"{path}: front matter is not closed with '---'") from None
    try:
        meta = yaml.safe_load(head) or {}
    except yaml.YAMLError as e:
        raise SkillError(f"{path}: front matter is not valid YAML ({e.__class__.__name__})") from None
    if not isinstance(meta, dict):
        raise SkillError(f"{path}: front matter must be a mapping")
    return meta, body.strip()


def _skill(folder: Path, *, builtin: bool) -> Skill:
    path = folder / "SKILL.md"
    meta, body = parse(path.read_text(encoding="utf-8"), str(path))
    unknown = set(meta) - {"name", "title", "description", "summary", "requires", "tools", "skills"}
    if unknown:
        raise SkillError(f"{path}: unknown front matter keys {sorted(unknown)}")
    name = str(meta.get("name", ""))
    if not NAME_RE.match(name) or name != folder.name:
        raise SkillError(f"{path}: name must be lowercase letters, digits, or '-' and match the folder name")
    desc = str(meta.get("description", "")).strip()
    if not desc or len(desc) > MAX_DESCRIPTION:
        raise SkillError(f"{path}: description is required and at most {MAX_DESCRIPTION} characters")
    if not body or len(body) > MAX_BODY:
        raise SkillError(f"{path}: instructions are required and at most {MAX_BODY} characters")
    tools = tuple(meta.get("tools") or ())
    if tools and not builtin:
        raise SkillError(f"{path}: only built-in skills may declare tools; use 'skills:' to build on built-in skills")
    summary = str(meta.get("summary") or desc).strip()[:MAX_DESCRIPTION]
    return Skill(name=name, title=str(meta.get("title") or name), description=desc, instructions=body, summary=summary, tools=tools,
                 requires=meta.get("requires"), skills=tuple(meta.get("skills") or ()), builtin=builtin, path=str(path))


def load_library(extra_dir: str | None = None, *, known_tools: set[str] | None = None, capabilities: set[str] | None = None) -> SkillLibrary:
    """Load built-in skills, then the owner's extra skills. Invalid skills raise SkillError: a
    deployment with a broken skill should fail at start-up, not in the middle of a task."""
    lib = SkillLibrary()
    folders = [(f, True) for f in sorted(BUILTIN_DIR.iterdir()) if (f / "SKILL.md").is_file()]
    if extra_dir:
        root = Path(extra_dir)
        if not root.is_dir():
            raise SkillError(f"OPA_SKILLS_DIR {extra_dir} is not a directory")
        folders += [(f, False) for f in sorted(root.iterdir()) if (f / "SKILL.md").is_file()]
    for folder, builtin in folders:
        s = _skill(folder, builtin=builtin)
        if s.name in lib.skills:
            raise SkillError(f"{s.path}: a skill named {s.name} already exists")
        lib.skills[s.name] = s
        for t in s.tools:
            if t in lib.home:
                raise SkillError(f"{s.path}: tool {t} already belongs to skill {lib.home[t]}")
            if known_tools is not None and t not in known_tools:
                raise SkillError(f"{s.path}: unknown tool {t}")
            lib.home[t] = s.name
    for s in lib.skills.values():
        for dep in s.skills:
            if dep not in lib.skills:
                raise SkillError(f"{s.path}: depends on unknown skill {dep}")
        if s.requires and capabilities is not None and s.requires not in capabilities:
            raise SkillError(f"{s.path}: requires unknown capability {s.requires}")
    for s in lib.skills.values():  # reject cycles
        stack: list[str] = []

        def visit(n: str) -> None:
            if n in stack:
                raise SkillError(f"skill dependency cycle: {' -> '.join([*stack, n])}")
            stack.append(n)
            for d in lib.skills[n].skills:
                visit(d)
            stack.pop()
        visit(s.name)
    return lib
