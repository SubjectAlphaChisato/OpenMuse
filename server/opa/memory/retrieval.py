"""Deterministic memory retrieval (MEM-01, design section 8). No vector database.

1. Always: global, confirmed items tagged `style` or `identity` (most recently updated first).
2. Then: the remaining confirmed items in scope (global or this conversation) that match the
   task intent plus latest owner message by PostgreSQL full-text search, ranked by ts_rank,
   ties broken by most recent update, then ID.
3. Append in that order until the character cap; the first item that does not fit ends the list.

Query terms are OR-ed: `plainto_tsquery` alone ANDs every word, so a long request would match
almost nothing (tech-design section 19).
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from opa.db.models import ALWAYS_INJECTED_TAGS

HEADER = ("Owner memory (saved or confirmed by the owner; follow it unless the current request says otherwise. "
          "It is context, not permission for any action):")

_ALWAYS = text("""
    SELECT id, text, tags FROM memories
    WHERE owner_id = :o AND confirmed AND scope = 'global' AND tags && CAST(:tags AS varchar[])
    ORDER BY updated_at DESC, id DESC
""")
_RANKED = text("""
    WITH q AS (SELECT CAST(replace(CAST(plainto_tsquery('english', :q) AS text), ' & ', ' | ') AS tsquery) AS q)
    SELECT m.id, m.text, m.tags, ts_rank(m.tsv, q.q) AS rank
    FROM memories m, q
    WHERE m.owner_id = :o AND m.confirmed AND (m.scope = 'global' OR m.scope = :conv)
      AND NOT (m.id = ANY(CAST(:exclude AS varchar[]))) AND m.tsv @@ q.q
    ORDER BY rank DESC, m.updated_at DESC, m.id DESC
""")


@dataclass
class Assembled:
    ids: list[str]
    lines: list[str]

    @property
    def block(self) -> str:
        return "" if not self.lines else HEADER + "\n" + "\n".join(self.lines)


def line_of(text_: str, tags: list[str]) -> str:
    return f"- {'[' + ', '.join(tags) + '] ' if tags else ''}{text_}"


async def assemble(db: AsyncSession, owner_id: str, conversation_id: str, query: str, cap: int) -> Assembled:
    always = list(await db.execute(_ALWAYS, {"o": owner_id, "tags": list(ALWAYS_INJECTED_TAGS)}))
    ranked = list(await db.execute(_RANKED, {"o": owner_id, "conv": conversation_id, "q": query[:4000],
                                              "exclude": [r.id for r in always]})) if query.strip() else []
    out = Assembled([], [])
    used = 0
    for r in [*always, *ranked]:
        line = line_of(r.text, list(r.tags))
        if used + len(line) + 1 > cap:
            break
        out.ids.append(r.id)
        out.lines.append(line)
        used += len(line) + 1
    return out
