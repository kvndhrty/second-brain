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


# --- lists and graph for the web UI --------------------------------------------


@dataclass
class EntitySummary:
    id: str  # node id
    type: str
    name: str
    state: str | None
    fragments: int
    last_seen: str | None
    detail: str | None  # role for people, kind for themes, description for threads


def entity_list(conn: sqlite3.Connection, etype: str) -> list[EntitySummary]:
    edge = {"thread": "in_thread", "theme": "about"}.get(etype, "mentions")
    rows = conn.execute(
        """
        SELECT n.id, n.label, n.data, COUNT(DISTINCT f.id), MAX(f.created_at)
        FROM nodes n
        LEFT JOIN edges e ON e.dst = n.id AND e.type = ?
        LEFT JOIN fragments f ON e.src = 'frag:' || f.id
        WHERE n.type = ? GROUP BY n.id ORDER BY MAX(f.created_at) IS NULL, MAX(f.created_at) DESC, n.label
        """,
        (edge, etype),
    ).fetchall()
    out = []
    for nid, label, data, count, last in rows:
        d = json.loads(data)
        detail = d.get("role") or d.get("kind") or d.get("description")
        out.append(EntitySummary(nid, etype, label, d.get("state"), count, last[:10] if last else None, detail))
    return out


@dataclass
class EntrySummary:
    id: str
    date: str
    title: str | None
    preview: str
    processed: bool
    fragments: int


def recent_entries(conn: sqlite3.Connection, *, limit: int = 50, before: str | None = None) -> list[EntrySummary]:
    rows = conn.execute(
        """
        SELECT e.id, e.created_at, e.title, e.body, e.next_step,
               (SELECT COUNT(*) FROM fragments f WHERE f.entry_id = e.id)
        FROM entries e WHERE e.created_at < ? ORDER BY e.created_at DESC, e.id DESC LIMIT ?
        """,
        (before or "9999", limit),
    ).fetchall()
    return [EntrySummary(r[0], r[1][:16], r[2], _snippet(r[3]), r[4] == "done", r[5]) for r in rows]


def dreams(conn: sqlite3.Connection) -> list[FragmentView]:
    rows = conn.execute(
        f"SELECT {queries.FRAGMENT_COLS} FROM fragments f WHERE f.dream = 1 ORDER BY f.created_at DESC, f.id DESC"
    ).fetchall()
    return [fragment_view(queries._frag(r)) for r in rows]


@dataclass
class Link:
    id: str
    type: str
    label: str


@dataclass
class TimelineFragment:
    fragment: FragmentView
    links: list[Link]


def node_timeline(conn: sqlite3.Connection, node_id: str) -> list[TimelineFragment]:
    """Every fragment linked to a node (and nodes merged into it), newest first, with its other links."""
    ntype = node_id.split(":", 1)[0]
    edge = {"thread": ("in_thread",), "theme": ("about",)}.get(ntype, ("mentions",))
    family = queries.node_family(conn, node_id)
    frags = queries.fragments_linked_to(conn, family, edge, limit=2000)
    links: dict[str, list[Link]] = {}
    if frags:
        src = [f"frag:{f.id}" for f in frags]
        for fid, nid, t, label in conn.execute(
            f"SELECT substr(e.src, 6), n.id, n.type, n.label FROM edges e JOIN nodes n ON n.id = e.dst "
            f"WHERE e.src IN ({','.join('?' * len(src))}) AND e.type IN ('in_thread', 'mentions', 'about') "
            "ORDER BY n.type, n.label",
            src,
        ):
            if nid not in family:
                links.setdefault(fid, []).append(Link(nid, t, label))
    return [TimelineFragment(fragment_view(f), links.get(f.id, [])) for f in reversed(frags)]


@dataclass
class GraphNode:
    id: str
    type: str
    label: str
    weight: int  # number of fragments linked to it


@dataclass
class GraphEdge:
    source: str
    target: str
    weight: int  # fragments linking both, or 1 for a membership edge
    type: str  # co_occurs | member_of | part_of


@dataclass
class Graph:
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    truncated: bool


def entity_graph(conn: sqlite3.Connection, *, center: str | None = None, depth: int = 2, limit: int = 250) -> Graph:
    """People, orgs, themes, and threads; two are joined when fragments link to both.

    This is where cross-thread patterns show up: a technique theme linked to
    two threads means the same approach was used in both.
    """
    weights = dict(
        conn.execute(
            "SELECT e.dst, COUNT(DISTINCT e.src) FROM edges e JOIN nodes n ON n.id = e.dst "
            "WHERE e.type IN ('in_thread', 'mentions', 'about') AND n.type IN ('person', 'org', 'theme', 'thread') "
            "GROUP BY e.dst"
        ).fetchall()
    )
    labels = {
        r[0]: (r[1], r[2])
        for r in conn.execute(
            "SELECT id, type, label FROM nodes WHERE type IN ('person', 'org', 'theme', 'thread')"
        ).fetchall()
    }
    merged = {r[0] for r in conn.execute("SELECT src FROM edges WHERE type = 'merged_into'")}
    pairs: dict[tuple[str, str], int] = {}
    for a, b, n in conn.execute(
        """
        SELECT e1.dst, e2.dst, COUNT(DISTINCT e1.src) FROM edges e1
        JOIN edges e2 ON e2.src = e1.src AND e2.dst > e1.dst
        WHERE e1.type IN ('in_thread', 'mentions', 'about') AND e2.type IN ('in_thread', 'mentions', 'about')
        GROUP BY e1.dst, e2.dst
        """
    ):
        if a in labels and b in labels and a not in merged and b not in merged:
            pairs[(a, b)] = n
    edges = [GraphEdge(a, b, n, "co_occurs") for (a, b), n in pairs.items()]
    for src, dst, t in conn.execute("SELECT src, dst, type FROM edges WHERE type IN ('member_of', 'part_of')"):
        if src in labels and dst in labels:
            edges.append(GraphEdge(src, dst, 1, t))

    keep = {n for n in labels if n not in merged}
    if center:
        adj: dict[str, set[str]] = {}
        for e in edges:
            adj.setdefault(e.source, set()).add(e.target)
            adj.setdefault(e.target, set()).add(e.source)
        frontier, seen = {center}, {center}
        for _ in range(depth):
            frontier = {m for n in frontier for m in adj.get(n, set())} - seen
            seen |= frontier
        keep &= seen
    ranked = sorted(keep, key=lambda n: (n != center, -weights.get(n, 0), labels[n][1]))
    truncated = len(ranked) > limit
    keep = set(ranked[:limit])
    nodes = [GraphNode(n, labels[n][0], labels[n][1], weights.get(n, 0)) for n in ranked[:limit]]
    return Graph(nodes, [e for e in edges if e.source in keep and e.target in keep], truncated)
