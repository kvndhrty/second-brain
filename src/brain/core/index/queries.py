"""Read-only queries over the index, shared by the pipeline and the services."""

from __future__ import annotations

import difflib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from brain.core.index.build import norm_alias
from brain.core.temporal import age, iso_day


@dataclass
class ThreadRow:
    id: str
    name: str
    description: str | None
    state: str
    pinned: bool
    summary: str | None
    summary_through: str | None
    last_activity: str | None
    last_entry: str | None
    summary_as_of: str | None = None  # created_at of the entry the summary runs through
    now_line: str | None = None  # dreaming's one line on where the thread stands
    now_through: str | None = None
    now_as_of: str | None = None
    fragments: int = 0
    open_items: int = 0
    people: list[str] = field(default_factory=list)

    @property
    def summary_stale(self) -> bool:
        return self.last_entry is not None and self.last_entry != self.summary_through


def _thread(r: sqlite3.Row) -> ThreadRow:
    return ThreadRow(
        id=r["id"],
        name=r["name"],
        description=r["description"],
        state=r["state"],
        pinned=bool(r["pinned"]),
        summary=r["summary"],
        summary_through=r["summary_through"],
        last_activity=r["last_activity"],
        last_entry=r["last_entry"],
        summary_as_of=r["summary_as_of"],
        now_line=r["now_line"],
        now_through=r["now_through"],
        now_as_of=r["now_as_of"],
    )


THREAD_SELECT = (
    "SELECT t.*, (SELECT created_at FROM entries WHERE id = t.summary_through) AS summary_as_of, "
    "(SELECT created_at FROM entries WHERE id = t.now_through) AS now_as_of FROM threads t"
)


def threads(
    conn: sqlite3.Connection, *, states: tuple[str, ...] = ("active", "proposed"), limit: int | None = None
) -> list[ThreadRow]:
    sql = (
        f"{THREAD_SELECT} WHERE t.state IN ({','.join('?' * len(states))}) "
        "ORDER BY pinned DESC, last_activity IS NULL, last_activity DESC, name"
    )
    params: list[object] = list(states)
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return [_thread(r) for r in conn.execute(sql, params)]


def thread(conn: sqlite3.Connection, thread_id: str) -> ThreadRow | None:
    r = conn.execute(f"{THREAD_SELECT} WHERE t.id = ?", (thread_id,)).fetchone()
    return _thread(r) if r else None


def node_family(conn: sqlite3.Connection, node_id: str) -> list[str]:
    """The node plus every node merged into it, directly or indirectly (node ids)."""
    rows = conn.execute(
        """
        WITH RECURSIVE fam(id) AS (
          SELECT ? UNION SELECT e.src FROM edges e JOIN fam ON e.dst = fam.id AND e.type = 'merged_into'
        ) SELECT id FROM fam
        """,
        (node_id,),
    ).fetchall()
    return [r[0] for r in rows]


LINK_EDGES = ("in_thread", "mentions", "about")  # fragment -> thread, person or org, theme
LINK_EDGES_SQL = "(" + ", ".join(f"'{e}'" for e in LINK_EDGES) + ")"  # for IN clauses


def edge_for(node_type: str) -> str:
    """The edge type that links a fragment to a node of this type."""
    return {"thread": "in_thread", "theme": "about"}.get(node_type, "mentions")


def thread_nodes(conn: sqlite3.Connection, thread_id: str) -> list[str]:
    """Node ids of the thread plus every thread merged into it."""
    return node_family(conn, f"thread:{thread_id}")


def thread_family(conn: sqlite3.Connection, thread_id: str) -> list[str]:
    """Thread ids: the thread plus every thread merged into it."""
    return [n.split(":", 1)[1] for n in thread_nodes(conn, thread_id)]


def thread_fragments(conn: sqlite3.Connection, thread_id: str, *, limit: int = 150) -> list[FragmentRow]:
    """Fragments in the thread or any thread merged into it, newest `limit`, oldest first."""
    return fragments_linked_to(conn, thread_nodes(conn, thread_id), ("in_thread",), limit=limit)


def node_fragments(conn: sqlite3.Connection, node_id: str, *, limit: int = 150) -> list[FragmentRow]:
    """Fragments linked to a node or any node merged into it, newest `limit`, oldest first."""
    return fragments_linked_to(conn, node_family(conn, node_id), (edge_for(node_id.split(":", 1)[0]),), limit=limit)


def linked_nodes(conn: sqlite3.Connection, fragment_ids: list[str]) -> list[str]:
    """Node ids these fragments link to, once per link (a node linked from three fragments appears three times)."""
    if not fragment_ids:
        return []
    src = [f"frag:{i}" for i in fragment_ids]
    rows = conn.execute(
        f"SELECT dst FROM edges WHERE src IN ({','.join('?' * len(src))}) AND type IN {LINK_EDGES_SQL}", src
    )
    return [r[0] for r in rows]


def active_threads(conn: sqlite3.Connection, *, now: datetime, inactivity_days: int) -> list[ThreadRow]:
    cutoff = (now - timedelta(days=inactivity_days)).isoformat()
    out = []
    for t in threads(conn, states=("active", "proposed")):
        if not (t.pinned or (t.last_activity and t.last_activity >= cutoff)):
            continue
        nodes = thread_nodes(conn, t.id)
        ph = ",".join("?" * len(nodes))
        t.fragments, t.open_items = conn.execute(
            f"""
            SELECT COUNT(DISTINCT f.id), COUNT(DISTINCT CASE
              WHEN f.state = 'open' AND f.status != 'overcome_by_events' AND f.someday = 0 THEN f.id END)
            FROM edges e JOIN fragments f ON e.src = 'frag:' || f.id
            WHERE e.type = 'in_thread' AND e.dst IN ({ph})
            """,
            nodes,
        ).fetchone()
        t.people = [
            r[0]
            for r in conn.execute(
                f"""
            SELECT n.label, COUNT(*) c FROM edges e
            JOIN edges m ON m.src = e.src AND m.type = 'mentions'
            JOIN nodes n ON n.id = m.dst AND n.type = 'person'
            WHERE e.type = 'in_thread' AND e.dst IN ({ph})
            GROUP BY n.label ORDER BY c DESC, n.label LIMIT 3
            """,
                nodes,
            )
        ]
        out.append(t)
    return out


def stale_threads(conn: sqlite3.Connection) -> list[ThreadRow]:
    return [t for t in threads(conn, states=("active", "proposed", "archived", "obe")) if t.summary_stale]


def pending_entries(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """(entry_id, key, next_step) for entries the pipeline has not finished, oldest first."""
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT id, key, next_step FROM entries WHERE next_step != 'done' ORDER BY created_at, id"
        )
    ]


def entry_key(conn: sqlite3.Connection, entry_id: str) -> str | None:
    r = conn.execute("SELECT key FROM entries WHERE id = ?", (entry_id,)).fetchone()
    return r[0] if r else None


def entity_names(conn: sqlite3.Connection, etype: str, *, limit: int = 150) -> list[tuple[str, str, list[str]]]:
    """(id, name, aliases) for entities of a type, most recently mentioned first."""
    rows = conn.execute(
        """
        SELECT n.id, n.label, MAX(f.created_at) last
        FROM nodes n
        LEFT JOIN edges e ON e.dst = n.id AND e.type IN ('mentions', 'about')
        LEFT JOIN fragments f ON e.src = 'frag:' || f.id
        WHERE n.type = ? GROUP BY n.id ORDER BY last IS NULL, last DESC, n.label LIMIT ?
        """,
        (etype, limit),
    ).fetchall()
    out = []
    for nid, label, _ in rows:
        aliases = [
            r[0]
            for r in conn.execute("SELECT alias FROM aliases WHERE node_id = ?", (nid,))
            if r[0] != norm_alias(label)
        ]
        out.append((nid.split(":", 1)[1], label, aliases))
    return out


@dataclass(frozen=True)
class Candidate:
    entity_id: str
    name: str
    score: float


def alias_matches(conn: sqlite3.Connection, etype: str, surface: str) -> list[str]:
    return [
        r[0].split(":", 1)[1]
        for r in conn.execute(
            "SELECT DISTINCT a.node_id FROM aliases a JOIN nodes n ON n.id = a.node_id "
            "WHERE a.alias = ? AND n.type = ? ORDER BY a.node_id",
            (norm_alias(surface), etype),
        )
    ]


def fuzzy_candidates(conn: sqlite3.Connection, etype: str, surface: str, *, limit: int = 5) -> list[Candidate]:
    """Name-based candidates: exact alias, close spelling, and (for people) first/last-name matches."""
    target = norm_alias(surface)
    target_tokens = target.split()
    scores: dict[str, tuple[float, str]] = {}
    rows = conn.execute(
        "SELECT a.alias, a.node_id, n.label FROM aliases a JOIN nodes n ON n.id = a.node_id WHERE n.type = ?",
        (etype,),
    ).fetchall()
    for alias, nid, label in rows:
        score = 1.0 if alias == target else difflib.SequenceMatcher(None, alias, target).ratio()
        tokens = alias.split()
        if etype == "person" and len(target_tokens) == 1 and len(tokens) > 1 and target_tokens[0] in tokens:
            score = max(score, 0.7)  # "Mike" vs "Mike Chen"
        if etype == "org" and target.replace(" ", "") == "".join(t[0] for t in tokens):
            score = max(score, 0.75)  # acronym
        eid = nid.split(":", 1)[1]
        if score > scores.get(eid, (0.0, ""))[0]:
            scores[eid] = (score, label)
    ranked = sorted(((s, eid, label) for eid, (s, label) in scores.items() if s >= 0.6), reverse=True)
    return [Candidate(eid, label, round(s, 3)) for s, eid, label in ranked[:limit]]


def pending_names(conn: sqlite3.Connection, *, limit: int = 50) -> list[str]:
    """Names of people and orgs proposed as new and still in review, most recent first."""
    rows = conn.execute(
        "SELECT payload FROM review_items WHERE kind IN ('new_person', 'new_org') ORDER BY created_at DESC, id"
    ).fetchall()
    names: list[str] = []
    for (payload,) in rows:
        p = json.loads(payload)
        name = f"{p['surface']} ({p['type']})"
        if name not in names:
            names.append(name)
    return names[:limit]


def last_mention(conn: sqlite3.Connection, node_id: str) -> str | None:
    """created_at of the newest fragment that mentions or is about this node."""
    r = conn.execute(
        "SELECT MAX(f.created_at) FROM edges e JOIN fragments f ON e.src = 'frag:' || f.id "
        "WHERE e.dst = ? AND e.type IN ('mentions', 'about')",
        (node_id,),
    ).fetchone()
    return r[0] if r else None


def entity_profile(conn: sqlite3.Connection, node_id: str, *, today: date, recent: int = 3) -> str:
    """One-line description of an entity for prompts: details and recent mentions, newest first, dated."""
    r = conn.execute("SELECT label, data FROM nodes WHERE id = ?", (node_id,)).fetchone()
    if r is None:
        return node_id
    data = json.loads(r["data"])
    parts = [f"name: {r['label']}"]
    aliases = [a[0] for a in conn.execute("SELECT alias FROM aliases WHERE node_id = ?", (node_id,))]
    if aliases:
        parts.append("aliases: " + ", ".join(aliases))
    for key in ("role", "kind", "description"):
        if data.get(key):
            parts.append(f"{key}: {data[key]}")
    orgs = [
        o[0]
        for o in conn.execute(
            "SELECT n.label FROM edges e JOIN nodes n ON n.id = e.dst WHERE e.src = ? AND e.type = 'member_of' "
            "AND e.valid_to IS NULL",
            (node_id,),
        )
    ]
    if orgs:
        parts.append("org: " + ", ".join(orgs))
    if data.get("works_on"):
        parts.append("works on: " + ", ".join(data["works_on"]))
    mentions = [
        f"{iso_day(m[1])} ({age(m[1], today)}): {m[0]}"
        for m in conn.execute(
            "SELECT f.text, f.created_at FROM edges e JOIN fragments f ON e.src = 'frag:' || f.id WHERE e.dst = ? "
            "ORDER BY f.created_at DESC LIMIT ?",
            (node_id, recent),
        )
    ]
    if mentions:
        parts.append("recently: " + " / ".join(mentions))
    return "; ".join(parts)


@dataclass
class FragmentRow:
    id: str
    entry_id: str
    kind: str
    text: str
    status: str
    state: str | None
    someday: bool
    obe_date: str | None
    obe_reason: str | None
    created_at: str

    @property
    def is_open(self) -> bool:
        """An open question or action item that has not been overcome by events."""
        return self.state == "open" and self.status != "overcome_by_events"


def _frag(r: sqlite3.Row) -> FragmentRow:
    return FragmentRow(
        r["id"],
        r["entry_id"],
        r["kind"],
        r["text"],
        r["status"],
        r["state"],
        bool(r["someday"]),
        r["obe_date"],
        r["obe_reason"],
        r["created_at"],
    )


FRAGMENT_COLS = "f.id, f.entry_id, f.kind, f.text, f.status, f.state, f.someday, f.obe_date, f.obe_reason, f.created_at"


def fragments_linked_to(
    conn: sqlite3.Connection, node_ids: list[str], edge_types: tuple[str, ...], *, limit: int = 150
) -> list[FragmentRow]:
    """Fragments with an edge of these types to any of these nodes, newest `limit`, returned oldest first."""
    if not node_ids:
        return []
    rows = conn.execute(
        f"""
        SELECT DISTINCT {FRAGMENT_COLS} FROM edges e JOIN fragments f ON e.src = 'frag:' || f.id
        WHERE e.dst IN ({",".join("?" * len(node_ids))}) AND e.type IN ({",".join("?" * len(edge_types))})
        ORDER BY f.created_at DESC, f.id DESC LIMIT ?
        """,
        [*node_ids, *edge_types, limit],
    ).fetchall()
    return list(reversed([_frag(r) for r in rows]))


# SQL condition, for fragments aliased `f`: the fragment is in an archived or OBE thread.
IN_RETIRED_THREAD = (
    "EXISTS (SELECT 1 FROM edges e JOIN threads t ON e.dst = 'thread:' || t.id "
    "WHERE e.src = 'frag:' || f.id AND e.type = 'in_thread' AND t.state IN ('archived', 'obe'))"
)


def select_fragments(conn: sqlite3.Connection, where: str, params: tuple | list = ()) -> list[FragmentRow]:
    """Fragments matching a WHERE clause (and any ORDER BY or LIMIT after it), with the table aliased `f`."""
    return [_frag(r) for r in conn.execute(f"SELECT {FRAGMENT_COLS} FROM fragments f WHERE {where}", params)]


def fragments_by_id(conn: sqlite3.Connection, ids: list[str]) -> dict[str, FragmentRow]:
    if not ids:
        return {}
    rows = conn.execute(f"SELECT {FRAGMENT_COLS} FROM fragments f WHERE f.id IN ({','.join('?' * len(ids))})", ids)
    return {r["id"]: _frag(r) for r in rows}


def open_items(conn: sqlite3.Connection, *, exclude_entry: str | None = None) -> dict[str, FragmentRow]:
    rows = conn.execute(
        f"SELECT {FRAGMENT_COLS} FROM fragments f WHERE f.state = 'open' AND f.status != 'overcome_by_events' "
        "AND f.entry_id != ? ORDER BY f.created_at",
        (exclude_entry or "",),
    )
    return {r["id"]: _frag(r) for r in rows}


def entry_dates(conn: sqlite3.Connection, ids: list[str]) -> dict[str, tuple[str, str]]:
    """entry id -> (created_at, label)"""
    if not ids:
        return {}
    rows = conn.execute(
        f"SELECT e.id, e.created_at, n.label FROM entries e JOIN nodes n ON n.id = 'entry:' || e.id "
        f"WHERE e.id IN ({','.join('?' * len(ids))})",
        ids,
    )
    return {r[0]: (r[1], r[2]) for r in rows}


def neighbors(conn: sqlite3.Connection, node_id: str, *, depth: int = 2) -> list[str]:
    """Node ids within `depth` hops, ignoring entry->fragment structure edges."""
    rows = conn.execute(
        """
        WITH RECURSIVE walk(id, d) AS (
          SELECT ?, 0
          UNION
          SELECT CASE WHEN e.src = w.id THEN e.dst ELSE e.src END, w.d + 1
          FROM edges e JOIN walk w ON w.id IN (e.src, e.dst)
          WHERE w.d < ? AND e.type != 'contains'
        ) SELECT DISTINCT id FROM walk WHERE id != ?
        """,
        (node_id, depth, node_id),
    ).fetchall()
    return [r[0] for r in rows]
