"""The Brain façade: the only API the CLI, web, and MCP front-ends use."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from types import TracebackType

from brain.core import answers, keys, paths
from brain.core.config import LoadedSettings, Settings
from brain.core.formats.decisions import FragmentDecision, OpenState, Status
from brain.core.formats.entity import ObeInfo
from brain.core.formats.entry import Entry
from brain.core.index import queries
from brain.core.index.db import IndexDB
from brain.core.index.sync import Syncer, SyncReport
from brain.core.llm.models import LLM, LLMError
from brain.core.localstate import LocalState
from brain.core.pipeline.context import PipelineContext
from brain.core.pipeline.runner import Pipeline, ProcessReport
from brain.core.review import Review, ReviewDecision, ReviewItem
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
    entry: Entry
    key: str


class Busy(Exception):
    """Another brain process on this machine is already running the pipeline."""


@dataclass(frozen=True)
class FragmentChange:
    status: Status | None = None
    obe_reason: str | None = None
    obe_date: date | None = None
    state: OpenState | None = None
    closed_note: str | None = None
    dream: bool | None = None


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

    # --- entries -------------------------------------------------------------

    def jot(self, text: str, *, title: str | None = None, tags: list[str] | None = None) -> JotResult:
        """Save an entry. The text is kept in a local draft until the store confirms the write."""
        if not text.strip():
            raise ValueError("entry is empty")
        draft = self.state.drafts_dir / f"draft-{secrets.token_hex(4)}.md"
        draft.write_text(text, encoding="utf-8")
        try:
            entry, key = self.vault.save_entry(text, title=title, tags=tags)
        except Exception as e:
            raise JotFailed(draft, e) from e
        draft.unlink(missing_ok=True)
        return JotResult(entry, key)

    def pending_drafts(self) -> list[Path]:
        return sorted(self.state.drafts_dir.glob("draft-*.md"))

    # --- models and pipeline -------------------------------------------------

    @property
    def llm(self) -> LLM:
        if self._llm is None:
            self._llm = LLM(self.settings)
        return self._llm

    def _pipeline(self) -> Pipeline:
        if not self.llm.configured:
            raise LLMError("no model endpoint is configured; set [llm] base_url (run `brain init`)")
        ctx = PipelineContext(self.settings, self.vault, self.db, self.llm)
        return Pipeline(ctx, self.state)

    def process(
        self, entry_id: str, *, force: bool = False, progress: Callable[[str], None] | None = None
    ) -> ProcessReport:
        """Run the pipeline for one entry, then update the summaries of its threads."""
        with self.state.try_lock("process") as got:
            if not got:
                raise Busy("another brain process is processing entries; it will pick this one up")
            return self._pipeline().process(entry_id, force=force, progress=progress)

    def process_pending(self, progress: Callable[[str, str], None] | None = None) -> ProcessReport:
        self.sync()
        with self.state.try_lock("process") as got:
            if not got:
                raise Busy("another brain process is already processing entries")
            return self._pipeline().process_pending(progress)

    def reextract(
        self,
        entry_ids: list[str] | None = None,
        *,
        since: datetime | None = None,
        stale_prompts: bool = False,
        progress: Callable[[str, str], None] | None = None,
    ) -> ProcessReport:
        """Re-run extraction even for entries that are current (after a model or prompt change)."""
        self.sync()
        ids = entry_ids or self._select_entries(since=since, stale_prompts=stale_prompts)
        with self.state.try_lock("process") as got:
            if not got:
                raise Busy("another brain process is already processing entries")
            pipeline = self._pipeline()
            report = ProcessReport()
            for eid in ids:
                say = (lambda msg, e=eid: progress(e, msg)) if progress else None
                report.entries.append(pipeline.run_entry(eid, force=True, progress=say))
            report.threads, report.thread_errors = pipeline.summarize_stale(
                progress=(lambda msg: progress("", msg)) if progress else None
            )
            return report

    def _select_entries(self, *, since: datetime | None, stale_prompts: bool) -> list[str]:
        rows = self.db.conn.execute(
            "SELECT id, created_at FROM entries WHERE created_at >= ? ORDER BY created_at, id",
            ((since or datetime(1970, 1, 1, tzinfo=UTC)).isoformat(),),
        ).fetchall()
        ids = [r[0] for r in rows]
        if not stale_prompts:
            return ids
        from brain.core.formats.sidecars import Extraction
        from brain.core.llm.prompts import load_prompt

        current = load_prompt("extract").stamp
        stale = []
        for eid in ids:
            try:
                x = Extraction.parse(self.storage.get(keys.extraction_key(eid)), where=eid)
            except Exception:  # noqa: BLE001 - missing or unreadable: re-extract
                stale.append(eid)
                continue
            if x.prompt != current:
                stale.append(eid)
        return stale

    def failures(self) -> dict[str, dict]:
        from brain.core.pipeline.runner import FAILURES

        return self.state.read_json(FAILURES)

    # --- review --------------------------------------------------------------

    def review_items(self) -> list[ReviewItem]:
        return Review(self.vault, self.db.conn).items()

    def resolve_review(self, item_id: str, decision: ReviewDecision) -> None:
        Review(self.vault, self.db.conn).resolve(item_id, decision)

    def entity_choices(self, etype: str, text: str, *, limit: int = 8) -> list[queries.Candidate]:
        """Existing entities of a type whose names are close to `text` (for linking in review)."""
        return queries.fuzzy_candidates(self.db.conn, etype, text, limit=limit)

    # --- fragments -----------------------------------------------------------

    def set_fragment(self, fragment_id: str, change: FragmentChange) -> None:
        row = self.db.conn.execute("SELECT entry_id FROM fragments WHERE id = ?", (fragment_id,)).fetchone()
        if row is None:
            raise KeyError(f"no fragment {fragment_id}")
        d = self.vault.get_decisions(row[0])
        fd = d.fragments.get(fragment_id) or FragmentDecision()
        if change.status is not None:
            fd.status = change.status
            if change.status == "overcome_by_events":
                fd.obe = ObeInfo(date=change.obe_date or datetime.now().date(), reason=change.obe_reason)
            else:
                fd.obe = None
        if change.state is not None:
            fd.state = change.state
            fd.closed_note = change.closed_note if change.state == "closed" else None
        if change.dream is not None:
            fd.dream = change.dream
        d.fragments[fragment_id] = fd
        self.vault.put_decisions(d)

    # --- reading ---------------------------------------------------------------

    def active_threads(self, *, now: datetime | None = None) -> list[queries.ThreadRow]:
        return queries.active_threads(
            self.db.conn, now=now or datetime.now(UTC), inactivity_days=self.settings.threads.inactivity_days
        )

    def ask(self, question: str) -> answers.Answer:
        return answers.ask(self.db.conn, self.llm, question)

    def brief(self, target: str, *, since: datetime | None = None) -> answers.Brief:
        return answers.brief(self.db.conn, self.llm, self.vault, target, since=since)

    def find_targets(self, text: str) -> list[answers.NodeMatch]:
        return answers.find_targets(self.db.conn, text)
