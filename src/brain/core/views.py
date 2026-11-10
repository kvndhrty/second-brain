"""Read models for front-ends and for the wiki files: pages, notes, search hits, the change log.

These are plain data so the MCP server and the web UI can serialize them
directly, and the wiki renderer can print them. They read only from the index.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from brain.core.formats.records import ENTITY_TYPES, PageFile
from brain.core.index import queries, search
from brain.core.index.db import get_meta
from brain.core.index.queries import Ent, Stmt, World
from brain.core.llm.models import LLM
from brain.core.temporal import age, iso_day, utc_iso

SNIPPET_CHARS = 280
HISTORY_LIMIT = 150
RRF_K = 60


@dataclass
class Line:
    """One statement as shown on a page."""

    id: str
    note_id: str
    date: str
    age: str
    kind: str
    text: str
    status: str
    replaced_by: str | None = None  # text of the statement that superseded or answered it
    thread: str | None = None
    thread_name: str | None = None


@dataclass
class CitedText:
    text: str
    citations: list[str]


@dataclass
class Ref:
    id: str
    type: str
    name: str


@dataclass
class PageData:
    id: str
    type: str
    name: str
    names: list[str]
    status: str
    profile: dict[str, str]
    as_of: str | None  # date of the newest note it covers
    last_activity: str | None
    closed_at: str | None
    now: CitedText | None
    summary: list[CitedText]
    open: list[Line]
    decided: list[Line]
    history: list[Line]
    related: list[Ref]
    note_count: int
    log: list[queries.LogEvent] = field(default_factory=list)


@dataclass
class EntitySummary:
    id: str
    type: str
    name: str
    status: str
    now: str | None
    last_activity: str | None
    open_items: int
    note_count: int


@dataclass
class NoteSummary:
    id: str
    date: str
    label: str
    done: bool
    statements: int


@dataclass
class NoteView:
    id: str
    created: str
    title: str | None
    about: str | None
    body: str
    done: bool
    statements: list[Line]
    entities: list[Ref]
    events: list[queries.LogEvent]


@dataclass
class Hit:
    id: str
    type: str  # note | statement | thread | person | org | topic
    label: str
    snippet: str
    date: str | None = None
    note_id: str | None = None
    kind: str | None = None
    status: str | None = None


class NoSuchPage(LookupError):
    pass


def line(world: World, s: Stmt, today: date, texts: dict[str, Stmt] | None = None) -> Line:
    by = (texts or {}).get(s.by or "")
    thread = world.entities.get(s.thread or "")
    return Line(
        s.id,
        s.note_id,
        iso_day(s.created_at),
        age(s.created_at, today),
        s.kind,
        s.text,
        s.status,
        by.text if by else None,
        thread.id if thread else None,
        thread.name if thread else None,
    )


def lines(world: World, stmts: list[Stmt], today: date) -> list[Line]:
    texts = world.by_id([s.by for s in stmts if s.by])
    return [line(world, s, today, texts) for s in stmts]


def _ref(e: Ent) -> Ref:
    return Ref(e.id, e.type, e.name)


def _page_file(conn: sqlite3.Connection, entity_id: str) -> PageFile | None:
    row = conn.execute("SELECT data FROM pages WHERE entity_id = ?", (entity_id,)).fetchone()
    return PageFile.model_validate_json(row[0]) if row else None


def resolve(world: World, text: str) -> Ent:
    """The surviving entity with this id or exactly this name."""
    e = world.get(text)
    if e is None:
        found = world.by_name(text)
        # A thread and a topic may share a name; the thread is the page people mean.
        found = [x for x in found if x.type == "thread"] or found
        if len(found) != 1:
            raise NoSuchPage(f"no page for {text!r}" if not found else f"{text!r} matches several pages")
        e = found[0]
    return e


def page(conn: sqlite3.Connection, entity: str, *, now: datetime, world: World | None = None) -> PageData:
    world = world or World(conn)
    e = resolve(world, entity)
    today = now.astimezone().date()
    stmts = [s for s in world.of_entity(e.id) if s.status != "retracted"]
    pf = _page_file(conn, e.id)
    sections = pf.sections if pf else {}
    now_s = sections.get("now")
    summary = sections.get("summary")
    closed = e.status == "closed"
    visible = [s for s in stmts if s.status != "repeat"]
    rec = e.record
    return PageData(
        id=e.id,
        type=e.type,
        name=e.name,
        names=[n for n in world.names_of(e.id) if n != e.name],
        status=e.status,
        profile=dict(rec.profile),
        as_of=iso_day(stmts[-1].created_at) if stmts else None,
        last_activity=world.last_activity(e.id),
        closed_at=iso_day(rec.closed_at) if rec.closed_at else None,
        now=CitedText(now_s.text, now_s.sentences[0].citations) if now_s and now_s.sentences and not closed else None,
        summary=[CitedText(s.text, s.citations) for s in summary.sentences] if summary else [],
        open=lines(world, [s for s in visible if s.is_open and not closed], today),
        decided=lines(world, [s for s in reversed(visible) if s.kind == "decision" and s.is_current][:30], today),
        history=lines(world, list(reversed(visible))[:HISTORY_LIMIT], today),
        related=[_ref(r) for r in world.related(e.id)],
        note_count=world.note_count(e.id),
        log=queries.events(conn, limit=20, entity=world.members(e.id)),
    )


def entity_list(
    conn: sqlite3.Connection, etype: str | None = None, *, world: World | None = None
) -> list[EntitySummary]:
    world = world or World(conn)
    nows = dict(conn.execute("SELECT entity_id, now FROM pages").fetchall())
    open_by_thread: dict[str, int] = {}
    for s in world.open_items():
        if s.thread:
            open_by_thread[s.thread] = open_by_thread.get(s.thread, 0) + 1
    out = []
    for e in world.roots(etype, with_provisional=False):
        out.append(
            EntitySummary(
                e.id,
                e.type,
                e.name,
                e.status,
                nows.get(e.id) if e.status != "closed" else None,
                world.last_activity(e.id),
                open_by_thread.get(e.id, 0),
                world.note_count(e.id),
            )
        )
    return sorted(out, key=lambda s: (s.last_activity or "", s.name), reverse=True)


def active_threads(conn: sqlite3.Connection, *, now: datetime, inactivity_days: int) -> list[EntitySummary]:
    """Threads that are not closed and had activity recently, or still have open items."""
    cutoff = utc_iso(now - timedelta(days=inactivity_days))
    return [
        t
        for t in entity_list(conn, "thread")
        if t.status == "active" and ((t.last_activity or "") >= cutoff or t.open_items)
    ]


def recent_notes(conn: sqlite3.Connection, *, limit: int = 50, before: str | None = None) -> list[NoteSummary]:
    rows = conn.execute(
        "SELECT n.id, n.created_at, COALESCE(n.title, n.body), n.done, "
        "(SELECT COUNT(*) FROM statements s WHERE s.note_id = n.id) FROM notes n "
        "WHERE (? IS NULL OR n.created_at < ?) ORDER BY n.created_at DESC, n.id DESC LIMIT ?",
        (before, before, limit),
    ).fetchall()
    out = []
    for r in rows:
        first = next((ln.strip() for ln in r[2].splitlines() if ln.strip()), r[0])
        out.append(NoteSummary(r[0], r[1], first[:120], bool(r[3]), r[4]))
    return out


def note(conn: sqlite3.Connection, note_id: str, *, now: datetime) -> NoteView:
    row = conn.execute("SELECT id, created_at, title, about, body, done FROM notes WHERE id = ?", (note_id,)).fetchone()
    if row is None:
        raise NoSuchPage(f"no note {note_id}")
    world = World(conn)
    stmts = world.of_note(note_id)
    ents: dict[str, Ent] = {}
    for (eid,) in conn.execute("SELECT DISTINCT entity_id FROM mentions WHERE note_id = ?", (note_id,)):
        e = world.get(eid)
        if e:
            ents[e.id] = e
    for s in stmts:
        if s.thread and s.thread in world.entities:
            ents[s.thread] = world.entities[s.thread]
    evs = [
        queries.LogEvent(*r)
        for r in conn.execute(
            "SELECT at, source, kind, text, entity_id FROM events WHERE source = ? ORDER BY seq", (note_id,)
        )
    ]
    return NoteView(
        row[0],
        row[1],
        row[2],
        row[3],
        row[4],
        bool(row[5]),
        lines(world, stmts, now.astimezone().date()),
        [_ref(e) for e in sorted(ents.values(), key=lambda e: (ENTITY_TYPES.index(e.type), e.name))],  # type: ignore[arg-type]
        evs,
    )


def _snippet(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= SNIPPET_CHARS else text[: SNIPPET_CHARS - 1] + "…"


def rrf(*rankings: list[search.Hit]) -> list[tuple[str, str]]:
    """Reciprocal rank fusion of keyword and vector results: (item id, item type), best first."""
    scores: dict[tuple[str, str], float] = {}
    for ranking in rankings:
        for rank, hit in enumerate(ranking):
            key = (hit.item_id, hit.item_type)
            scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank + 1)
    return sorted(scores, key=lambda k: (-scores[k], k))


def retrieve(conn: sqlite3.Connection, llm: LLM | None, text: str, *, limit: int) -> list[tuple[str, str]]:
    kw = search.keyword(conn, text, limit=limit * 2)
    vec: list[search.Hit] = []
    if llm is not None and llm.embed_model and get_meta(conn, "embed_dim") is not None:
        vec = search.nearest(conn, llm.embed([text])[0], k=limit * 2)
    return rrf(kw, vec)[: limit * 2]


def search_all(
    conn: sqlite3.Connection, llm: LLM | None, query: str, *, limit: int = 10, types: tuple[str, ...] | None = None
) -> list[Hit]:
    world = World(conn)
    hits: list[Hit] = []
    seen: set[str] = set()
    for e in [*world.by_name(query), *world.similar(query, limit=5, floor=0.7)]:
        if e.id not in seen and e.status != "provisional":
            seen.add(e.id)
            hits.append(Hit(e.id, e.type, e.name, "; ".join(world.names_of(e.id)[1:4])))
    ranked = retrieve(conn, llm, query, limit=limit)
    stmts = world.by_id([i for i, t in ranked if t == "statement"])
    dates = queries.note_dates(conn, [i for i, t in ranked if t == "note"])
    for item_id, item_type in ranked:
        if item_type == "statement" and item_id in stmts:
            s = stmts[item_id]
            if s.status == "retracted":
                continue
            hits.append(
                Hit(s.id, "statement", s.kind, _snippet(s.text), iso_day(s.created_at), s.note_id, s.kind, s.status)
            )
        elif item_type == "note" and item_id in dates:
            created, label = dates[item_id]
            hits.append(Hit(item_id, "note", label, "", iso_day(created), item_id))
        elif item_type == "entity":
            e = world.get(item_id)
            if e and e.id not in seen and e.status != "provisional":
                seen.add(e.id)
                hits.append(Hit(e.id, e.type, e.name, "; ".join(world.names_of(e.id)[1:4])))
    if types:
        hits = [h for h in hits if h.type in types]
    return hits[:limit]
