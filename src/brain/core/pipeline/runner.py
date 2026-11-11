"""Run the layers for one note, or for every note that is pending.

Each layer writes one file that records a hash of its input and the versions
of its prompts, so running again does only the work that is missing and the
pipeline can be interrupted at any point. The last file, `pages`, is written
once the note's pages and wiki files are up to date; a note is pending until
it has that file.

Notes are processed in date order, and a note is never processed while an
earlier one is pending: what a note answers, replaces, or corrects depends on
the notes before it. So a failure (the endpoint is down, the store cannot be
written) stops the run at that note. The note stays saved, the error is kept
in local state, and the note is tried again later, with a longer wait after
each failure. `Pipeline.skip` leaves a note out for good.

A model that answers but never validly does not fail the note: the layer
takes its safe result (see `Calls`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from brain.core import keys
from brain.core.formats.note import Note
from brain.core.formats.records import (
    LAYER_MODELS,
    ClassifyFile,
    EmbeddingFile,
    Event,
    FilingFile,
    LayerFile,
    LinksFile,
    NamesFile,
    PagesFile,
    SplitFile,
    StateFile,
    bytes_hash,
)
from brain.core.index import queries
from brain.core.localstate import LocalState
from brain.core.pipeline import embed as embed_step
from brain.core.pipeline import filing, layers, pages, state, wiki
from brain.core.pipeline import link as link_layer
from brain.core.pipeline.context import Calls, PipelineContext, stamps
from brain.core.temporal import local_date, utc_iso

FAILURES = "pipeline-failures.json"
RETRY_FIRST = 60  # seconds before a failed note is tried again; doubles with each failure
RETRY_MAX = 1800
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
    notes: list[NoteResult] = field(default_factory=list)  # notes tried, in date order; only the last can have failed
    waiting: int = 0  # notes still pending when the run stopped
    held: str | None = None  # a note that failed earlier and is not due to be tried again yet

    @property
    def failed(self) -> list[NoteResult]:
        return [n for n in self.notes if n.error]

    def result(self, note_id: str) -> NoteResult:
        """What happened to one note. If the run stopped before reaching it, the error says what it waits for."""
        for n in self.notes:
            if n.note_id == note_id:
                return n
        if self.failed:
            why = f"it waits for an earlier note that failed ({self.failed[-1].note_id}: {self.failed[-1].error})"
        elif self.held:
            why = f"it waits for an earlier note that failed and will be tried again ({self.held})"
        else:
            return NoteResult(note_id)
        return NoteResult(note_id, error=f"saved, not processed yet: {why}")


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
            attempts = int(failures.get(note_id, {}).get("attempts", 0)) + 1
            at = datetime.now(UTC)
            wait = min(RETRY_FIRST * 2 ** (attempts - 1), RETRY_MAX)
            failures[note_id] = {
                "step": step,
                "error": error,
                "at": at.isoformat(),
                "attempts": attempts,
                "next_try": (at + timedelta(seconds=wait)).isoformat(),
            }
        self.local.write_json(FAILURES, failures)

    def _not_due(self, note_id: str) -> bool:
        next_try = self.local.read_json(FAILURES).get(note_id, {}).get("next_try")
        return bool(next_try) and datetime.fromisoformat(next_try) > datetime.now(UTC)

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
            st, st_b = self._layer(
                result, note, "state", bytes_hash(split_b, classify_b, links_b, filed_b), state.PROMPTS,
                lambda c: {
                    "relations": state.relate(ctx, c, note, now),
                    "operations": state.corrections(ctx, c, note, linked, now),
                },
                force=force,
            )  # fmt: skip
            assert isinstance(st, StateFile)

            step = "pages"
            say("updating pages")

            def write_pages(c: Calls) -> dict[str, Any]:
                touched = self.touched(note_id, links, filed, st)
                changed = [eid for eid in sorted(touched) if pages.update(ctx, c, eid, now)]
                wiki.write(ctx, touched, now)
                late = ctx.conn.execute(
                    "SELECT COUNT(*) FROM notes WHERE done = 1 AND created_at > ? AND id != ?",
                    (utc_iso(note.created), note_id),
                ).fetchone()[0]
                if late:
                    c.log(
                        "out_of_order",
                        f"This note is dated before {late} note(s) that were already processed. What those notes "
                        "answer, replace, or correct was worked out without it. `brain rebuild` applies every "
                        "note in date order.",
                    )
                return {"pages": changed}

            written, _ = self._layer(
                result, note, "pages", bytes_hash(split_b, classify_b, links_b, filed_b, st_b), (), write_pages,
                force=force,
            )  # fmt: skip
            assert isinstance(written, PagesFile)
            result.pages = written.pages
        except Exception as e:  # noqa: BLE001 - any failure is this note's; it is recorded and tried again
            result.error = f"{step}: {e}"
            self._note_failure(note_id, step, str(e))
            return result
        self._note_failure(note_id, step, None)
        return result

    def skip(self, note_id: str) -> None:
        """Leave a note out of the wiki, so the notes after it are not held up.

        It is recorded as processed with no statements. `brain rebuild` tries it again.
        """
        ctx = self.ctx
        note, _ = ctx.vault.get_note(note_id, queries.note_key(ctx.conn, note_id))
        failure = self.local.read_json(FAILURES).get(note_id)
        why = f" It was failing at {failure['step']}: {failure['error']}" if failure else ""
        said = Event(kind="skipped", text=f"This note was left out of the wiki.{why} `brain rebuild` tries it again.")
        for name in keys.LAYERS:
            first = name == keys.LAYERS[0]
            record = LAYER_MODELS[name](
                note_id=note.id,
                input_hash=note.content_hash if first else "skipped",
                created_at=ctx.clock(),
                events=[said] if first else [],
            )
            ctx.write(keys.layer_key(note.id, name), record)
        self._note_failure(note_id, "", None)

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

    def process_pending(
        self,
        progress: Callable[[str, str], None] | None = None,
        *,
        through: str | None = None,
        wait_for_retry: bool = False,
    ) -> ProcessReport:
        """Process pending notes in date order, stopping at the first that fails.

        `through`: stop after this note. `wait_for_retry`: do not try a note
        that failed recently until its wait is over (the background worker).
        """
        report = ProcessReport()
        pending = queries.pending_notes(self.ctx.conn)
        todo = pending
        if through is not None:
            todo = pending[: pending.index(through) + 1] if through in pending else []
        for note_id in todo:
            if wait_for_retry and self._not_due(note_id):
                report.held = note_id
                break
            say = (lambda msg, n=note_id: progress(n, msg)) if progress else None
            result = self.run_note(note_id, progress=say)
            report.notes.append(result)
            if result.error:
                break
        report.waiting = len(pending) - sum(1 for n in report.notes if not n.error)
        return report


def queries_about(ctx: PipelineContext, note_id: str) -> str | None:
    row = ctx.conn.execute("SELECT about FROM notes WHERE id = ?", (note_id,)).fetchone()
    return row[0] if row else None
