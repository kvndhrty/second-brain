"""Background processing for `brain start`.

The worker has its own Brain (its own SQLite connection to the same index),
so long model calls do not hold the lock that web requests and MCP calls use.
It processes pending notes on startup, when woken (a note was saved), and
every `worker.poll_seconds`. A full listing of the store runs every
`FULL_LIST_SECONDS` to pick up markdown files saved into notes/ by hand.

When a round processes no note successfully and a dream pass is due
(`[dream] nightly_at` or `after_notes`), it dreams. New notes always come
first: a round that processed one leaves dreaming to a later round. Notes
that keep failing do not hold dreaming back.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from brain.core.pipeline.runner import NoteResult
from brain.core.services import Brain, Busy

FULL_LIST_SECONDS = 600


@dataclass
class WorkerStatus:
    running: bool = False
    enabled: bool = True
    current: str | None = None  # what it is doing now
    last_run: str | None = None
    last_error: str | None = None
    processed: int = 0
    failed: int = 0
    last_dream: str | None = None  # run id of the last dream pass


@dataclass
class _Waiter:
    event: threading.Event = field(default_factory=threading.Event)
    result: NoteResult | None = None


class Worker:
    def __init__(
        self, open_brain: Callable[[], Brain], *, poll_seconds: float, full_list_seconds: float = FULL_LIST_SECONDS
    ) -> None:
        self._open = open_brain
        self.poll = poll_seconds
        self.full_list = full_list_seconds
        self.status = WorkerStatus()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._waiters: dict[str, list[_Waiter]] = {}
        self._waiters_lock = threading.Lock()
        self._dream_requested = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="brain-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)

    def wake(self) -> None:
        self._wake.set()

    def request_dream(self) -> None:
        """Dream on the next idle round, whether or not a pass is due."""
        self._dream_requested = True
        self.wake()

    def process_now(self, note_id: str, timeout: float = 300.0) -> NoteResult | None:
        """Ask the worker to process a note and wait for it. None if it did not finish in time."""
        w = _Waiter()
        with self._waiters_lock:
            self._waiters.setdefault(note_id, []).append(w)
        self.wake()
        w.event.wait(timeout)
        return w.result

    def _finish(self, results: list[NoteResult], brain: Brain) -> None:
        with self._waiters_lock:
            done = {r.note_id: r for r in results}
            pending = {r[0] for r in brain.db.conn.execute("SELECT id FROM notes WHERE done = 0")}
            for note_id in list(self._waiters):
                if note_id in done or note_id not in pending:
                    for w in self._waiters.pop(note_id):
                        w.result = done.get(note_id) or NoteResult(note_id)
                        w.event.set()

    def _run(self) -> None:
        self.status.running = True
        brain: Brain | None = None
        last_full = 0.0
        try:
            while not self._stop.is_set():
                try:
                    if brain is None:
                        brain = self._open()
                    if not brain.llm.configured:
                        self.status.enabled = False
                        self.status.current = "idle: no model endpoint configured"
                        brain.sync()
                        self._finish([], brain)
                    else:
                        if time.monotonic() - last_full >= self.full_list:
                            brain.sync(full=True)
                            last_full = time.monotonic()
                        self.status.current = "processing"
                        report = brain.process_pending(progress=self._progress)
                        self.status.processed += sum(1 for e in report.notes if not e.error)
                        self.status.failed += len(report.failed)
                        self.status.last_error = report.failed[-1].error if report.failed else None
                        self._finish(report.notes, brain)
                        if not any(e.error is None for e in report.notes):
                            self._maybe_dream(brain)
                        self.status.current = "idle"
                    self.status.last_run = datetime.now(UTC).isoformat()
                except Busy:
                    self.status.current = "waiting: another brain process is processing"
                except Exception as e:  # noqa: BLE001 - keep running; report in status
                    self.status.last_error = str(e)
                    self.status.current = "error"
                self._wake.wait(self.poll)
                self._wake.clear()
        finally:
            self.status.running = False
            if brain is not None:
                brain.close()
            with self._waiters_lock:
                for waiters in self._waiters.values():
                    for w in waiters:
                        w.event.set()
                self._waiters.clear()

    def _maybe_dream(self, brain: Brain) -> None:
        reason = "requested" if self._dream_requested else brain.dream_due()
        self._dream_requested = False
        if reason is None:
            return
        self.status.current = f"dreaming: {reason}"
        run = brain.dream(progress=lambda msg: self._progress("", f"dreaming: {msg}"))
        self.status.last_dream = run.run_id
        if run.errors:
            self.status.last_error = run.errors[-1]

    def _progress(self, note_id: str, message: str) -> None:
        self.status.current = f"{note_id}: {message}" if note_id else message
