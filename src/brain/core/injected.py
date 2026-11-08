"""The injected context: a short summary of current state, loaded into every agent session.

It is the top of the memory hierarchy: what an agent should know without
having to ask. It stays small, because it costs tokens in every session and a
long one lowers how well instructions are followed; details stay in `brief`
and `get_entity`, and every line carries the ids that lead to them.

It is rendered from the index on every read, so dates and ages are never
stale. Each thread's "Now" line comes from dreaming; before a thread has been
dreamed about, the first sentence of its summary stands in, with the
summary's as-of date.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from brain.core.config import Settings
from brain.core.index import queries
from brain.core.temporal import age, iso_day, local_date

REVIEW_GROUPS = {
    "name(s) to review": ("new_person", "new_org", "ambiguous_mention"),
    "proposed theme(s) or thread(s)": ("proposed_theme", "proposed_thread"),
    "proposed answer(s)": ("proposed_answer",),
    "item(s) that may be overcome by events": ("dream_obe",),
}
MAX_THREADS = 12
MAX_CHANGES = 8
NOW_FALLBACK_CHARS = 200


@dataclass
class ContextThread:
    id: str
    name: str
    pinned: bool
    proposed: bool  # the pipeline proposed this thread and you have not accepted it yet
    last_activity: str | None
    age: str | None
    open_items: int
    now: str | None
    now_as_of: str | None
    now_source: str | None  # dream | summary


@dataclass
class ContextItem:
    id: str
    entry_id: str
    date: str
    age: str
    kind: str
    text: str
    thread: str | None
    note: str | None = None


@dataclass
class ContextDoc:
    today: str
    weekday: str
    entries_through: str | None
    threads: list[ContextThread] = field(default_factory=list)
    open_items: list[ContextItem] = field(default_factory=list)
    open_total: int = 0
    changes: list[ContextItem] = field(default_factory=list)
    recent_days: int = 7
    waiting: dict[str, int] = field(default_factory=dict)
    pending_entries: int = 0


def _first_sentence(md: str) -> str:
    text = re.sub(r"\[e-[^\]]+\]", "", md)  # inline citations
    text = " ".join(line.strip().lstrip("-*# ").strip() for line in text.splitlines() if line.strip())
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    sentence = (m.group(1) if m else text).strip()
    return sentence if len(sentence) <= NOW_FALLBACK_CHARS else sentence[: NOW_FALLBACK_CHARS - 1] + "…"


def _thread_of(conn: sqlite3.Connection, fragment_id: str) -> str | None:
    r = conn.execute(
        "SELECT substr(dst, 8) FROM edges WHERE src = ? AND type = 'in_thread' ORDER BY weight DESC, dst LIMIT 1",
        (f"frag:{fragment_id}",),
    ).fetchone()
    return r[0] if r else None


def build(conn: sqlite3.Connection, settings: Settings, *, now: datetime) -> ContextDoc:
    today = local_date(now)
    newest = conn.execute("SELECT MAX(created_at) FROM entries").fetchone()[0]
    doc = ContextDoc(
        today=today.isoformat(),
        weekday=today.strftime("%A"),
        entries_through=iso_day(newest) if newest else None,
        recent_days=settings.context.recent_days,
    )
    for t in queries.active_threads(conn, now=now, inactivity_days=settings.threads.inactivity_days)[:MAX_THREADS]:
        if t.now_line:
            now_line, now_as_of, source = t.now_line, t.now_as_of, "dream"
        elif t.summary:
            now_line, now_as_of, source = _first_sentence(t.summary), t.summary_as_of, "summary"
        else:
            now_line = now_as_of = source = None
        doc.threads.append(
            ContextThread(
                id=t.id,
                name=t.name,
                pinned=t.pinned,
                proposed=t.state == "proposed",
                last_activity=iso_day(t.last_activity) if t.last_activity else None,
                age=age(t.last_activity, today) if t.last_activity else None,
                open_items=t.open_items,
                now=now_line,
                now_as_of=iso_day(now_as_of) if now_as_of else None,
                now_source=source,
            )
        )

    excluded = "SELECT 1 FROM edges e JOIN threads t ON e.dst = 'thread:' || t.id WHERE e.src = 'frag:' || f.id "
    excluded += "AND e.type = 'in_thread' AND t.state IN ('archived', 'obe')"
    open_rows = conn.execute(
        f"SELECT {queries.FRAGMENT_COLS} FROM fragments f WHERE f.state = 'open' AND f.status != 'overcome_by_events' "
        f"AND f.someday = 0 AND NOT EXISTS ({excluded}) ORDER BY f.created_at DESC, f.id DESC"
    ).fetchall()
    doc.open_total = len(open_rows)
    for r in open_rows[: settings.context.open_items]:
        f = queries._frag(r)
        doc.open_items.append(
            ContextItem(
                f.id,
                f.entry_id,
                iso_day(f.created_at),
                age(f.created_at, today),
                f.kind,
                f.text,
                _thread_of(conn, f.id),
            )
        )

    cutoff = (now - timedelta(days=settings.context.recent_days)).isoformat()
    for r in conn.execute(
        f"SELECT {queries.FRAGMENT_COLS} FROM fragments f WHERE f.kind = 'decision' AND f.created_at >= ? "
        "AND f.status != 'overcome_by_events' AND f.someday = 0 ORDER BY f.created_at DESC, f.id DESC LIMIT ?",
        (cutoff, MAX_CHANGES),
    ):
        f = queries._frag(r)
        doc.changes.append(
            ContextItem(
                f.id,
                f.entry_id,
                iso_day(f.created_at),
                age(f.created_at, today),
                "decision",
                f.text,
                _thread_of(conn, f.id),
            )
        )
    obe_cutoff = (today - timedelta(days=settings.context.recent_days)).isoformat()
    for r in conn.execute(
        f"SELECT {queries.FRAGMENT_COLS} FROM fragments f WHERE f.status = 'overcome_by_events' AND f.obe_date >= ? "
        "ORDER BY f.obe_date DESC, f.id DESC LIMIT ?",
        (obe_cutoff, MAX_CHANGES),
    ):
        f = queries._frag(r)
        assert f.obe_date is not None
        doc.changes.append(
            ContextItem(
                f.id,
                f.entry_id,
                f.obe_date,
                age(f.obe_date, today),
                "no longer holds",
                f.text,
                _thread_of(conn, f.id),
                note=f.obe_reason,
            )
        )
    doc.changes.sort(key=lambda c: c.date, reverse=True)

    counts = dict(conn.execute("SELECT kind, COUNT(*) FROM review_items GROUP BY kind").fetchall())
    for label, kinds in REVIEW_GROUPS.items():
        n = sum(counts.get(k, 0) for k in kinds)
        if n:
            doc.waiting[label] = n
    doc.pending_entries = conn.execute("SELECT COUNT(*) FROM entries WHERE next_step != 'done'").fetchone()[0]
    return doc


def render_markdown(doc: ContextDoc, *, max_lines: int, max_bytes: int) -> str:
    """The summary as markdown, filled section by section until a limit is reached."""
    through = f" Entries through {doc.entries_through}." if doc.entries_through else " No entries yet."
    head = [f"# brain: {doc.today} ({doc.weekday}).{through}"]
    foot = ["For details: brief <thread-id>, get_entity <id>, get_entry <entry-id>."]

    sections: list[tuple[str, list[str], int]] = []  # (heading, lines, total items)
    threads = []
    for t in doc.threads:
        bits = [
            f"{t.name} [{t.id}]",
            f"last activity {t.last_activity} ({t.age})" if t.last_activity else "no activity",
        ]
        if t.pinned:
            bits.append("pinned")
        if t.proposed:
            bits.append("proposed")
        if t.open_items:
            bits.append(f"{t.open_items} open")
        threads.append("- " + " · ".join(bits))
        if t.now:
            threads.append(f"  Now: {t.now} [as of {t.now_as_of or 'unknown'}]")
    sections.append(("## Active threads, most recent first", threads, len(doc.threads)))

    items = [
        f"- {i.date} ({i.age}) {i.kind.replace('_', ' ')}"
        + (f" · {i.thread}" if i.thread else "")
        + f": {i.text} [{i.id}]"
        for i in doc.open_items
    ]
    shown = (
        f"{len(doc.open_items)} of {doc.open_total}" if doc.open_total > len(doc.open_items) else str(doc.open_total)
    )
    sections.append((f"## Open items, newest first ({shown})", items, len(doc.open_items)))

    changes = [
        f"- {c.date} ({c.age}) {c.kind}"
        + (f" · {c.thread}" if c.thread else "")
        + f": {c.text}"
        + (f" ({c.note})" if c.note else "")
        + f" [{c.entry_id}]"
        for c in doc.changes
    ]
    sections.append((f"## Changed in the last {doc.recent_days} days", changes, len(doc.changes)))

    waiting = [f"{n} {label}" for label, n in doc.waiting.items()]
    if doc.pending_entries:
        waiting.append(f"{doc.pending_entries} entry(s) not processed yet")
    tail = ["## Waiting on the user: " + ", ".join(waiting)] if waiting else []

    out = list(head)
    # The tail and footer always appear, each after a blank line; one more line
    # is kept free for a "not shown" notice.
    closing = (["", *tail] if tail else []) + ["", *foot]
    budget_lines = max_lines - len(closing) - 1
    budget_bytes = max_bytes - _size(closing) - 80

    def fits(extra: list[str]) -> bool:
        return len(out) + len(extra) <= budget_lines and _size(out) + _size(extra) <= budget_bytes

    for heading, lines, _total in sections:
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
