"""The overcome-by-events sweep: items that may no longer be true or relevant.

Over weeks, open questions get answered elsewhere, action items get done or
dropped, speculation gets confirmed or forgotten, and threads go quiet. The
sweep lists them so you can mark each one overcome by events, close it,
confirm it, or keep it. Items whose status or state you already set yourself
are not listed again.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from brain.core.index import queries
from brain.core.views import FragmentView, fragment_view


@dataclass
class SweepItem:
    kind: str  # stale_open | stale_speculative | quiet_thread
    age_days: int
    fragment: FragmentView | None = None
    thread_id: str | None = None
    thread_name: str | None = None
    threads: list[str] | None = None  # names of the fragment's threads, for context
    suggestion: dict | None = None  # dreaming's verdict: verdict, evidence, reason, citations


def sweep_items(conn: sqlite3.Connection, *, now: datetime, days: int = 30, quiet_days: int = 60) -> list[SweepItem]:
    cutoff = (now - timedelta(days=days)).isoformat()
    rows = conn.execute(
        f"""
        SELECT {queries.FRAGMENT_COLS} FROM fragments f
        WHERE f.created_at < ? AND f.status != 'overcome_by_events'
          AND ((f.state = 'open' AND f.state_origin = 'llm')
               OR (f.status = 'speculative' AND f.status_origin = 'llm' AND COALESCE(f.state, '') != 'closed'))
          AND NOT EXISTS (
            SELECT 1 FROM edges e JOIN threads t ON e.dst = 'thread:' || t.id
            WHERE e.src = 'frag:' || f.id AND e.type = 'in_thread' AND t.state IN ('archived', 'obe')
          )
        ORDER BY f.created_at, f.id
        """,
        (cutoff,),
    ).fetchall()
    items = []
    for r in rows:
        f = queries._frag(r)
        names = [
            x[0]
            for x in conn.execute(
                "SELECT t.name FROM edges e JOIN threads t ON e.dst = 'thread:' || t.id "
                "WHERE e.src = ? AND e.type = 'in_thread' ORDER BY t.name",
                (f"frag:{f.id}",),
            )
        ]
        age = (now - datetime.fromisoformat(f.created_at)).days
        kind = "stale_open" if f.state == "open" else "stale_speculative"
        items.append(SweepItem(kind, age, fragment=fragment_view(f), threads=names))
    quiet_cutoff = (now - timedelta(days=quiet_days)).isoformat()
    for t in queries.threads(conn, states=("active", "proposed")):
        if not t.pinned and t.last_activity and t.last_activity < quiet_cutoff:
            age = (now - datetime.fromisoformat(t.last_activity)).days
            items.append(SweepItem("quiet_thread", age, thread_id=t.id, thread_name=t.name))
    return items
