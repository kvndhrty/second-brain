"""MCP server exposing the journal to agents such as Claude Code.

The same tools are served over stdio (`brain mcp`) and, from milestone 4,
over streamable HTTP inside `brain start`. Tools call the Brain façade; they
add no behaviour of their own beyond converting results and errors.

Sync tools run on worker threads; the shared Brain (core/shared.py) lets one
call use it at a time.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import datetime
from typing import Literal, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from brain import __version__
from brain.core.answers import AmbiguousTarget
from brain.core.formats.yamlio import FormatError
from brain.core.index.db import IndexError_
from brain.core.llm.models import LLMError
from brain.core.pipeline.runner import EntryResult
from brain.core.review import ReviewError
from brain.core.services import Brain, Busy, JotFailed
from brain.core.shared import SharedBrain
from brain.core.storage.base import StorageError
from brain.core.storage.lease import LeaseError
from brain.core.views import fragment_view
from brain.mcp import schemas as s

INSTRUCTIONS = """\
brain is the user's work journal, turned into a knowledge graph. Entries are
dated free text the user wrote. Each entry is split into fragments
(observations, decisions, questions, ideas, claims, action items) linked to
people, orgs (teams, offices, companies), themes (what the work is about, and
techniques used), and threads (workstreams).

- brief: get up to speed on a thread, person, org, theme, or topic. Best first
  call when the user returns to something after time away.
- ask: answer a specific question from the journal.
- search: find entries, fragments, and entities by words or meaning.
- get_entry: read a cited entry in full.
- get_entity: details on one person, org, theme, or thread.
- list_active: the user's current workstreams.
- add_entry: record something in the journal. Only when the user asks you to;
  write it as the user would, in plain text.

Results cite entry ids (e-YYYYMMDDTHHMMSSZ). Cite them when you report facts.
Fragments with status "speculative" are unconfirmed; "overcome_by_events" no
longer apply. Newer entries supersede older ones.
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
    ReviewError,
    Busy,
    JotFailed,
    KeyError,
    ValueError,
)


def _fragment(f) -> s.Fragment:
    return s.Fragment(**dataclasses.asdict(f))


def _sources(sources: dict) -> list[s.Source]:
    return [s.Source(entry_id=x.entry_id, date=x.date, label=x.label) for x in sources.values()]


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


# Runs the pipeline for a saved entry. In `brain mcp` it runs inline; inside
# `brain start` it hands the entry to the background worker and waits.
ProcessEntry = Callable[[str], EntryResult | None]


EntityKind = Literal["entry", "fragment", "person", "org", "theme", "thread"]


def build_server(shared: SharedBrain, *, process_entry: ProcessEntry | None = None) -> MCPServer:
    handle = ToolBrain(shared)
    srv = MCPServer(
        "brain", title="brain work journal", instructions=INSTRUCTIONS, version=__version__, log_level="WARNING"
    )

    @srv.tool(annotations=READ)
    def search(query: str, limit: int = 10, types: list[EntityKind] | None = None) -> s.SearchResult:
        """Find entries, fragments, people, orgs, themes, and threads by words or meaning."""
        hits = handle.call(
            lambda b: b.search(query, limit=max(1, min(limit, 50)), types=tuple(types) if types else None)
        )
        return s.SearchResult(hits=[s.SearchHit(**dataclasses.asdict(h)) for h in hits])

    @srv.tool(annotations=READ)
    def ask(question: str) -> s.AskResult:
        """Answer a question from the journal. Every claim cites the entries it comes from."""
        a = handle.call(lambda b: b.ask(question))
        return s.AskResult(claims=_claims(a.claims), insufficient=a.insufficient, sources=_sources(a.sources))

    @srv.tool(annotations=READ)
    def brief(target: str, since: str | None = None, mark_seen: bool = False) -> s.BriefResult:
        """Re-entry brief on a thread, person, org, or theme (name or id), or on free text: where things
        stand, recent decisions, open items, people, what is speculative or overcome by events, and what
        changed since `since` (YYYY-MM-DD; default: when the user last read a brief on it). Set mark_seen
        only if you are showing this brief to the user, so their "last looked" date moves forward."""
        when = datetime.fromisoformat(since).astimezone() if since else None
        r = handle.call(lambda b: b.brief(target, since=when, record=mark_seen))
        o = r.output
        return s.BriefResult(
            target=r.target,
            label=r.label,
            type=r.type,
            last_looked=r.since.date().isoformat() if r.since else None,
            summary=r.summary,
            where_things_stand=_claims(o.where_things_stand),
            recent_decisions=_claims(o.recent_decisions),
            open_items=_claims(o.open_items),
            people=_claims(o.people),
            speculative=_claims(o.speculative),
            overcome_by_events=_claims(o.overcome_by_events),
            changed_since=_claims(o.changed_since),
            open_fragments=[_fragment(fragment_view(f)) for f in r.open_items],
            sources=_sources(r.sources),
            notes=r.notes,
        )

    @srv.tool(annotations=READ)
    def get_entity(name_or_id: str) -> s.EntityResult:
        """One person, org, theme, or thread: details, notes, related nodes, open items, recent fragments."""
        e = handle.call(lambda b: b.entity(name_or_id))
        data = dataclasses.asdict(e)
        data["open_items"] = [s.Fragment(**f) for f in data["open_items"]]
        data["recent"] = [s.Fragment(**f) for f in data["recent"]]
        return s.EntityResult(**data)

    @srv.tool(annotations=READ)
    def get_entry(entry_id: str) -> s.EntryResult:
        """The full text of one entry, as the user wrote it, with the fragments extracted from it."""
        e = handle.call(lambda b: b.entry(entry_id))
        return s.EntryResult(
            id=e.id,
            date=e.date,
            title=e.title,
            text=e.text,
            processed=e.processed,
            fragments=[_fragment(f) for f in e.fragments],
        )

    @srv.tool(annotations=READ)
    def list_active() -> s.ActiveResult:
        """Threads (workstreams) with recent activity, plus pinned ones, most recent first."""

        def run(b: Brain) -> s.ActiveResult:
            threads = [
                s.ActiveThread(
                    id=t.id,
                    name=t.name,
                    state=t.state,
                    pinned=t.pinned,
                    last_activity=t.last_activity,
                    fragments=t.fragments,
                    open_items=t.open_items,
                    people=t.people,
                    summary=t.summary,
                )
                for t in b.active_threads()
            ]
            return s.ActiveResult(threads=threads, inactivity_days=b.settings.threads.inactivity_days)

        return handle.call(run)

    @srv.tool(annotations=WRITE)
    def add_entry(text: str, title: str | None = None) -> s.AddEntryResult:
        """Save a new journal entry and process it. Only when the user asks to record something."""

        def save(b: Brain):
            return b.jot(text, title=title), b.llm.configured

        saved, configured = handle.call(save)
        result = s.AddEntryResult(entry_id=saved.entry.id, key=saved.key, processed=False)
        if not configured:
            result.error = "no model endpoint configured; the entry is saved and will be processed later"
            return result
        if process_entry is not None:
            outcome = process_entry(saved.entry.id)
        else:
            outcome = handle.call(lambda b: b.process(saved.entry.id).entries[0], sync=False)
        if outcome is None:
            result.error = "still processing in the background; check later with get_entry"
            return result
        result.error = outcome.error
        result.processed = outcome.error is None

        def details(b: Brain) -> s.AddEntryResult:
            result.fragments = [_fragment(f) for f in b.entry(saved.entry.id).fragments]
            result.names_to_review = sum(
                1
                for i in b.review_items()
                if i.entry_id == saved.entry.id and i.kind in ("new_person", "new_org", "ambiguous_mention")
            )
            return result

        return handle.call(details)

    return srv
