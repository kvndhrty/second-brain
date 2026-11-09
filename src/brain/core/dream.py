"""Dreaming: an offline pass that re-checks derived notes against the entries.

Entries are the ground truth and are never edited here. A pass works through
the threads most likely to have changed and, for each, can:

1. rebuild the thread summary from all of its fragments, since incremental
   updates carry errors and omissions forward;
2. propose that an earlier fragment is overcome by events because a later one
   replaced or contradicted it;
3. propose answer links from later fragments to open questions and action
   items;
4. write the thread's one-line "Now" for the injected context.

It then checks old items in the OBE sweep and records a verdict for each.

Derived, regenerable output (summaries, Now lines, the run log, watermarks) is
written directly. Anything that changes what is recorded as true goes to the
review queue or the sweep as a proposal, and nothing applies until you accept
it. Each step is skipped when nothing has changed since its watermark, and a
pass stops after `[dream] max_calls` model calls; skipped work comes first next
time because the watermarks did not move.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from brain.core import keys
from brain.core.config import DreamConfig
from brain.core.formats.sidecars import (
    BriefState,
    DreamProposal,
    DreamProposals,
    DreamRun,
    DreamState,
    DreamThreadState,
    ThreadNow,
)
from brain.core.index import queries, search
from brain.core.index.queries import FragmentRow, ThreadRow
from brain.core.llm.models import LLMError
from brain.core.llm.schemas import AnswersOutput, NowOutput, SupersedeOutput, SweepOutput
from brain.core.pipeline import threads as threads_step
from brain.core.pipeline.context import PipelineContext
from brain.core.pipeline.text import attr, check_citations, fragment_el, thread_el
from brain.core.storage.base import StorageError
from brain.core.sweep import sweep_items
from brain.core.temporal import as_of, local_date, today_el

SUPERSEDE_FRAGMENTS = 80
SUPERSEDE_CONFIDENCE = 0.7
ANSWER_CONFIDENCE = 0.6
ANSWER_LATER = 60
NOW_FRAGMENTS = 15
NOW_MAX_WORDS = 35
SWEEP_BATCH = 8
SWEEP_LATER = 8
BRIEFED_RECENTLY_DAYS = 14

Progress = Callable[[str], None]


@dataclass
class Job:
    kind: str  # rebuild | supersede | answers | now | sweep
    thread_id: str | None = None
    label: str = ""
    fragments: list[str] = field(default_factory=list)  # sweep: the items to check


@dataclass
class DreamPlan:
    jobs: list[Job]
    threads: list[str]  # thread ids considered, in priority order
    only: str | None = None  # a single-thread pass (`--thread`), which leaves the schedule alone

    @property
    def calls(self) -> int:
        return len(self.jobs)  # each job is one model call


class _Budget(Exception):
    pass


def read_state(ctx: PipelineContext) -> DreamState:
    found = ctx.read_sidecar(keys.DREAM_STATE, DreamState)
    return found[0] if found else DreamState()


def last_run(ctx: PipelineContext) -> DreamRun | None:
    state = read_state(ctx)
    if not state.last_run:
        return None
    found = ctx.read_sidecar(keys.dream_run_key(state.last_run), DreamRun)
    return found[0] if found else None


def is_due(ctx: PipelineContext, cfg: DreamConfig, *, now: datetime) -> str | None:
    """Why a scheduled pass should run now, or None. Used by `brain start`."""
    if not cfg.enabled:
        return None
    state = read_state(ctx)
    entries = ctx.conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    if entries == 0:
        return None
    if state.unfinished:
        return "the last pass stopped at [dream] max_calls"
    if cfg.after_entries and entries - state.entries_at_last_run >= cfg.after_entries:
        return f"{entries - state.entries_at_last_run} new entries since the last dream"
    nightly = cfg.nightly_time()
    if nightly is not None:
        local = now.astimezone()
        # From the local wall time, so the slot keeps its hour across a DST change.
        slot = datetime.combine(local.date(), time(*nightly)).astimezone()
        if slot > local:
            slot = datetime.combine(local.date() - timedelta(days=1), time(*nightly)).astimezone()
        if state.last_run_at is None or state.last_run_at < slot:
            if entries > state.entries_at_last_run or state.last_run_at is None:
                return f"nightly pass ({cfg.nightly_at})"
    return None


class Dreamer:
    def __init__(self, ctx: PipelineContext, cfg: DreamConfig) -> None:
        self.ctx = ctx
        self.cfg = cfg
        self.conn: sqlite3.Connection = ctx.conn
        self.state = read_state(ctx)

    # --- planning ------------------------------------------------------------

    def _entries_since(self, nodes: list[str], through: str | None) -> int:
        """Distinct entries with fragments in these thread nodes after the watermark entry."""
        ph = ",".join("?" * len(nodes))
        after = ("", "")
        if through:
            d = queries.entry_dates(self.conn, [through]).get(through)
            after = (d[0], through) if d else ("", "")
        row = self.conn.execute(
            f"""
            SELECT COUNT(DISTINCT f.entry_id) FROM edges e JOIN fragments f ON e.src = 'frag:' || f.id
            WHERE e.type = 'in_thread' AND e.dst IN ({ph}) AND (f.created_at > ? OR (f.created_at = ? AND f.entry_id > ?))
            """,
            [*nodes, after[0], after[0], after[1]],
        ).fetchone()
        return row[0]

    def _briefed(self) -> dict[str, datetime]:
        found = self.ctx.read_sidecar(keys.BRIEF_STATE, BriefState)
        return found[0].last if found else {}

    def _ordered_threads(self, now: datetime, only: str | None) -> list[ThreadRow]:
        if only:
            t = queries.thread(self.conn, only)
            if t is None:
                raise KeyError(f"no thread {only}")
            return [t]
        briefed = self._briefed()
        cutoff = now - timedelta(days=BRIEFED_RECENTLY_DAYS)
        found = [t for t in queries.threads(self.conn, states=("active", "proposed")) if self._newest(t)]
        found.sort(key=lambda t: t.last_activity or "", reverse=True)  # newest activity first ...

        def recently_briefed(t: ThreadRow) -> bool:
            when = briefed.get(f"thread:{t.id}")
            return bool(when and when >= cutoff)

        found.sort(key=lambda t: (not t.pinned, not recently_briefed(t)))  # ... within pinned, then briefed
        return found

    def _newest(self, t: ThreadRow) -> str | None:
        """Newest entry in the thread or any thread merged into it: the watermark every step records."""
        nodes = queries.thread_nodes(self.conn, t.id)
        r = self.conn.execute(
            f"SELECT f.entry_id FROM edges e JOIN fragments f ON e.src = 'frag:' || f.id "
            f"WHERE e.type = 'in_thread' AND e.dst IN ({','.join('?' * len(nodes))}) "
            "ORDER BY f.created_at DESC, f.entry_id DESC LIMIT 1",
            nodes,
        ).fetchone()
        return r[0] if r else None

    def plan(self, *, thread: str | None = None, now: datetime | None = None) -> DreamPlan:
        now = now or self.ctx.clock()
        jobs: list[Job] = []
        threads = self._ordered_threads(now, thread)
        for t in threads:
            nodes = queries.thread_nodes(self.conn, t.id)
            ts = self.state.threads.get(t.id, DreamThreadState())
            since_rebuild = self._entries_since(nodes, ts.rebuilt_through)
            stale_rebuild = ts.rebuilt_at is not None and now - ts.rebuilt_at > timedelta(
                days=self.cfg.rebuild_after_days
            )
            if t.summary and (since_rebuild >= self.cfg.rebuild_after_updates or (stale_rebuild and since_rebuild)):
                jobs.append(Job("rebuild", t.id, f"rebuild the summary of {t.name}"))
            frags = self._thread_fragments(t.id)
            if len(frags) >= 2 and self._entries_since(nodes, ts.supersede_through):
                jobs.append(Job("supersede", t.id, f"look for superseded fragments in {t.name}"))
            if self._entries_since(nodes, ts.answers_through) and self._answer_inputs(frags)[0]:
                jobs.append(Job("answers", t.id, f"look for answers to open items in {t.name}"))
            if t.now_through != self._newest(t):
                jobs.append(Job("now", t.id, f"write the Now line for {t.name}"))
        if thread is None:
            cands = self._sweep_candidates(now)
            for i in range(0, len(cands), SWEEP_BATCH):
                batch = cands[i : i + SWEEP_BATCH]
                jobs.append(Job("sweep", None, f"check {len(batch)} old items for the sweep", batch))
        return DreamPlan(jobs, [t.id for t in threads], only=thread)

    def _thread_fragments(self, thread_id: str) -> list[FragmentRow]:
        return [f for f in queries.thread_fragments(self.conn, thread_id, limit=SUPERSEDE_FRAGMENTS) if not f.someday]

    def _answer_inputs(self, frags: list[FragmentRow]) -> tuple[list[FragmentRow], list[FragmentRow]]:
        """(open items, fragments written after the oldest of them) within one thread."""
        items = [f for f in frags if f.is_open]
        if not items:
            return [], []
        first = min((f.created_at, f.entry_id) for f in items)
        ids = {f.id for f in items}
        later = [f for f in frags if (f.created_at, f.entry_id) > first and f.id not in ids]
        return (items, later[-ANSWER_LATER:]) if later else ([], [])

    def _sweep_later(self, item: FragmentRow) -> list[FragmentRow]:
        """Fragments written after an item, in its threads or sharing its words; newest few, oldest first."""
        threads = [
            r[0]
            for r in self.conn.execute(
                "SELECT dst FROM edges WHERE src = ? AND type = 'in_thread'", (f"frag:{item.id}",)
            )
        ]
        family = sorted({n for t in threads for n in queries.node_family(self.conn, t)})
        found = {f.id: f for f in queries.fragments_linked_to(self.conn, family, ("in_thread",), limit=200)}
        hits = [h.item_id for h in search.keyword(self.conn, item.text, limit=20, types=("fragment",))]
        found.update(queries.fragments_by_id(self.conn, hits))
        later = [f for f in found.values() if (f.created_at, f.entry_id) > (item.created_at, item.entry_id)]
        later.sort(key=lambda f: (f.created_at, f.id))
        return later[-SWEEP_LATER:]

    def _sweep_candidates(self, now: datetime) -> list[str]:
        checked = {r[0]: r[1] for r in self.conn.execute("SELECT fragment_id, checked_through FROM suggestions")}
        out = []
        for item in sweep_items(self.conn, now=now):
            if item.fragment is None:
                continue
            row = queries.fragments_by_id(self.conn, [item.fragment.id]).get(item.fragment.id)
            if row is None or row.someday:
                continue
            later = self._sweep_later(row)
            if not later:
                continue  # nothing written since; there is nothing to judge it against
            if checked.get(item.fragment.id, "") == later[-1].entry_id:
                continue  # nothing new since the last verdict
            out.append(item.fragment.id)
        return out

    # --- running ---------------------------------------------------------------

    def run(self, plan: DreamPlan, *, progress: Progress | None = None) -> DreamRun:
        now = self.ctx.clock()
        say = progress or (lambda _: None)
        run = DreamRun(run_id=keys.basic_timestamp(now), started_at=now, max_calls=self.cfg.max_calls)
        self._run = run
        self._new: dict[str, list[DreamProposal]] = {}  # entry id -> new proposals
        # (kind, fragment or item id, thread) looked at this pass; a proposal is
        # replaced only by a pass over the same thread, which could have made it again.
        self._examined: set[tuple[str, str, str | None]] = set()
        self._affected: set[str] = set()  # entries whose proposals file may change
        handlers = {
            "rebuild": self._rebuild,
            "supersede": self._supersede,
            "answers": self._answers,
            "now": self._now,
            "sweep": self._sweep,
        }
        examined = set()
        for job in plan.jobs:
            if run.calls >= self.cfg.max_calls:
                run.budget_exhausted = True
                break
            if job.thread_id:
                examined.add(f"thread:{job.thread_id}")
            elif job.kind == "sweep":
                examined.add("sweep")
            say(job.label)
            try:
                handlers[job.kind](job, now)
            except _Budget:
                run.budget_exhausted = True
                break
            except (LLMError, StorageError) as e:
                run.errors.append(f"{job.label}: {e}")
        run.examined = sorted(examined)
        self._write_proposals(now)
        entries = self.conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        run.finished_at = self.ctx.clock()
        self.state.last_run = run.run_id
        if plan.only is None:  # a single-thread pass does not count as the scheduled one
            self.state.last_run_at = run.finished_at
            self.state.entries_at_last_run = entries
            self.state.unfinished = run.budget_exhausted
        self.ctx.write_sidecar(keys.dream_run_key(run.run_id), run)
        self.ctx.write_sidecar(keys.DREAM_STATE, self.state)
        return run

    def _call(self, prompt: str, user: str, output_type, validate):
        if self._run.calls >= self.cfg.max_calls:
            raise _Budget
        self._run.calls += 1
        result = self.ctx.llm.run(prompt, user, output_type, validate=validate)
        self._run.model = result.model
        return result

    def _thread_state(self, thread_id: str) -> DreamThreadState:
        return self.state.threads.setdefault(thread_id, DreamThreadState())

    # 1. summary rebuild

    def _rebuild(self, job: Job, now: datetime) -> None:
        t = queries.thread(self.conn, job.thread_id or "")
        if t is None:
            return
        if self._run.calls >= self.cfg.max_calls:
            raise _Budget
        summary = threads_step.summarize(self.ctx, t.id, full=True)
        if summary is not None:
            self._run.calls += 1
            self._run.model = summary.model
            self._run.applied.append(f"rebuilt the summary of thread {t.id}")
        ts = self._thread_state(t.id)
        ts.rebuilt_through = self._newest(t)
        ts.rebuilt_at = now

    # 2. supersession

    def _supersede(self, job: Job, now: datetime) -> None:
        t = queries.thread(self.conn, job.thread_id or "")
        if t is None:
            return
        today = local_date(now)
        frags = [f for f in self._thread_fragments(t.id) if f.status != "overcome_by_events"]
        by_id = {f.id: f for f in frags}
        lines = [today_el(now), thread_el(t), "<fragments>"]
        lines += [fragment_el(f, today) for f in frags]
        lines.append("</fragments>")

        def validate(out: SupersedeOutput) -> None:
            bad = [p for p in out.pairs if p.older not in by_id or p.newer not in by_id]
            if bad:
                raise ValueError(f"use only fragment ids from the input; not found: {bad[0].older} / {bad[0].newer}")
            backwards = [p for p in out.pairs if _order(by_id[p.newer]) <= _order(by_id[p.older])]
            if backwards:
                raise ValueError(f"newer must be written after older: {backwards[0].older} / {backwards[0].newer}")

        out = self._call("dream_supersede", "\n".join(lines), SupersedeOutput, validate).output
        newest = self._newest(t)
        user_set = _user_decided(self.conn, list(by_id))
        answered = _answer_pairs(self.conn)
        for f in frags:
            self._examined.add(("supersede", f.id, t.id))
            self._affected.add(f.entry_id)
        for p in out.pairs:
            old, new = by_id[p.older], by_id[p.newer]
            if p.confidence < SUPERSEDE_CONFIDENCE or old.id in user_set:
                continue
            if (new.id, old.id) in answered:
                continue  # a later fragment that answers an open item is an answer, not a replacement
            self._propose(
                old.entry_id,
                DreamProposal(
                    kind="supersede",
                    fragment=old.id,
                    other=new.id,
                    reason=p.reason,
                    confidence=round(p.confidence, 3),
                    citations=sorted({old.entry_id, new.entry_id}),
                    checked_through=newest,
                    thread=t.id,
                ),
            )
            self._run.proposed.append(f"{old.id} superseded by {new.id}")
        self._thread_state(t.id).supersede_through = newest

    # 3. answers

    def _answers(self, job: Job, now: datetime) -> None:
        t = queries.thread(self.conn, job.thread_id or "")
        if t is None:
            return
        today = local_date(now)
        items, later = self._answer_inputs(self._thread_fragments(t.id))
        if not items:
            return
        item_ids = {f.id: f for f in items}
        later_ids = {f.id: f for f in later}
        lines = [today_el(now), thread_el(t), "<open_items>"]
        lines += [fragment_el(f, today, tag="item", status=False) for f in items]
        lines += ["</open_items>", "<later_fragments>"]
        lines += [fragment_el(f, today, status=False) for f in later]
        lines.append("</later_fragments>")

        def validate(out: AnswersOutput) -> None:
            for link in out.links:
                if link.fragment not in later_ids or link.answers not in item_ids:
                    raise ValueError(f"use only ids from the input: {link.fragment} / {link.answers}")
                if _order(later_ids[link.fragment]) <= _order(item_ids[link.answers]):
                    raise ValueError(f"{link.fragment} was not written after {link.answers}")

        out = self._call("dream_answers", "\n".join(lines), AnswersOutput, validate).output
        newest = self._newest(t)
        for f in items:
            self._examined.add(("answer", f.id, t.id))
        self._affected.update(f.entry_id for f in later)
        for link in out.links:
            if link.confidence < ANSWER_CONFIDENCE:
                continue
            f = later_ids[link.fragment]
            fd = self.ctx.vault.get_decisions(f.entry_id).fragments.get(f.id)
            if fd and fd.decided_answer(link.answers):
                continue  # you already accepted or rejected this link
            self._propose(
                f.entry_id,
                DreamProposal(
                    kind="answer",
                    fragment=f.id,
                    other=link.answers,
                    confidence=round(link.confidence, 3),
                    citations=sorted({f.entry_id, item_ids[link.answers].entry_id}),
                    checked_through=newest,
                    thread=t.id,
                ),
            )
            self._run.proposed.append(f"{f.id} answers {link.answers}")
            self._drop_supersede(item_ids[link.answers].entry_id, link.answers, f.id)
        self._thread_state(t.id).answers_through = newest

    # 4. Now line

    def _now(self, job: Job, now: datetime) -> None:
        t = queries.thread(self.conn, job.thread_id or "")
        newest = self._newest(t) if t else None
        if t is None or newest is None:
            return
        today = local_date(now)
        frags = queries.thread_fragments(self.conn, t.id, limit=NOW_FRAGMENTS)
        allowed = {f.entry_id for f in frags}
        lines = [today_el(now), thread_el(t)]
        if t.summary:
            lines += [f"<thread_summary {as_of(t.summary_as_of, today)}>", t.summary, "</thread_summary>"]
        lines.append("<recent_fragments>")
        lines += [fragment_el(f, today, with_id=False) for f in frags]
        lines.append("</recent_fragments>")

        def validate(out: NowOutput) -> None:
            check_citations(out.citations, allowed, where="the fragments")
            if not out.line.strip():
                raise ValueError("the line is empty")

        result = self._call("dream_now", "\n".join(lines), NowOutput, validate)
        line = _clip_words(result.output.line, NOW_MAX_WORDS)
        self.ctx.write_sidecar(
            keys.thread_now_key(t.id),
            ThreadNow(
                thread_id=t.id,
                line=line,
                citations=sorted(set(result.output.citations)),
                through_entry=newest,
                model=result.model,
                prompt=result.prompt,
                created_at=self.ctx.clock(),
            ),
        )
        self._run.applied.append(f"wrote the Now line for thread {t.id}")

    # 5. sweep verdicts

    def _sweep(self, job: Job, now: datetime) -> None:
        today = local_date(now)
        items = queries.fragments_by_id(self.conn, job.fragments)
        lines = [today_el(now), "<items>"]
        later_for: dict[str, dict[str, FragmentRow]] = {}
        allowed: dict[str, set[str]] = {}
        for fid in job.fragments:
            item = items.get(fid)
            if item is None:
                continue
            later = self._sweep_later(item)
            if not later:
                continue
            later_for[fid] = {f.id: f for f in later}
            allowed[fid] = {item.entry_id} | {f.entry_id for f in later}
            lines.append(fragment_el(item, today, tag="item"))
            lines += [fragment_el(f, today, tag="later", status=False, extra=f' item="{attr(item.id)}"') for f in later]
        lines.append("</items>")
        if not later_for:
            return

        def validate(out: SweepOutput) -> None:
            for c in out.checks:
                if c.item not in later_for:
                    raise ValueError(f"unknown item {c.item}; use the ids of the items")
                if c.evidence and c.evidence not in later_for[c.item]:
                    raise ValueError(f"evidence {c.evidence} is not one of the later fragments for {c.item}")
                check_citations(c.citations, allowed[c.item], where=f"the input for {c.item}")

        out = self._call("dream_sweep", "\n".join(lines), SweepOutput, validate).output
        for fid in later_for:
            self._examined.add(("sweep", fid, None))
            self._affected.add(items[fid].entry_id)
        for c in out.checks:
            later = later_for[c.item]
            newest = max(later.values(), key=_order).entry_id
            self._propose(
                items[c.item].entry_id,
                DreamProposal(
                    kind="sweep",
                    fragment=c.item,
                    other=c.evidence,
                    verdict=c.verdict,
                    reason=c.reason,
                    confidence=round(c.confidence, 3),
                    citations=sorted(set(c.citations) or {items[c.item].entry_id}),
                    checked_through=newest,
                ),
            )
            if c.verdict != "still_open":
                self._run.proposed.append(f"sweep: {c.item} {c.verdict}")

    # --- proposal files ----------------------------------------------------------

    def _propose(self, entry_id: str, p: DreamProposal) -> None:
        self._new.setdefault(entry_id, []).append(p)
        self._affected.add(entry_id)

    def _drop_supersede(self, entry_id: str, fragment: str, other: str) -> None:
        """Remove a supersede proposal from this pass for a pair that the answers step linked."""
        props = self._new.get(entry_id, [])
        keep = [p for p in props if not (p.kind == "supersede" and p.fragment == fragment and p.other == other)]
        if len(keep) != len(props):
            self._new[entry_id] = keep
            self._run.proposed.remove(f"{fragment} superseded by {other}")

    def _stale(self, p: DreamProposal) -> bool:
        """True if this pass looked at the same thing again, so its new result replaces p."""
        key = p.other if p.kind == "answer" else p.fragment
        return (p.kind, key or "", p.thread) in self._examined

    def _write_proposals(self, now: datetime) -> None:
        for entry_id in sorted(self._affected):
            key = keys.dream_proposals_key(entry_id)
            found = self.ctx.read_sidecar(key, DreamProposals)
            old = found[0].proposals if found else []
            keep = [p for p in old if not self._stale(p)]
            proposals = keep + self._new.get(entry_id, [])
            if proposals == old:
                continue
            if not proposals:
                if found:
                    self.ctx.vault.delete(key)
                continue
            self.ctx.write_sidecar(
                key,
                DreamProposals(
                    entry_id=entry_id,
                    run_id=self._run.run_id,
                    model=self._run.model or "none",
                    created_at=now,
                    proposals=proposals,
                ),
            )


def _order(f: FragmentRow) -> tuple[str, str]:
    return (f.created_at, f.entry_id)


def _user_decided(conn: sqlite3.Connection, ids: list[str]) -> set[str]:
    if not ids:
        return set()
    rows = conn.execute(
        f"SELECT id FROM fragments WHERE status_origin = 'user' AND id IN ({','.join('?' * len(ids))})", ids
    )
    return {r[0] for r in rows}


def _clip_words(text: str, limit: int) -> str:
    """At most `limit` words, cut at the last sentence or clause end that fits, else at a word with "…"."""
    words = text.split()
    if len(words) <= limit:
        return " ".join(words)
    for ends in (".!?", ";", ","):
        cut = max((i for i, w in enumerate(words[:limit]) if w[-1] in ends), default=-1)
        if cut >= limit // 2:
            return " ".join(words[: cut + 1]).rstrip(",;") + ("" if ends == ".!?" else ".")
    return " ".join(words[:limit]).rstrip(",;.") + "…"


def _answer_pairs(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """(answering fragment, answered item) for every accepted answer link and every pending proposal."""
    pairs = {
        (src.removeprefix("frag:"), dst.removeprefix("frag:"))
        for src, dst in conn.execute("SELECT src, dst FROM edges WHERE type = 'answers'")
    }
    for (payload,) in conn.execute("SELECT payload FROM review_items WHERE kind = 'proposed_answer'"):
        p = json.loads(payload)
        pairs.add((p["fragment"], p["answers"]))
    return pairs


def suggestions(conn: sqlite3.Connection) -> dict[str, dict]:
    """fragment id -> dream verdict, for the sweep."""
    return {
        r[0]: {"verdict": r[1], "evidence": r[2], "reason": r[3], "citations": json.loads(r[4])}
        for r in conn.execute("SELECT fragment_id, verdict, evidence, reason, citations FROM suggestions")
    }
