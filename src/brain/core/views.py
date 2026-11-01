"""Read models for front-ends: search hits, entity pages, and entries.

These are plain data so the MCP server and the web UI can serialize them
directly. They read only from the index.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from brain.core.answers import NODE_TYPES, AmbiguousTarget, find_targets, retrieve
from brain.core.index import queries, search
from brain.core.llm.models import LLM

SNIPPET_CHARS = 280


@dataclass
class Hit:
    id: str
    type: str  # entry | fragment | person | org | theme | thread
    label: str
    snippet: str
    date: str | None = None
    entry_id: str | None = None
    kind: str | None = None
    status: str | None = None


@dataclass
class FragmentView:
    id: str
    entry_id: str
    date: str
    kind: str
    text: str
    status: str
    state: str | None
    dream: bool
    obe_reason: str | None


@dataclass
class EntityView:
    id: str  # node id, e.g. "person:jane-doe"
    type: str
    name: str
    aliases: list[str]
    details: dict
    notes: str
    summary: str | None
    related: dict[str, list[str]]  # type -> labels of linked threads, people, orgs, themes
    open_items: list[FragmentView]
    recent: list[FragmentView]
    fragment_count: int


@dataclass
class EntryView:
    id: str
    date: str
    title: str | None
    text: str
    key: str
    processed: bool
    fragments: list[FragmentView] = field(default_factory=list)


def fragment_view(f: queries.FragmentRow) -> FragmentView:
    return FragmentView(f.id, f.entry_id, f.created_at[:10], f.kind, f.text, f.status, f.state, f.dream, f.obe_reason)


def _snippet(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= SNIPPET_CHARS else text[: SNIPPET_CHARS - 1] + "…"


def search_all(
    conn: sqlite3.Connection, llm: LLM | None, query: str, *, limit: int = 10, types: tuple[str, ...] | None = None
) -> list[Hit]:
    """Entries and fragments by keyword and meaning, plus entities by name, best first."""
    want = set(types or ("entry", "fragment", *NODE_TYPES))
    hits: list[Hit] = []
    entity_types = tuple(t for t in NODE_TYPES if t in want)
    if entity_types:
        for h in search.keyword(conn, query, limit=limit, types=entity_types):
            node = conn.execute("SELECT label FROM nodes WHERE id = ?", (f"{h.item_type}:{h.item_id}",)).fetchone()
            body = conn.execute(
                "SELECT body FROM fts WHERE item_id = ? AND item_type = ?", (h.item_id, h.item_type)
            ).fetchone()
            if node:
                hits.append(Hit(f"{h.item_type}:{h.item_id}", h.item_type, node[0], _snippet(body[0] if body else "")))
    if want & {"entry", "fragment"}:
        if llm is not None:
            ranked = retrieve(conn, llm, query, limit=limit * 2)
        else:
            ranked = [
                (h.item_id, h.item_type)
                for h in search.keyword(conn, query, limit=limit * 2, types=("entry", "fragment"))
            ]
        frags = queries.fragments_by_id(conn, [i for i, t in ranked if t == "fragment"])
        for item_id, item_type in ranked:
            if item_type not in want:
                continue
            if item_type == "fragment" and item_id in frags:
                f = frags[item_id]
                hits.append(
                    Hit(
                        f.id, "fragment", f.text[:80], _snippet(f.text), f.created_at[:10], f.entry_id, f.kind, f.status
                    )
                )
            elif item_type == "entry":
                r = conn.execute(
                    "SELECT e.created_at, e.body, n.label FROM entries e JOIN nodes n "
                    "ON n.id = 'entry:' || e.id WHERE e.id = ?",
                    (item_id,),
                ).fetchone()
                if r:
                    hits.append(Hit(item_id, "entry", r[2], _snippet(r[1]), r[0][:10], item_id))
    return hits[:limit]


def entity_view(conn: sqlite3.Connection, text: str, *, recent: int = 20) -> EntityView:
    """An entity by node id, id, alias, or close name. Raises AmbiguousTarget or KeyError."""
    matches = find_targets(conn, text)
    if not matches:
        raise KeyError(f"nothing named '{text}'")
    if len(matches) > 1:
        raise AmbiguousTarget(text, matches)
    m = matches[0]
    row = conn.execute("SELECT label, data FROM nodes WHERE id = ?", (m.node_id,)).fetchone()
    data = json.loads(row["data"])
    notes = data.pop("notes", "") if isinstance(data.get("notes"), str) else ""
    data.pop("key", None)
    aliases = [a[0] for a in conn.execute("SELECT alias FROM aliases WHERE node_id = ? ORDER BY alias", (m.node_id,))]
    edge_type = {"thread": ("in_thread",), "theme": ("about",)}.get(m.type, ("mentions",))
    family = queries.node_family(conn, m.node_id)
    frags = queries.fragments_linked_to(conn, family, edge_type, limit=500)
    related: dict[str, list[str]] = {}
    if frags:
        src = [f"frag:{f.id}" for f in frags]
        rows = conn.execute(
            f"SELECT n.type, n.label, COUNT(*) c FROM edges e JOIN nodes n ON n.id = e.dst "
            f"WHERE e.src IN ({','.join('?' * len(src))}) AND e.type IN ('in_thread', 'mentions', 'about') "
            f"AND n.id NOT IN ({','.join('?' * len(family))}) GROUP BY n.id ORDER BY c DESC, n.label",
            [*src, *family],
        ).fetchall()
        for t, label, _ in rows:
            related.setdefault(t, []).append(label)
    for r in conn.execute(
        "SELECT e.type, n.label FROM edges e JOIN nodes n ON n.id = e.dst "
        "WHERE e.src = ? AND e.type IN ('member_of', 'part_of', 'merged_into')",
        (m.node_id,),
    ):
        related.setdefault({"member_of": "member of", "part_of": "part of"}.get(r[0], "merged into"), []).append(r[1])
    summary = None
    if m.type == "thread":
        t = queries.thread(conn, m.node_id.split(":", 1)[1])
        summary = t.summary if t else None
    return EntityView(
        id=m.node_id,
        type=m.type,
        name=row["label"],
        aliases=aliases,
        details=data,
        notes=notes,
        summary=summary,
        related=related,
        open_items=[fragment_view(f) for f in frags if f.state == "open" and f.status != "overcome_by_events"],
        recent=[fragment_view(f) for f in frags[-recent:]][::-1],
        fragment_count=len(frags),
    )


def entry_view(conn: sqlite3.Connection, entry_id: str) -> EntryView:
    r = conn.execute(
        "SELECT id, created_at, title, body, key, next_step FROM entries WHERE id = ?", (entry_id,)
    ).fetchone()
    if r is None:
        raise KeyError(f"no entry {entry_id}")
    ids = [x[0] for x in conn.execute("SELECT id FROM fragments WHERE entry_id = ? ORDER BY id", (entry_id,))]
    frags = queries.fragments_by_id(conn, ids)
    return EntryView(
        r["id"],
        r["created_at"][:16],
        r["title"],
        r["body"],
        r["key"],
        r["next_step"] == "done",
        [fragment_view(frags[i]) for i in ids if i in frags],
    )
