"""Run the layers for one note, or for every note that is pending.

Each layer writes one file that records a hash of its input and the versions
of its prompts, so running again does only the work that is missing: the
pipeline is idempotent and can be interrupted at any point. A failure (the
endpoint is down, the store cannot be written) leaves the note saved and its
remaining layers pending; the error is kept in local state for `brain
doctor`. A model that answers but never validly does not fail the note: the
layer takes its safe result (see `Calls`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from brain.core import keys
from brain.core.formats.note import Note
from brain.core.formats.records import (
    LAYER_MODELS,
    ClassifyFile,
    EmbeddingFile,
    FilingFile,
    LayerFile,
    LinksFile,
    NamesFile,
    SplitFile,
    StateFile,
    bytes_hash,
)
from brain.core.formats.yamlio import FormatError
from brain.core.index import queries
from brain.core.llm.models import LLMError
from brain.core.localstate import LocalState
from brain.core.pipeline import embed as embed_step
from brain.core.pipeline import filing, layers, pages, state, wiki
from brain.core.pipeline import link as link_layer
from brain.core.pipeline.context import Calls, PipelineContext, stamps
from brain.core.storage.base import StorageError
from brain.core.temporal import local_date

FAILURES = "pipeline-failures.json"
Progress = Callable[[str], None]


@dataclass
class NoteResult:
    note_id: str
    layers: list[str] = field(default_factory=list)  # layers that ran (others were current)
    calls: int = 0
    pages: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class ProcessReport:
    notes: list[NoteResult] = field(default_factory=list)

    @property
    def failed(self) -> list[NoteResult]:
        return [n for n in self.notes if n.error]


class Pipeline:
    def __init__(self, ctx: PipelineContext, local: LocalState) -> None:
        self.ctx = ctx
        self.local = local

    def _note_failure(self, note_id: str, step: str, error: str | None) -> None:
        failures = self.local.read_json(FAILURES)
        if error is None:
            if note_id not in failures:
                return
            failures.pop(note_id)
        else:
            failures[note_id] = {"step": step, "error": error, "at": datetime.now(UTC).isoformat()}
        self.local.write_json(FAILURES, failures)

    def _layer(
        self,
        result: NoteResult,
        note: Note,
        name: str,
        input_hash: str,
        prompts: tuple[str, ...],
        run: Callable[[Calls], dict[str, Any]],
        *,
        force: bool,
    ) -> tuple[LayerFile, bytes]:
        """Reuse a layer's file when its input and prompts are unchanged; otherwise run the layer and write it."""
        ctx = self.ctx
        key = keys.layer_key(note.id, name)
        want = stamps(prompts)
        found = ctx.read(key, LAYER_MODELS[name])
        if found and not force and found[0].input_hash == input_hash and found[0].prompts == want:
            return found
        calls = Calls(ctx.llm)
        data = run(calls)
        record = LAYER_MODELS[name](
            note_id=note.id,
            input_hash=input_hash,
            model=calls.model,
            prompts=want,
            created_at=ctx.clock(),
            events=calls.events,
            **data,
        )
        result.layers.append(name)
        result.calls += calls.count
        return record, ctx.write(key, record)

    def run_note(self, note_id: str, *, force: bool = False, progress: Progress | None = None) -> NoteResult:
        """Run whatever layers this note still needs, then update the pages it touched."""
        ctx = self.ctx
        say = progress or (lambda _: None)
        result = NoteResult(note_id)
        step = "load"
        try:
            note, _ = ctx.vault.get_note(note_id, queries.note_key(ctx.conn, note_id))
            now = ctx.clock()
            today = local_date(now)

            step = "split"
            say("splitting into statements")
            split, split_b = self._layer(
                result, note, "split", note.content_hash, layers.SPLIT_PROMPTS,
                lambda c: {"statements": layers.split(c, note, today)}, force=force,
            )  # fmt: skip
            assert isinstance(split, SplitFile)

            step = "classify"
            say("labelling statements")
            classify, classify_b = self._layer(
                result, note, "classify", bytes_hash(split_b), layers.CLASSIFY_PROMPTS,
                lambda c: {"kinds": layers.classify(c, note, split.statements, now)}, force=force,
            )  # fmt: skip
            assert isinstance(classify, ClassifyFile)

            step = "names"
            say("finding names")
            names, names_b = self._layer(
                result, note, "names", note.content_hash, layers.NAMES_PROMPTS,
                lambda c: {"names": layers.names(c, note, today)}, force=force,
            )  # fmt: skip
            assert isinstance(names, NamesFile)

            if ctx.llm.embed_model:
                step = "embed"
                ekey = keys.embedding_key(ctx.llm.embed_model, note_id)
                prev = ctx.read(ekey, EmbeddingFile)
                texts = embed_step.needed_texts(note, split.statements)
                if not embed_step.is_current(prev[0] if prev else None, texts):
                    say("embedding")
                    ctx.write(ekey, embed_step.embed(ctx, note, texts, prev[0] if prev else None))
                    result.layers.append("embed")

            step = "links"
            say("linking names")
            links, links_b = self._layer(
                result, note, "links", bytes_hash(names_b), link_layer.PROMPTS,
                lambda c: {"links": link_layer.link(ctx, c, note, names.names, today)}, force=force,
            )  # fmt: skip
            assert isinstance(links, LinksFile)

            step = "filing"
            say("filing under threads")
            filed, filed_b = self._layer(
                result, note, "filing", bytes_hash(split_b, classify_b, links_b), filing.PROMPTS,
                lambda c: {"threads": filing.file(ctx, c, note, split.statements, classify.kinds, now)}, force=force,
            )  # fmt: skip
            assert isinstance(filed, FilingFile)

            step = "state"
            say("checking what this changes")
            linked = [ln.entity for ln in links.links]
            st, _ = self._layer(
                result, note, "state", bytes_hash(split_b, classify_b, links_b, filed_b), state.PROMPTS,
                lambda c: {
                    "relations": state.relate(ctx, c, note, now),
                    "operations": state.corrections(ctx, c, note, linked, now),
                },
                force=force,
            )  # fmt: skip
            assert isinstance(st, StateFile)

            step = "pages"
            touched = self.touched(note_id, links, filed, st)
            say("updating pages")
            result.pages, calls = self.update_pages(touched, now)
            result.calls += calls
        except (LLMError, StorageError, FormatError) as e:
            result.error = f"{step}: {e}"
            self._note_failure(note_id, step, str(e))
            return result
        self._note_failure(note_id, step, None)
        return result

    def touched(self, note_id: str, links: LinksFile, filed: FilingFile, st: StateFile) -> set[str]:
        """Surviving entities whose pages this note can have changed."""
        world = self.ctx.world()
        ids: set[str | None] = {ln.entity for ln in links.links}
        ids |= set(filed.threads.values())
        affected = [r.dst for r in st.relations]
        for op in st.operations:
            if op.op == "retract":
                affected += op.targets
            else:
                ids |= set(op.targets)
            affected += op.moved
        if affected:
            for s in world.by_id(affected).values():
                ids.add(s.thread)
            for ents in world.entities_of(affected).values():
                ids |= set(ents)
        if (about := world.get(queries_about(self.ctx, note_id))) is not None:
            ids.add(about.id)
        # Include the entities themselves (a merged one loses its wiki file) and what they merged into.
        return {i for i in ids if i} | {r for i in ids if (r := world.root(i))}

    def update_pages(self, entity_ids: set[str], now: datetime) -> tuple[list[str], int]:
        calls = Calls(self.ctx.llm)
        changed = [eid for eid in sorted(entity_ids) if pages.update(self.ctx, calls, eid, now)]
        wiki.write(self.ctx, entity_ids, now)
        return changed, calls.count

    def process_pending(self, progress: Callable[[str, str], None] | None = None) -> ProcessReport:
        report = ProcessReport()
        for note_id in queries.pending_notes(self.ctx.conn):
            say = (lambda msg, n=note_id: progress(n, msg)) if progress else None
            report.notes.append(self.run_note(note_id, progress=say))
        return report


def queries_about(ctx: PipelineContext, note_id: str) -> str | None:
    row = ctx.conn.execute("SELECT about FROM notes WHERE id = ?", (note_id,)).fetchone()
    return row[0] if row else None
