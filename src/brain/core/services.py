"""The Brain façade: the only API the CLI, web, and MCP front-ends use."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

from brain.core import answers, dream, injected, keys, paths, views
from brain.core.config import LoadedSettings, Settings
from brain.core.formats.note import Note
from brain.core.index import queries
from brain.core.index.db import IndexDB
from brain.core.index.queries import World
from brain.core.index.sync import Syncer, SyncReport
from brain.core.llm.models import LLM, LLMError
from brain.core.localstate import LocalState
from brain.core.pipeline import wiki
from brain.core.pipeline.context import PipelineContext
from brain.core.pipeline.runner import FAILURES, Pipeline, ProcessReport
from brain.core.storage import Storage, open_storage
from brain.core.storage.base import ObjectInfo
from brain.core.storage.lease import Lease
from brain.core.storage.probe import ensure_probed
from brain.core.vault import Vault


class JotFailed(Exception):
    def __init__(self, draft: Path, cause: Exception) -> None:
        self.draft = draft
        self.cause = cause
        super().__init__(f"{cause}\nYour text is saved in {draft}; run `brain jot --file {draft}` to retry.")


@dataclass(frozen=True)
class JotResult:
    note: Note
    key: str


class Busy(Exception):
    """Another brain process on this machine is already running the pipeline."""


@dataclass
class RebuildReport:
    notes: int
    failed: list[str]  # the note the rebuild stopped at, if any
    calls: int
    remaining: int = 0  # notes not reached; `brain process` continues from there


def index_path(settings: Settings, storage: Storage) -> Path:
    if settings.index.path:
        p = Path(settings.index.path).expanduser()
        return p / "index.db" if p.is_dir() or settings.index.path.endswith("/") else p
    store_hash = hashlib.sha256(storage.ident.encode()).hexdigest()[:12]
    return paths.cache_dir() / store_hash / "index.db"


class Brain:
    def __init__(
        self,
        loaded: LoadedSettings,
        *,
        command: str = "brain",
        storage: Storage | None = None,
        state: LocalState | None = None,
        force_lease: bool = False,
        llm: LLM | None = None,
    ) -> None:
        self.loaded = loaded
        self.settings = loaded.settings
        self.storage = storage or open_storage(self.settings)
        self.state = state or LocalState()
        self.lease = Lease(
            self.storage,
            machine_id=self.state.machine_id,
            ttl_seconds=self.settings.lease.ttl_seconds,
            command=command,
            force=force_lease,
            local_lock=self.state.lease_lock(self.storage.ident),
        )
        self.db = IndexDB(index_path(self.settings, self.storage), embed_model=self.settings.llm.embed_model)
        self.syncer = Syncer(self.db, self.storage, embed_model=self.settings.llm.embed_model)
        self.vault = Vault(
            self.storage,
            self.lease,
            self.state,
            on_write=self._on_write,
            prepare=lambda: ensure_probed(self.storage, self.state),
        )
        self._llm = llm

    def __enter__(self) -> Brain:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.lease.release()
        finally:
            self.db.close()

    def _on_write(self, key: str, info: ObjectInfo, data: bytes | None, prev: str | None, new: str) -> None:
        self.syncer.apply_write(key, info, data, prev_token=prev, new_token=new)

    # --- index ---------------------------------------------------------------

    def sync(self, *, full: bool = False) -> SyncReport:
        return self.syncer.sync(full=full)

    def reindex(self) -> SyncReport:
        """Delete the local index and rebuild it from the store. Calls no models."""
        self.db.delete()
        self.db = IndexDB(self.db.path, embed_model=self.settings.llm.embed_model)
        self.syncer = Syncer(self.db, self.storage, embed_model=self.settings.llm.embed_model)
        return self.syncer.sync(full=True)

    # --- notes ----------------------------------------------------------------

    def jot(self, text: str, *, title: str | None = None, about: str | None = None) -> JotResult:
        """Save a note. The text is kept in a local draft until the store confirms the write.

        `about` is the id of the wiki page the note was written from (the
        "This is wrong" button); the note is then read as a correction to it.
        """
        if not text.strip():
            raise ValueError("note is empty")
        draft = self.state.drafts_dir / f"draft-{secrets.token_hex(4)}.md"
        draft.write_text(text, encoding="utf-8")
        try:
            note, key = self.vault.save_note(text, title=title, about=about)
        except Exception as e:
            raise JotFailed(draft, e) from e
        draft.unlink(missing_ok=True)
        return JotResult(note, key)

    def pending_drafts(self) -> list[Path]:
        return sorted(self.state.drafts_dir.glob("draft-*.md"))

    def adopt(self) -> list[Note]:
        """Turn markdown files saved into notes/ by hand into notes."""
        self.sync()
        return self._adopt()

    def _adopt(self) -> list[Note]:
        found = self.vault.adopt_bare_notes()
        known = {r[0] for r in self.db.conn.execute("SELECT key FROM notes")}
        if any(k not in known for k in found.in_place):
            self.sync(full=True)  # a complete note file was put in notes/ by hand; only a listing finds it
        return found.notes

    # --- models and pipeline -------------------------------------------------

    @property
    def llm(self) -> LLM:
        if self._llm is None:
            self._llm = LLM(self.settings)
        return self._llm

    def _ctx(self) -> PipelineContext:
        return PipelineContext(self.settings, self.vault, self.db, self.llm, clock=self.clock)

    def clock(self) -> datetime:
        return datetime.now(UTC)

    def _require_llm(self) -> None:
        if not self.llm.configured:
            raise LLMError("no model endpoint is configured; set [llm] base_url (run `brain init`)")

    @contextmanager
    def _processing(self, busy: str) -> Iterator[None]:
        """Hold the processing lock, which keeps the pipeline and dreaming from overlapping across processes."""
        with self.state.try_lock("process") as got:
            if not got:
                raise Busy(busy)
            yield

    def _pipeline(self) -> Pipeline:
        self._require_llm()
        return Pipeline(self._ctx(), self.state)

    def process(
        self, note_id: str, *, force: bool = False, progress: Callable[[str], None] | None = None
    ) -> ProcessReport:
        """Process a note, after any earlier notes that are still pending.

        Notes are processed in date order, so if an earlier note fails this
        one waits; `ProcessReport.result(note_id)` says so. `force` runs this
        one note's layers again, whether or not they are current.
        """
        with self._processing("another brain process is processing notes; it will pick this one up"):
            pipeline = self._pipeline()
            if force:
                return ProcessReport([pipeline.run_note(note_id, force=True, progress=progress)])
            say = (lambda _note, msg: progress(msg)) if progress else None
            return pipeline.process_pending(say, through=note_id)

    def process_pending(
        self, progress: Callable[[str, str], None] | None = None, *, wait_for_retry: bool = False
    ) -> ProcessReport:
        self.sync()
        with self._processing("another brain process is already processing notes"):
            self._adopt()
            return self._pipeline().process_pending(progress, wait_for_retry=wait_for_retry)

    def skip(self, note_id: str) -> None:
        """Leave a note that cannot be processed out of the wiki, so later notes are not held up."""
        with self._processing("another brain process is processing notes"):
            self._pipeline().skip(note_id)

    def rebuild(self, progress: Callable[[str, str], None] | None = None) -> RebuildReport:
        """Discard everything generated except entity ids, and run every note again in date order.

        This is how an improved prompt reaches old notes. It stops at the first
        note that fails; `process_pending` continues from there. Entity records are
        reset to how they were created (names, status, and merges are learned
        again from the notes) but keep their ids, so page paths and links stay
        the same. An entity no note leads to any more stays provisional, with
        no page.
        """
        self.sync(full=True)
        with self._processing("another brain process is processing notes or dreaming"):
            pipeline = self._pipeline()
            for o in list(self.storage.list(keys.GENERATED)):
                if not o.key.startswith(keys.ENTITIES):
                    self.vault.delete(o.key)
            for o in list(self.storage.list(keys.WIKI)):
                self.vault.delete(o.key)
            for e in self.world().entities.values():
                self.vault.put_entity(e.record.reset())
            ids = [r[0] for r in self.db.conn.execute("SELECT id FROM notes ORDER BY created_at, id")]
            failed, calls, done = [], 0, 0
            for note_id in ids:
                say = (lambda msg, n=note_id: progress(n, msg)) if progress else None
                result = pipeline.run_note(note_id, force=True, progress=say)
                calls += result.calls
                if result.error:
                    # Later notes depend on this one, so the rebuild stops here.
                    failed.append(f"{note_id}: {result.error}")
                    break
                done += 1
            wiki.write(pipeline.ctx, [], self.clock(), everything=True)
            return RebuildReport(len(ids), failed, calls, remaining=len(ids) - done)

    def failures(self) -> dict[str, dict]:
        return self.state.read_json(FAILURES)

    # --- reading ---------------------------------------------------------------

    def world(self) -> World:
        return World(self.db.conn)

    def active_threads(self, *, now: datetime | None = None) -> list[views.EntitySummary]:
        return views.active_threads(
            self.db.conn, now=now or self.clock(), inactivity_days=self.settings.threads.inactivity_days
        )

    def ask(self, question: str) -> answers.Answer:
        self._require_llm()
        return answers.ask(self.db.conn, self.llm, question, now=self.clock())

    def brief(self, request: str, *, since: datetime | None = None, record: bool = True) -> answers.Brief:
        self._require_llm()
        return answers.brief(self.db.conn, self.llm, self.vault, request, now=self.clock(), since=since, record=record)

    def search(self, query: str, *, limit: int = 10, types: tuple[str, ...] | None = None) -> list[views.Hit]:
        llm = self.llm if self.llm.configured and self.llm.embed_model else None
        return views.search_all(self.db.conn, llm, query, limit=limit, types=types)

    def page(self, entity: str) -> views.PageData:
        """A wiki page by id or exact name. Raises views.NoSuchPage."""
        return views.page(self.db.conn, entity, now=self.clock())

    def page_markdown(self, entity: str) -> str:
        e = views.resolve(self.world(), entity)
        return wiki.Renderer(self._ctx(), self.clock()).page(e.id)

    def pages(self, etype: str | None = None) -> list[views.EntitySummary]:
        return views.entity_list(self.db.conn, etype)

    def note(self, note_id: str) -> views.NoteView:
        return views.note(self.db.conn, note_id, now=self.clock())

    def recent_notes(self, *, limit: int = 50, before: str | None = None) -> list[views.NoteSummary]:
        return views.recent_notes(self.db.conn, limit=limit, before=before)

    def log(self, *, limit: int = 100) -> list[queries.LogEvent]:
        return queries.events(self.db.conn, limit=limit)

    def status(self) -> dict:
        c = self.db.conn
        return {
            "store": self.storage.describe(),
            "notes": c.execute("SELECT COUNT(*) FROM notes").fetchone()[0],
            "pending": c.execute("SELECT COUNT(*) FROM notes WHERE done = 0").fetchone()[0],
            "pages": c.execute(
                "SELECT COUNT(*) FROM entities WHERE merged_into IS NULL AND status != 'provisional'"
            ).fetchone()[0],
            "llm_configured": self.llm.configured,
            "failures": len(self.failures()),
        }

    # --- injected context and dreaming ------------------------------------------

    def context(self, *, now: datetime | None = None) -> injected.ContextDoc:
        return injected.build(self.db.conn, self.settings, now=now or self.clock())

    def context_text(self, *, now: datetime | None = None) -> str:
        cfg = self.settings.context
        return injected.render_markdown(self.context(now=now), max_lines=cfg.max_lines, max_bytes=cfg.max_bytes)

    def dream_plan(self) -> dream.DreamPlan:
        """What a dream pass would look at, without calling a model."""
        self.sync()
        return dream.Dreamer(self._ctx(), self.settings.dream).plan()

    def dream(self, *, progress: Callable[[str], None] | None = None) -> dream.DreamRun:
        """Run one dream pass. Holds the processing lock so it never overlaps the pipeline."""
        self._require_llm()
        self.sync()
        with self._processing("another brain process is processing notes or dreaming"):
            return dream.Dreamer(self._ctx(), self.settings.dream).run(progress=progress)

    def dream_due(self, *, now: datetime | None = None) -> str | None:
        return dream.is_due(self._ctx(), self.settings.dream, now=now or self.clock())

    def last_dream(self) -> dream.DreamRun | None:
        return dream.last_run(self._ctx())
