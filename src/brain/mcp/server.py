"""MCP server exposing the journal to agents such as Claude Code.

The same tools are served over stdio (`brain mcp`) and over streamable HTTP
inside `brain start`. Tools call the Brain façade; they add no behaviour of
their own beyond converting results and errors.

Sync tools run on worker threads; the shared Brain (core/shared.py) lets one
call use it at a time.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Literal, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from brain import __version__
from brain.core.answers import AmbiguousTarget
from brain.core.formats.yamlio import FormatError
from brain.core.index.db import IndexError_
from brain.core.llm.models import LLMError
from brain.core.pipeline.runner import NoteResult
from brain.core.services import Brain, Busy, JotFailed
from brain.core.shared import SharedBrain
from brain.core.storage.base import StorageError
from brain.core.storage.lease import LeaseError
from brain.core.temporal import iso_day, local_date
from brain.core.views import NoSuchPage
from brain.mcp import schemas as s

INSTRUCTIONS = """\
brain is the user's work journal. The user only writes notes: dated free
text. Models turn the notes into a wiki: each note is split into statements
(facts, decisions, questions, actions, ideas), and the statements are linked
to pages for threads (ongoing pieces of work), people, orgs, and topics.

- get_context: a short, dated summary of current state. Read it first.
- brief: get up to speed on a thread, person, org, or topic. Best first call
  when the user returns to something after time away.
- ask: answer a specific question from the journal.
- search: find pages, statements, and notes by words or meaning.
- get_page: one wiki page: where it stands, summary, open items, decisions,
  history.
- get_note: read a cited note in full.
- list_active: the user's current threads.
- jot: add a note. Only when the user asks you to record something; write it
  as the user would, in plain text. Notes are the only way to change the
  wiki: to correct it, jot what is wrong ("Jane Doe and J. Doe are the same
  person", "the vendor evaluation is finished", "ignore the note about the
  outage cause").

Results cite note ids (n-YYYYMMDDTHHMMSSZ). Cite them when you report facts.
A statement with status "superseded" was replaced by a later note and no
longer holds. Every result has today's date and a date and age for each
item: newer notes replace older ones on the same point.
"""

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)

T = TypeVar("T")
EXPECTED = (
    LLMError,
    StorageError,
    LeaseError,
    FormatError,
    IndexError_,
    AmbiguousTarget,
    NoSuchPage,
    Busy,
    JotFailed,
    KeyError,
    ValueError,
)


def _today() -> date:
    return local_date(datetime.now(UTC))


def _statements(lines) -> list[s.Statement]:
    return [s.Statement(**dataclasses.asdict(ln)) for ln in lines]


def _refs(refs) -> list[s.Ref]:
    return [s.Ref(id=r.id, type=r.type, name=r.name) for r in refs]


def _sources(sources: dict) -> list[s.Source]:
    """Oldest first, so the newest source is last."""
    return [s.Source(**dataclasses.asdict(x)) for x in sorted(sources.values(), key=lambda x: x.date)]


def _claims(claims) -> list[s.Claim]:
    return [s.Claim(text=c.text, citations=c.citations) for c in claims]


class ToolBrain:
    """The shared Brain, with expected errors turned into ToolError so the agent sees the message."""

    def __init__(self, shared: SharedBrain) -> None:
        self.shared = shared

    def call(self, fn: Callable[[Brain], T], *, sync: bool = True) -> T:
        try:
            return self.shared.call(fn, sync=sync)
        except EXPECTED as e:
            message = e.args[0] if isinstance(e, KeyError) and e.args else str(e)
            raise ToolError(str(message)) from e


# Runs the layers for a saved note. In `brain mcp` it runs inline; inside
# `brain start` it hands the note to the background worker and waits.
ProcessNote = Callable[[str], NoteResult | None]


HitType = Literal["note", "statement", "person", "org", "topic", "thread"]


def _with_context(shared: SharedBrain) -> str:
    """INSTRUCTIONS plus the injected context, for a server started fresh for each session."""
    try:
        text = shared.call(lambda b: b.context_text())
    except Exception:  # noqa: BLE001 - the tools still work; the agent can call get_context
        return INSTRUCTIONS
    return f"{INSTRUCTIONS}\nCurrent state of the journal, as of this session's start:\n\n{text}"


def build_server(
    shared: SharedBrain, *, process_note: ProcessNote | None = None, context_in_instructions: bool = False
) -> MCPServer:
    """context_in_instructions: put the injected context in the server instructions. Only for
    `brain mcp`, which starts once per agent session; `brain start` runs for days, so its MCP
    server leaves it out and agents read get_context or brain://context instead."""
    handle = ToolBrain(shared)
    instructions = _with_context(shared) if context_in_instructions else INSTRUCTIONS
    srv = MCPServer(
        "brain", title="brain work journal", instructions=instructions, version=__version__, log_level="WARNING"
    )

    @srv.resource(
        "brain://context",
        name="context",
        title="Current state of the journal",
        description="Active threads with where each stands, open items, and recent changes, all dated.",
        mime_type="text/markdown",
    )
    def context_resource() -> str:
        return handle.call(lambda b: b.context_text())

    @srv.tool(annotations=READ)
    def get_context() -> str:
        """A short, dated summary of current state: active threads and where each stands, the newest open
        questions and things to do, and recent changes. Read it first in a session if it was not already
        provided."""
        return handle.call(lambda b: b.context_text())

    @srv.tool(annotations=READ)
    def search(query: str, limit: int = 10, types: list[HitType] | None = None) -> s.SearchResult:
        """Find wiki pages (threads, people, orgs, topics), statements, and notes by words or meaning."""
        hits = handle.call(
            lambda b: b.search(query, limit=max(1, min(limit, 50)), types=tuple(types) if types else None)
        )
        return s.SearchResult(today=_today().isoformat(), hits=[s.SearchHit(**dataclasses.asdict(h)) for h in hits])

    @srv.tool(annotations=READ)
    def ask(question: str) -> s.AskResult:
        """Answer a question from the journal. Every claim cites the notes it comes from."""
        a = handle.call(lambda b: b.ask(question))
        return s.AskResult(
            today=_today().isoformat(),
            claims=_claims(a.claims),
            insufficient=a.insufficient,
            sources=_sources(a.sources),
        )

    @srv.tool(annotations=READ)
    def brief(request: str, since: str | None = None, mark_seen: bool = False) -> s.BriefResult:
        """Re-entry brief on a thread, person, org, or topic (name or id), or on a request in plain words
        ("where are we on the billing move and what does Dana owe us"), which may cover several of them:
        where things stand, recent decisions, open items, people, what no longer applies, and what changed
        since `since` (YYYY-MM-DD; default: when the user last read a brief on it). Set mark_seen only if
        you are showing this brief to the user, so their "last looked" date moves forward."""
        when = datetime.fromisoformat(since).astimezone() if since else None
        r = handle.call(lambda b: b.brief(request, since=when, record=mark_seen))
        o = r.output
        return s.BriefResult(
            today=_today().isoformat(),
            topics=_refs(r.topics),
            last_looked=r.since.date().isoformat() if r.since else None,
            where_things_stand=_claims(o.where_things_stand),
            recent_decisions=_claims(o.recent_decisions),
            open_items=_claims(o.open_items),
            people=_claims(o.people),
            no_longer_applies=_claims(o.no_longer_applies),
            changed_since=_claims(o.changed_since),
            sources=_sources(r.sources),
        )

    @srv.tool(annotations=READ)
    def get_page(name_or_id: str) -> s.PageResult:
        """One wiki page: a thread, person, org, or topic. Where it stands, its summary, open questions and
        things to do, decisions that stand, the history of statements, and related pages."""
        p = handle.call(lambda b: b.page(name_or_id))
        return s.PageResult(
            today=_today().isoformat(),
            id=p.id,
            type=p.type,
            name=p.name,
            names=p.names,
            status=p.status,
            profile=p.profile,
            as_of=p.as_of,
            now=s.Cited(text=p.now.text, citations=p.now.citations) if p.now else None,
            summary=[s.Cited(text=c.text, citations=c.citations) for c in p.summary],
            open=_statements(p.open),
            decided=_statements(p.decided),
            history=_statements(p.history[:60]),
            related=_refs(p.related),
        )

    def _note(b: Brain, note_id: str) -> s.NoteResult:
        n = b.note(note_id)
        return s.NoteResult(
            today=_today().isoformat(),
            id=n.id,
            date=iso_day(n.created),
            title=n.title,
            text=n.body,
            processed=n.done,
            statements=_statements(n.statements),
            mentions=_refs(n.entities),
            changes=[ev.text for ev in n.events],
        )

    @srv.tool(annotations=READ)
    def get_note(note_id: str) -> s.NoteResult:
        """The full text of one note, as the user wrote it, with the statements made from it."""
        return handle.call(lambda b: _note(b, note_id))

    @srv.tool(annotations=READ)
    def list_active() -> s.ActiveResult:
        """Threads (ongoing pieces of work) with recent activity or open items, most recent first."""

        def run(b: Brain) -> s.ActiveResult:
            threads = [
                s.ActiveThread(
                    id=t.id,
                    name=t.name,
                    last_activity=iso_day(t.last_activity) if t.last_activity else None,
                    open_items=t.open_items,
                    now=t.now,
                )
                for t in b.active_threads()
            ]
            return s.ActiveResult(today=_today().isoformat(), threads=threads)

        return handle.call(run)

    @srv.tool(annotations=WRITE)
    def jot(text: str, title: str | None = None, about: str | None = None) -> s.JotResult:
        """Save a new note and process it. Only when the user asks to record something. To correct the
        wiki, write what is wrong as a note; `about` may name the page (id or name) the correction is for."""

        def save(b: Brain):
            page = b.page(about).id if about else None
            return b.jot(text, title=title, about=page), b.llm.configured

        saved, configured = handle.call(save)
        result = s.JotResult(today=_today().isoformat(), note_id=saved.note.id, key=saved.key, processed=False)
        if not configured:
            result.error = "no model endpoint configured; the note is saved and will be processed later"
            return result
        if process_note is not None:
            outcome = process_note(saved.note.id)
        else:

            def run(b: Brain):
                try:
                    return b.process(saved.note.id).result(saved.note.id)
                except (Busy, LeaseError):
                    return None  # the note is saved; the process that is running will pick it up

            outcome = handle.call(run, sync=False)
        if outcome is None:
            result.error = "still processing in the background; check later with get_note"
            return result
        result.error = outcome.error
        result.processed = outcome.error is None

        def details(b: Brain) -> s.JotResult:
            n = _note(b, saved.note.id)
            result.statements, result.changes = n.statements, n.changes
            return result

        return handle.call(details)

    return srv
