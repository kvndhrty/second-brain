"""`ask` and `brief`: retrieve from the index, write an answer with citations.

Every claim the model returns must cite note ids that were in its input; an
output validator rejects anything else and makes the model retry. Retracted
statements are never shown to the model; superseded ones are shown marked, so
the answer can say what changed.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from brain.core import keys, views
from brain.core.formats.records import BriefState, PageFile
from brain.core.index import queries
from brain.core.index.queries import CONTENT_KINDS, Ent, Stmt, World
from brain.core.llm.models import LLM
from brain.core.llm.schemas import AskOutput, BriefOutput, BriefRoute, Claim
from brain.core.pipeline.text import attr, candidate_el, check_citations, stmt_el
from brain.core.storage.lease import LeaseError
from brain.core.temporal import as_of, iso_day, local_date, today_el, utc_iso
from brain.core.vault import Vault

ASK_STATEMENTS = 24
ASK_PAGES = 3
BRIEF_STATEMENTS = 80
ROUTE_CANDIDATES = 12
ROUTE_MAX_TOPICS = 4


@dataclass(frozen=True)
class Source:
    note_id: str
    date: str
    label: str


@dataclass
class Answer:
    question: str
    claims: list[Claim]
    insufficient: bool
    sources: dict[str, Source]


class AmbiguousTarget(Exception):
    def __init__(self, text: str, matches: list[Ent], *, question: str | None = None) -> None:
        self.text = text
        self.matches = matches
        self.question = question
        listed = ", ".join(f"{m.id} ({m.type}: {m.name})" for m in matches)
        super().__init__(f"{question or f'{text!r} matches several things.'} Candidates: {listed}. Use one of the ids.")


@dataclass
class Brief:
    request: str
    topics: list[views.Ref]
    since: datetime | None
    output: BriefOutput
    sources: dict[str, Source]
    recorded: bool = False
    routed: bool = False
    statuses: dict[str, str] = field(default_factory=dict)  # topic id -> status


def _sources(conn: sqlite3.Connection, note_ids: set[str]) -> dict[str, Source]:
    return {n: Source(n, iso_day(d), label) for n, (d, label) in queries.note_dates(conn, note_ids).items()}


def _summary_el(conn: sqlite3.Connection, e: Ent, today) -> tuple[str, set[str]] | None:
    row = conn.execute("SELECT data FROM pages WHERE entity_id = ?", (e.id,)).fetchone()
    section = PageFile.model_validate_json(row[0]).sections.get("summary") if row else None
    if section is None or not section.sentences:
        return None
    cites = {c for s in section.sentences for c in s.citations}
    lines = [f"{s.text} [{', '.join(s.citations)}]" for s in section.sentences]
    head = f'<page id="{attr(e.id)}" type="{e.type}" name="{attr(e.name)}" status="{e.status}" {as_of(section.through, today)}>'
    return "\n".join([head, *lines, "</page>"]), cites


def _statements_for(conn: sqlite3.Connection, world: World, llm: LLM, text: str, *, limit: int) -> list[Stmt]:
    ranked = views.retrieve(conn, llm if llm.configured else None, text, limit=limit)
    out: dict[str, Stmt] = {}
    found = world.by_id([i for i, t in ranked if t == "statement"])
    for item_id, item_type in ranked:
        if item_type == "statement" and item_id in found:
            out[item_id] = found[item_id]
        elif item_type == "note":
            for s in world.of_note(item_id):
                out.setdefault(s.id, s)
    keep = [s for s in out.values() if s.status not in ("retracted", "repeat") and s.kind in CONTENT_KINDS]
    return keep[:limit]


def ask(conn: sqlite3.Connection, llm: LLM, question: str, *, now: datetime) -> Answer:
    world = World(conn)
    today = local_date(now)
    stmts = _statements_for(conn, world, llm, question, limit=ASK_STATEMENTS)
    allowed = {s.note_id for s in stmts}
    lines = [today_el(now), f"<question>{question}</question>"]
    threads: dict[str, int] = {}
    for s in stmts:
        if s.thread:
            threads[s.thread] = threads.get(s.thread, 0) + 1
    for tid in sorted(threads, key=lambda t: (-threads[t], t))[:ASK_PAGES]:
        el = _summary_el(conn, world.entities[tid], today)
        if el:
            lines.append(el[0])
            allowed |= el[1]
    lines += [stmt_el(s, today, with_id=False) for s in sorted(stmts, key=lambda s: (s.created_at, s.id))]
    if not stmts:
        return Answer(question, [], True, {})

    def validate(out: AskOutput) -> None:
        for c in out.claims:
            if not c.citations:
                raise ValueError("every claim needs at least one citation")
            check_citations(c.citations, allowed)

    out = llm.run("ask", "\n".join(lines), AskOutput, validate=validate).output
    cited = {c for claim in out.claims for c in claim.citations}
    return Answer(question, out.claims, out.insufficient, _sources(conn, cited))


# --- brief ---------------------------------------------------------------------------


def find_targets(world: World, text: str) -> list[Ent]:
    """Entities named exactly by the text: an id, or a name. A thread wins over a topic of the same name."""
    text = text.strip()
    e = world.get(text)
    if e is not None:
        return [e]
    found = [e for e in world.by_name(text) if e.status != "provisional"]
    threads = [e for e in found if e.type == "thread"]
    if threads and all(e.type in ("thread", "topic") for e in found):
        return threads[:1] if len(threads) == 1 else threads
    return found


def _route_candidates(conn: sqlite3.Connection, world: World, llm: LLM, text: str, named: list[Ent]) -> list[Ent]:
    out: dict[str, Ent] = {e.id: e for e in named}
    for e in world.similar(text, limit=6, floor=0.6):
        if e.status != "provisional":
            out.setdefault(e.id, e)
    low = text.casefold()
    for e in world.roots(with_provisional=False):
        if any(len(n) > 2 and n.casefold() in low for n in world.names_of(e.id)):
            out.setdefault(e.id, e)
    stmts = _statements_for(conn, world, llm, text, limit=30)
    counts: dict[str, int] = {}
    for s in stmts:
        if s.thread:
            counts[s.thread] = counts.get(s.thread, 0) + 1
    for ents in world.entities_of([s.id for s in stmts]).values():
        for eid in ents:
            counts[eid] = counts.get(eid, 0) + 1
    for eid in sorted(counts, key=lambda i: (-counts[i], i)):
        e = world.entities[eid]
        if e.status != "provisional":
            out.setdefault(e.id, e)
    return list(out.values())[:ROUTE_CANDIDATES]


def choose_topics(
    conn: sqlite3.Connection, world: World, llm: LLM, text: str, *, now: datetime
) -> tuple[list[Ent], bool]:
    """(what the brief covers, whether the model chose). One exact match needs no call."""
    named = find_targets(world, text)
    if len(named) == 1:
        return named, False
    cands = _route_candidates(conn, world, llm, text, named)
    if not cands:
        return [], False
    today = local_date(now)
    ids = {c.id for c in cands}

    def validate(out: BriefRoute) -> None:
        bad = [t for t in out.targets if t not in ids]
        if bad:
            raise ValueError(f"targets must be candidate ids; not candidates: {', '.join(bad)}")
        if len(out.targets) > ROUTE_MAX_TOPICS:
            raise ValueError(f"at most {ROUTE_MAX_TOPICS} targets")
        if out.clarify and out.targets:
            raise ValueError("return either targets or clarify, not both")

    user = "\n".join([today_el(now), f"<request>{text}</request>", *[candidate_el(world, c, today) for c in cands]])
    out = llm.run("brief_route", user, BriefRoute, validate=validate).output
    if out.clarify and not out.targets:
        raise AmbiguousTarget(text, named or cands[:4], question=out.clarify)
    return [world.entities[t] for t in dict.fromkeys(out.targets)], True


def _brief_state(vault: Vault) -> BriefState:
    found = vault.read_record(keys.BRIEF_STATE, BriefState)
    return found[0] if found else BriefState()


def brief(
    conn: sqlite3.Connection,
    llm: LLM,
    vault: Vault,
    text: str,
    *,
    now: datetime,
    since: datetime | None = None,
    record: bool = True,
) -> Brief:
    world = World(conn)
    today = local_date(now)
    topics, routed = choose_topics(conn, world, llm, text, now=now)
    state = _brief_state(vault)
    if since is None and topics:
        seen = [state.last[t.id] for t in topics if t.id in state.last]
        since = min(seen) if len(seen) == len(topics) else None
    lines = [today_el(now)]
    if routed or not topics:
        lines.append(f"<request>{text}</request>")
    lines.append(f'<last_looked date="{iso_day(since)}"/>' if since else '<last_looked date="never"/>')
    allowed: set[str] = set()

    def show(stmts: list[Stmt]) -> None:
        for s in stmts:
            new = ' new="yes"' if since and s.created_at > utc_iso(since) else ""
            lines.append(stmt_el(s, today, with_id=False, extra=new))
            allowed.add(s.note_id)

    for t in topics:
        lines.append(f'<topic id="{attr(t.id)}" type="{t.type}" name="{attr(t.name)}" status="{t.status}">')
        el = _summary_el(conn, t, today)
        if el:
            lines.append(el[0])
            allowed.update(el[1])
        show([s for s in world.of_entity(t.id) if s.status not in ("retracted", "repeat")][-BRIEF_STATEMENTS:])
        lines.append("</topic>")
    if not topics:
        show(sorted(_statements_for(conn, world, llm, text, limit=40), key=lambda s: (s.created_at, s.id)))
    refs = [views.Ref(t.id, t.type, t.name) for t in topics]
    statuses = {t.id: t.status for t in topics}
    if not allowed:
        return Brief(text, refs, since, BriefOutput(where_things_stand=[]), {}, routed=routed, statuses=statuses)

    def validate(out: BriefOutput) -> None:
        for c in out.all_claims():
            if not c.citations:
                raise ValueError("every claim needs at least one citation")
            check_citations(c.citations, allowed)

    out = llm.run("brief", "\n".join(lines), BriefOutput, validate=validate).output
    recorded = False
    if record and topics:
        for t in topics:
            state.last[t.id] = now
        try:
            vault.put_record(keys.BRIEF_STATE, state)
            recorded = True
        except LeaseError:
            pass  # another machine holds the store; the brief is still good, only "last looked" is not saved
    cited = {c for claim in out.all_claims() for c in claim.citations}
    return Brief(text, refs, since, out, _sources(conn, cited), recorded=recorded, routed=routed, statuses=statuses)
