"""The injected context: a short summary of current state, loaded into every agent session.

It is what an agent should know without having to ask. It stays small,
because it costs tokens in every session; details stay in the wiki pages and
in `brief`, and every line carries the ids that lead to them.

It is rendered from the index on every read, so dates and ages are never
stale. Each thread's "Now" line is the one on its wiki page.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from brain.core import views
from brain.core.config import Settings
from brain.core.index import queries
from brain.core.index.queries import World
from brain.core.temporal import age, iso_day, local_date, utc_iso

MAX_THREADS = 12
MAX_CHANGES = 10
# Bookkeeping that an agent does not need to hear about.
QUIET_EVENTS = ("fallback", "ignored", "linked", "confirmed", "profile")


@dataclass
class ContextThread:
    id: str
    name: str
    last_activity: str | None
    age: str | None
    open_items: int
    now: str | None


@dataclass
class ContextDoc:
    date: str
    weekday: str
    recent_days: int
    threads: list[ContextThread] = field(default_factory=list)
    open_items: list[views.Line] = field(default_factory=list)
    open_total: int = 0
    changes: list[tuple[str, str]] = field(default_factory=list)  # (date, text)
    pending_notes: int = 0


def build(conn: sqlite3.Connection, settings: Settings, *, now: datetime) -> ContextDoc:
    today = local_date(now)
    cfg = settings.context
    world = World(conn)
    doc = ContextDoc(today.isoformat(), today.strftime("%A"), cfg.recent_days)
    for t in views.active_threads(conn, now=now, inactivity_days=settings.threads.inactivity_days)[:MAX_THREADS]:
        doc.threads.append(
            ContextThread(
                t.id,
                t.name,
                iso_day(t.last_activity) if t.last_activity else None,
                age(t.last_activity, today) if t.last_activity else None,
                t.open_items,
                t.now,
            )
        )
    items = sorted(world.open_items(), key=lambda s: (s.created_at, s.id), reverse=True)
    doc.open_total = len(items)
    doc.open_items = views.lines(world, items[: cfg.open_items], today)
    cutoff = utc_iso(now - timedelta(days=cfg.recent_days))
    for ev in queries.events(conn, limit=60):
        if ev.at >= cutoff and ev.kind not in QUIET_EVENTS and not ev.text.endswith("(provisional)"):
            doc.changes.append((iso_day(ev.at), ev.text))
    doc.changes = doc.changes[:MAX_CHANGES]
    doc.pending_notes = conn.execute("SELECT COUNT(*) FROM notes WHERE done = 0").fetchone()[0]
    return doc


def _one_line(text: str | None) -> str:
    return " ".join((text or "").split())


def render_markdown(doc: ContextDoc, *, max_lines: int, max_bytes: int) -> str:
    head = [
        f"# brain: the user's work journal, as of {doc.date} ({doc.weekday})",
        "Built from the user's notes. Dates are when a note was written; ages are relative to today.",
    ]
    foot = [
        "More: `get_page(id)` for a thread, person, org, or topic; `brief(request)` to catch up; "
        "`ask(question)`; `jot(text)` to add a note. To correct anything here, jot a note saying what is wrong."
    ]
    sections: list[tuple[str, list[str]]] = []
    threads = []
    for t in doc.threads:
        when = f"last activity {t.last_activity} ({t.age})" if t.last_activity else "no activity yet"
        opened = f", {t.open_items} open" if t.open_items else ""
        threads.append(f"- {t.name} [{t.id}]: {when}{opened}")
        if t.now:
            threads.append(f"  Now: {_one_line(t.now)}")
    sections.append(("## Active threads, most recent first", threads))
    shown = (
        f"{len(doc.open_items)} of {doc.open_total}" if doc.open_total > len(doc.open_items) else str(doc.open_total)
    )
    items = [
        f"- {i.date} ({i.age}) {'to do' if i.kind == 'action' else i.kind}"
        + (f" · {i.thread_name}" if i.thread_name else "")
        + f": {_one_line(i.text)} [{i.note_id}]"
        for i in doc.open_items
    ]
    sections.append((f"## Open questions and things to do, newest first ({shown})", items))
    sections.append(
        (f"## Changed in the last {doc.recent_days} days", [f"- {d}: {_one_line(text)}" for d, text in doc.changes])
    )
    tail = [f"{doc.pending_notes} note(s) not processed yet."] if doc.pending_notes else []

    out = list(head)
    closing = (["", *tail] if tail else []) + ["", *foot]
    budget_lines = max_lines - len(closing) - 1
    budget_bytes = max_bytes - _size(closing) - 80

    def fits(extra: list[str]) -> bool:
        return len(out) + len(extra) <= budget_lines and _size(out) + _size(extra) <= budget_bytes

    for heading, lines in sections:
        if not lines:
            continue
        block = ["", heading]
        if not fits(block + lines[:1]):
            out.append(f"({heading.removeprefix('## ')}: left out to keep this short)")
            break
        out += block
        for i, line in enumerate(lines):
            if not fits([line]):
                out.append(f"- … {len(lines) - i} more lines not shown")
                break
            out.append(line)
        else:
            continue
        break  # a section was cut short; the rest would not fit either
    out += closing
    return "\n".join(out) + "\n"


def _size(lines: list[str]) -> int:
    return sum(len(line.encode()) + 1 for line in lines)
