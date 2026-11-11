"""Dreaming (layer 9): an offline pass that looks across notes.

The layers that run when a note arrives see one note at a time. Dreaming
looks at the whole journal and does what they cannot:

- duplicates: pairs of entities with similar names are compared, and merged
  when the statements about them show they are the same;
- stale items: open questions and things to do older than
  `[dream] stale_days` are checked against what was written since;
- quiet threads: a thread with no open items and no activity for
  `[dream] close_after_days` is closed (new activity reopens it);
- profiles: a person's role and organization are filled in from the
  statements that mention them;
- rebuilds: summaries that have been updated many times, or not rebuilt for
  a long while, are rebuilt from all their statements.

Its calls have the same closed form as the other layers. It runs on the
strong model when `[llm] dream_uses = "strong"` and one is set. It never
overrides an operation that came from a note: a "not the same" instruction
stops a merge, and a thread closed or reopened by a note is left alone.
Everything it does goes in the change log and can be reversed by a note.

A pass stops after `[dream] max_calls` model calls; what it did not reach is
looked at first next time, because checked work is remembered in
`generated/dreams/state.json`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from brain.core import keys
from brain.core.config import DreamConfig
from brain.core.formats.records import (
    ENTITY_TYPES,
    DreamRun,
    DreamState,
    EntityRecord,
    PageFile,
    Relation,
    bytes_hash,
)
from brain.core.index import queries
from brain.core.index.queries import CONTENT_KINDS, Ent, Stmt, World
from brain.core.llm.models import LLMError
from brain.core.llm.schemas import ProfileOut, SameOut, StaleOut
from brain.core.pipeline import pages, wiki
from brain.core.pipeline.context import Calls, PipelineContext
from brain.core.pipeline.state import _dot
from brain.core.pipeline.text import attr, stmt_el
from brain.core.storage.base import StorageError
from brain.core.temporal import last_mentioned, local_date, today_el, utc_iso

PROMPTS = ("dream_same", "dream_stale", "dream_profile")
MAX_PAIRS = 40
# Name similarity above which two entities are compared. A shared whole word (a first name) is enough.
SIMILAR_NAMES = 0.65
MAX_PROFILES = 15
LATER_STATEMENTS = 6
RECORD_STATEMENTS = 6
PROFILE_STATEMENTS = 20
Progress = Callable[[str], None]


@dataclass
class DreamPlan:
    """What a pass would look at, found without calling a model."""

    duplicates: list[tuple[str, str]] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    close: list[str] = field(default_factory=list)
    profiles: list[str] = field(default_factory=list)
    rebuilds: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.duplicates or self.stale or self.close or self.profiles or self.rebuilds)


def read_state(ctx: PipelineContext) -> DreamState:
    found = ctx.read(keys.DREAM_STATE, DreamState)
    return found[0] if found else DreamState()


def last_run(ctx: PipelineContext) -> DreamRun | None:
    state = read_state(ctx)
    if not state.last_run:
        return None
    found = ctx.read(keys.dream_run_key(state.last_run), DreamRun)
    return found[0] if found else None


def is_due(ctx: PipelineContext, cfg: DreamConfig, *, now: datetime) -> str | None:
    """Why a scheduled pass should run now, or None. Used by `brain start`."""
    if not cfg.enabled:
        return None
    state = read_state(ctx)
    notes = ctx.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    if notes == 0:
        return None
    if cfg.after_notes and notes - state.notes_at_last_run >= cfg.after_notes:
        return f"{notes - state.notes_at_last_run} new notes since the last dream"
    nightly = cfg.nightly_time()
    if nightly is not None:
        local = now.astimezone()
        # From the local wall time, so the slot keeps its hour across a DST change.
        slot = datetime.combine(local.date(), time(*nightly)).astimezone()
        if slot > local:
            slot = datetime.combine(local.date() - timedelta(days=1), time(*nightly)).astimezone()
        if state.last_run_at is None or state.last_run_at < slot:
            if notes > state.notes_at_last_run or state.last_run_at is None:
                return f"nightly pass ({cfg.nightly_at})"
    return None


def _pair_key(a: str, b: str) -> str:
    return "|".join(sorted((a, b)))


def _kept_apart(world: World, a: str, b: str) -> bool:
    """True if a note said any page merged into `a` is not the same as any page merged into `b`."""
    in_a, in_b = set(world.members(a)), set(world.members(b))
    return any(other in in_b for m in in_a for other in world.entities[m].record.not_same_as) or any(
        other in in_a for m in in_b for other in world.entities[m].record.not_same_as
    )


class Dreamer:
    def __init__(self, ctx: PipelineContext, cfg: DreamConfig) -> None:
        self.ctx = ctx
        self.cfg = cfg
        self.state = read_state(ctx)

    # --- planning ------------------------------------------------------------

    def _pair_sig(self, world: World, a: str, b: str) -> str:
        return bytes_hash(*[f"{'/'.join(world.names_of(x))}:{world.note_count(x)}" for x in sorted((a, b))])

    def _stale_sig(self, later: list[Stmt]) -> str:
        return later[-1].id if later else "-"

    def _later(self, world: World, item: Stmt) -> list[Stmt]:
        """Later statements that could settle an open item: same thread or entities, nearest in meaning first."""
        pool: dict[str, Stmt] = {}
        related = ([item.thread] if item.thread else []) + world.entities_of([item.id])[item.id]
        for e in related:
            for s in world.of_entity(e):
                if s.created_at > item.created_at and s.is_current and s.kind in CONTENT_KINDS and s.kind != "question":
                    pool[s.id] = s
        cands = list(pool.values())
        vecs = queries.vectors(self.ctx.conn, [item.id, *pool])
        mine = vecs.get(item.id)
        if mine is not None:
            cands.sort(key=lambda s: -_dot(mine, vecs[s.id]) if s.id in vecs else 0.0)
        return sorted(cands[:LATER_STATEMENTS], key=lambda s: (s.created_at, s.id))

    def plan(self, *, now: datetime | None = None) -> DreamPlan:
        now = now or self.ctx.clock()
        world = self.ctx.world()
        plan = DreamPlan()
        seen: set[str] = set()
        for etype in ENTITY_TYPES:
            for e in world.roots(etype):
                for other in world.similar(e.name, etype, limit=4, floor=SIMILAR_NAMES):
                    key = _pair_key(e.id, other.id)
                    if other.id == e.id or key in seen:
                        continue
                    seen.add(key)
                    if _kept_apart(world, e.id, other.id):
                        continue
                    if self.state.checked_pairs.get(key) == self._pair_sig(world, e.id, other.id):
                        continue
                    plan.duplicates.append((e.id, other.id))
        plan.duplicates = plan.duplicates[:MAX_PAIRS]

        cutoff = utc_iso(now - timedelta(days=self.cfg.stale_days))
        for item in world.open_items():
            if item.created_at > cutoff:
                continue
            later = self._later(world, item)
            if later and self.state.checked_items.get(item.id) != self._stale_sig(later):
                plan.stale.append(item.id)

        quiet = utc_iso(now - timedelta(days=self.cfg.close_after_days))
        open_threads = {s.thread for s in world.open_items()}
        for t in world.roots("thread", with_provisional=False):
            last = world.last_activity(t.id)
            if t.status == "active" and t.id not in open_threads and last and last < quiet:
                if not t.record.note_set("status"):
                    plan.close.append(t.id)

        for p in world.roots("person", with_provisional=False):
            stmts = [s for s in world.of_entity(p.id) if s.is_current]
            if len(stmts) >= 2 and self.state.profiles.get(p.id) != bytes_hash(*[s.id for s in stmts]):
                plan.profiles.append(p.id)
        plan.profiles = plan.profiles[:MAX_PROFILES]

        old = now - timedelta(days=self.cfg.rebuild_after_days)
        for entity_id, data in self.ctx.conn.execute("SELECT entity_id, data FROM pages"):
            e = world.entities.get(entity_id)
            summary = PageFile.model_validate_json(data).sections.get("summary")
            if e is None or e.merged_into or summary is None:
                continue
            aged = summary.rebuilt_at is not None and summary.rebuilt_at < old and summary.updates > 0
            if summary.updates >= self.cfg.rebuild_after_updates or aged:
                plan.rebuilds.append(entity_id)
        return plan

    # --- running -------------------------------------------------------------

    def _record_el(self, world: World, e: Ent, today) -> str:
        stmts = [s for s in world.of_entity(e.id) if s.is_current][-RECORD_STATEMENTS:]
        role = e.record.profile.get("role")
        lines = [
            f'<record id="{attr(e.id)}" type="{e.type}" {last_mentioned(world.last_activity(e.id), today)}>',
            f"names: {'; '.join(world.names_of(e.id))}",
            *([f"role: {role}"] if role else []),
            *[stmt_el(s, today, with_id=False) for s in stmts],
            "</record>",
        ]
        return "\n".join(lines)

    def _get(self, entity_id: str) -> EntityRecord:
        return self.ctx.vault.get_entity(entity_id)

    def _duplicates(self, plan: DreamPlan, calls: Calls, run: DreamRun, touched: set[str], now: datetime) -> None:
        today = local_date(now)
        for a_id, b_id in plan.duplicates:
            if calls.exhausted:
                run.budget_exhausted = True
                return
            world = self.ctx.world()
            a, b = world.get(a_id), world.get(b_id)
            if a is None or b is None or a.id == b.id:
                continue  # one of them was merged earlier in this pass
            if _kept_apart(world, a.id, b.id):
                continue  # a note said two of the pages now merged into these are different
            key = _pair_key(a_id, b_id)
            user = "\n".join([today_el(now), self._record_el(world, a, today), self._record_el(world, b, today)])
            same = calls.ask("dream_same", user, SameOut, fallback=SameOut(same=False)).same
            self.state.checked_pairs[key] = self._pair_sig(world, a_id, b_id)
            if not same:
                continue
            # Keep the one with more notes; on a tie, the confirmed one, then the older one.
            keep, gone = sorted(
                (a, b), key=lambda e: (-world.note_count(e.id), e.status == "provisional", e.created_at, e.id)
            )
            k, g = self._get(keep.id), self._get(gone.id)
            g.merge_into(k, "dream")
            self.ctx.vault.put_entity(g)
            self.ctx.vault.put_entity(k)
            calls.log("merged", f"{g.name} and {k.name} are the same {k.type}; merged", k.id)
            touched.update((k.id, g.id))

    def _stale(self, plan: DreamPlan, calls: Calls, run: DreamRun, touched: set[str], now: datetime) -> None:
        today = local_date(now)
        world = self.ctx.world()
        items = world.by_id(plan.stale)
        for sid in plan.stale:
            if calls.exhausted:
                run.budget_exhausted = True
                return
            item = items.get(sid)
            if item is None or not item.is_open:
                continue
            later = self._later(world, item)
            ids = {s.id for s in later}

            def validate(out: StaleOut, ids=ids) -> None:
                if out.verdict != "open" and out.by not in ids:
                    raise ValueError("by must be the id of one of the later statements")

            user = "\n".join(
                [today_el(now), stmt_el(item, today, tag="item"), *[stmt_el(s, today, tag="later") for s in later]]
            )
            out = calls.ask("dream_stale", user, StaleOut, fallback=StaleOut(verdict="open"), validate=validate)
            self.state.checked_items[sid] = self._stale_sig(later)
            if out.verdict == "open" or not out.by:
                continue
            rel = "answers" if out.verdict == "answered" else "supersedes"
            run.relations.append(Relation(src=out.by, dst=sid, rel=rel))
            verb = "Answered" if rel == "answers" else "No longer applies"
            calls.log(rel, f'{verb}: "{item.text}"', item.thread)
            touched.update(x for x in [item.thread, *world.entities_of([sid])[sid]] if x)

    def _close(self, plan: DreamPlan, calls: Calls, touched: set[str], now: datetime) -> None:
        for tid in plan.close:
            rec = self._get(tid)
            if rec.status != "active" or rec.note_set("status"):
                continue
            rec.status, rec.closed_at = "closed", now
            rec.set_by["status"] = "dream"
            self.ctx.vault.put_entity(rec)
            calls.log("closed", f"{rec.name}: closed after {self.cfg.close_after_days} days with nothing open", rec.id)
            touched.add(tid)

    def _profiles(self, plan: DreamPlan, calls: Calls, run: DreamRun, touched: set[str], now: datetime) -> None:
        today = local_date(now)
        world = self.ctx.world()
        orgs = world.roots("org", with_provisional=False)
        org_ids = {o.id for o in orgs}
        org_els = [
            f'<candidate id="{attr(o.id)}">\nnames: {"; ".join(world.names_of(o.id))}\n</candidate>' for o in orgs
        ]

        def validate(out: ProfileOut) -> None:
            if out.org and out.org not in org_ids:
                raise ValueError("org must be a candidate id or empty")

        for pid in plan.profiles:
            if calls.exhausted:
                run.budget_exhausted = True
                return
            p = world.get(pid)
            if p is None:
                continue
            stmts = [s for s in world.of_entity(p.id) if s.is_current]
            user = "\n".join(
                [
                    today_el(now),
                    f"<person>names: {'; '.join(world.names_of(p.id))}</person>",
                    *[stmt_el(s, today, with_id=False) for s in stmts[-PROFILE_STATEMENTS:]],
                    *org_els,
                ]
            )
            out = calls.ask("dream_profile", user, ProfileOut, fallback=ProfileOut(), validate=validate)
            self.state.profiles[pid] = bytes_hash(*[s.id for s in stmts])
            profile = {}
            if out.role and out.role.strip():
                profile["role"] = " ".join(out.role.split())[:80]
            if out.org:
                profile["org"] = world.entities[out.org].name
            rec = self._get(p.id)
            if profile and profile != rec.profile:
                rec.profile = {**rec.profile, **profile}
                self.ctx.vault.put_entity(rec)
                calls.log("profile", f"{rec.name}: " + ", ".join(f"{k} {v}" for k, v in profile.items()), rec.id)
                touched.add(p.id)

    def run(self, plan: DreamPlan | None = None, *, progress: Progress | None = None) -> DreamRun:
        ctx = self.ctx
        now = ctx.clock()
        plan = plan or self.plan(now=now)
        say = progress or (lambda _: None)
        run = DreamRun(run_id=keys.basic_timestamp(now), started_at=now)
        cfg = ctx.settings.llm
        calls = Calls(ctx.llm, budget=self.cfg.max_calls, strong=cfg.dream_uses == "strong" and bool(cfg.strong_model))
        touched: set[str] = set()
        steps: list[tuple[str, Callable[[], None]]] = [
            ("looking for duplicates", lambda: self._duplicates(plan, calls, run, touched, now)),
            ("checking old open items", lambda: self._stale(plan, calls, run, touched, now)),
            ("closing quiet threads", lambda: self._close(plan, calls, touched, now)),
            ("filling in profiles", lambda: self._profiles(plan, calls, run, touched, now)),
        ]
        try:
            for label, step in steps:
                say(label)
                step()
        except (LLMError, StorageError) as e:
            run.errors.append(str(e))
        run.events = list(calls.events)
        run.model = calls.model
        run.finished_at = ctx.clock()
        ctx.write(keys.dream_run_key(run.run_id), run)  # relations and events reach the index before pages are rebuilt
        try:
            say("updating pages")
            for eid in sorted(touched):
                pages.update(ctx, calls, eid, now)
            for eid in plan.rebuilds:
                if calls.exhausted:
                    run.budget_exhausted = True
                    break
                pages.update(ctx, calls, eid, now, rebuild=True)
                touched.add(eid)
            wiki.write(ctx, touched, now)
        except (LLMError, StorageError) as e:
            run.errors.append(str(e))
        run.calls = calls.count
        run.model = calls.model or run.model
        ctx.write(keys.dream_run_key(run.run_id), run)
        self.state.last_run = run.run_id
        self.state.last_run_at = run.finished_at
        self.state.notes_at_last_run = ctx.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        ctx.write(keys.DREAM_STATE, self.state)
        return run
