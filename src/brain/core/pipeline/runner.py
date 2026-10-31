"""Run the pipeline for one entry, or for everything that is pending.

Each step writes one sidecar that records the exact version of its input, so
running again does only the work that is missing: the pipeline is idempotent
and can be interrupted at any point. A failure leaves the entry saved and its
remaining steps pending; the error is kept in local state for `brain doctor`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from brain.core import keys
from brain.core.formats.entry import content_hash
from brain.core.formats.sidecars import EmbeddingFile, Extraction, ResolutionFile, bytes_hash
from brain.core.formats.yamlio import FormatError
from brain.core.index import queries
from brain.core.llm.models import LLMError
from brain.core.localstate import LocalState
from brain.core.pipeline import embed as embed_step
from brain.core.pipeline import extract as extract_step
from brain.core.pipeline import resolve as resolve_step
from brain.core.pipeline import threads as threads_step
from brain.core.pipeline.context import PipelineContext
from brain.core.storage.base import StorageError

FAILURES = "pipeline-failures.json"
Progress = Callable[[str], None]


@dataclass
class EntryResult:
    entry_id: str
    steps: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class ProcessReport:
    entries: list[EntryResult] = field(default_factory=list)
    threads: list[str] = field(default_factory=list)
    thread_errors: dict[str, str] = field(default_factory=dict)

    @property
    def failed(self) -> list[EntryResult]:
        return [e for e in self.entries if e.error]


class Pipeline:
    def __init__(self, ctx: PipelineContext, state: LocalState) -> None:
        self.ctx = ctx
        self.state = state

    def _note_failure(self, entry_id: str, step: str, error: str | None) -> None:
        failures = self.state.read_json(FAILURES)
        if error is None:
            failures.pop(entry_id, None)
        else:
            failures[entry_id] = {"step": step, "error": error, "at": datetime.now(UTC).isoformat()}
        self.state.write_json(FAILURES, failures)

    def failures(self) -> dict[str, dict]:
        return self.state.read_json(FAILURES)

    def run_entry(self, entry_id: str, *, force: bool = False, progress: Progress | None = None) -> EntryResult:
        """Run whatever steps this entry still needs. force re-extracts even if current."""
        ctx = self.ctx
        say = progress or (lambda _: None)
        result = EntryResult(entry_id)
        step = "load"
        try:
            entry, _ = ctx.vault.get_entry(entry_id, queries.entry_key(ctx.conn, entry_id))
            decisions = ctx.vault.get_decisions(entry_id)

            step = "extract"
            prev = ctx.read_sidecar(keys.extraction_key(entry_id), Extraction)
            if force or prev is None or prev[0].entry_hash != entry.content_hash:
                say("extracting fragments")
                extraction = extract_step.extract(ctx, entry, prev[0] if prev else None)
                x_bytes = ctx.write_sidecar(keys.extraction_key(entry_id), extraction)
                result.steps.append("extract")
            else:
                extraction, x_bytes = prev

            embedding: EmbeddingFile | None = None
            if ctx.llm.embed_model:
                step = "embed"
                ekey = keys.embedding_key(ctx.llm.embed_model, entry_id)
                prev_e = ctx.read_sidecar(ekey, EmbeddingFile)
                needed = embed_step.needed_texts(entry, extraction, decisions)
                have = {i.id: i.hash for i in prev_e[0].items} if prev_e else {}
                if prev_e and all(have.get(i) == content_hash(t) for i, t in needed.items()):
                    embedding = prev_e[0]
                else:
                    say("embedding")
                    embedding = embed_step.embed(ctx, entry, extraction, decisions, prev_e[0] if prev_e else None)
                    ctx.write_sidecar(ekey, embedding)
                    result.steps.append("embed")

            step = "resolve"
            prev_r = ctx.read_sidecar(keys.resolution_key(entry_id), ResolutionFile)
            if force or prev_r is None or prev_r[0].extraction_hash != bytes_hash(x_bytes):
                say("resolving people, orgs, themes, and threads")
                resolution = resolve_step.resolve(ctx, entry, extraction, x_bytes, decisions, embedding)
                ctx.write_sidecar(keys.resolution_key(entry_id), resolution)
                result.steps.append("resolve")
        except (LLMError, StorageError, FormatError) as e:
            result.error = f"{step}: {e}"
            self._note_failure(entry_id, step, str(e))
            return result
        self._note_failure(entry_id, step, None)
        return result

    def touched_threads(self, entry_id: str) -> list[str]:
        rows = self.ctx.conn.execute(
            "SELECT DISTINCT substr(e.dst, 8) FROM edges e JOIN fragments f ON e.src = 'frag:' || f.id "
            "WHERE e.type = 'in_thread' AND f.entry_id = ? ORDER BY 1",
            (entry_id,),
        ).fetchall()
        return [r[0] for r in rows]

    def summarize_stale(
        self, only: list[str] | None = None, progress: Progress | None = None
    ) -> tuple[list[str], dict[str, str]]:
        done, errors = [], {}
        for t in queries.stale_threads(self.ctx.conn):
            if only is not None and t.id not in only:
                continue
            if progress:
                progress(f"summarizing thread {t.name}")
            try:
                if threads_step.summarize(self.ctx, t.id):
                    done.append(t.id)
            except (LLMError, StorageError) as e:
                errors[t.id] = str(e)
        return done, errors

    def process(self, entry_id: str, *, force: bool = False, progress: Progress | None = None) -> ProcessReport:
        """One entry, then the summaries of the threads it touches."""
        report = ProcessReport([self.run_entry(entry_id, force=force, progress=progress)])
        if not report.entries[0].error:
            report.threads, report.thread_errors = self.summarize_stale(self.touched_threads(entry_id), progress)
        return report

    def process_pending(self, progress: Callable[[str, str], None] | None = None) -> ProcessReport:
        report = ProcessReport()
        for entry_id, _key, _step in queries.pending_entries(self.ctx.conn):
            say = (lambda msg, e=entry_id: progress(e, msg)) if progress else None
            report.entries.append(self.run_entry(entry_id, progress=say))
        report.threads, report.thread_errors = self.summarize_stale(
            progress=(lambda msg: progress("", msg)) if progress else None
        )
        return report
