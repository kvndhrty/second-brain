"""Layer 6: how a note changes what earlier notes said, and layer 6b: correction notes.

Layer 6 compares each new statement with the few earlier statements it most
plausibly affects, one pair per call: does it answer the earlier one,
supersede it, repeat it, or none of these.

Layer 6b turns each correction statement into one operation from a fixed
list. One call picks the operation; a second picks what it applies to, from
candidates code found. Every correction in a note is decided before any is
applied, so a call that fails leaves nothing half done. Each operation has
one function that applies it. An operation from a note always wins over
anything a model inferred.
"""

from __future__ import annotations

from array import array
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from brain.core.formats.note import Note
from brain.core.formats.records import OPEN_KINDS, EntityRecord, Operation, Relation, contains_name
from brain.core.index import queries, search
from brain.core.index.queries import CONTENT_KINDS, Ent, Stmt, World
from brain.core.llm.schemas import OpOut, RelationOut, TargetsOut
from brain.core.pipeline import link as link_layer
from brain.core.pipeline.context import Calls, PipelineContext
from brain.core.pipeline.text import attr, candidate_el, stmt_el, words
from brain.core.storage.base import NotFound
from brain.core.temporal import local_date, today_el

PROMPTS = ("relate", "correction_op", "correction_targets")
OPEN_PAIRS = 2  # open questions and actions a statement is compared with
PAIRS_PER_NOTE = 24
POOL = 200
RETRACT_CANDIDATES = 10
ENTITY_CANDIDATES = 10
TWO_TARGETS = ("same_as", "not_same_as", "not_part_of")
ONE_TARGET = ("rename", "close", "reopen")
THREADS_ONLY = ("close", "reopen")
# Two pages can be one only if they are the same type of thing. A team is sometimes read as an
# org and sometimes as a topic, so those two may be merged.
MERGEABLE = ({"org", "topic"},)


def _dot(a: bytes, b: bytes) -> float:
    x, y = array("f"), array("f")
    x.frombytes(a)
    y.frombytes(b)
    return sum(p * q for p, q in zip(x, y, strict=False))


def _earlier(ctx: PipelineContext, world: World, note: Note, s: Stmt) -> list[Stmt]:
    """Earlier statements this one may settle or replace, nearest in meaning first.

    Open questions and actions come from the statement's thread and from pages
    it mentions: any statement but a question may answer one. A decision or
    fact may also replace an earlier decision or fact in the same thread. A
    statement with no thread and no names is compared with its nearest
    neighbours by embedding instead.
    """
    if s.kind == "question":
        return []
    related = ([s.thread] if s.thread else []) + world.entities_of([s.id])[s.id]
    pool: dict[str, Stmt] = {}
    for e in related:
        for other in world.of_entity(e)[-POOL:]:
            pool[other.id] = other
    vec = queries.vectors(ctx.conn, [s.id]).get(s.id)
    if not related and vec is not None:
        hits = [h.item_id for h in search.nearest_blob(ctx.conn, vec, k=8) if h.item_type == "statement"]
        pool.update(world.by_id(hits))
    earlier = [
        o
        for o in pool.values()
        if o.note_id != note.id and o.created_at <= s.created_at and o.kind in CONTENT_KINDS and o.is_current
    ]
    vecs = queries.vectors(ctx.conn, [o.id for o in earlier]) if vec is not None else {}

    def nearest(cands: list[Stmt], n: int, mine: bytes | None = vec) -> list[Stmt]:
        if mine is None:
            return sorted(cands, key=lambda o: o.created_at, reverse=True)[:n]
        return sorted(cands, key=lambda o: (-_dot(mine, vecs[o.id]) if o.id in vecs else 0.0, o.id))[:n]

    out = nearest([o for o in earlier if o.kind in OPEN_KINDS], OPEN_PAIRS)
    if s.kind in ("decision", "fact"):
        same_thread = [o for o in earlier if o.kind in ("decision", "fact") and (o.thread == s.thread or not related)]
        out += nearest(same_thread, 2 if s.kind == "decision" else 1)
    return out


def relate(ctx: PipelineContext, calls: Calls, note: Note, now: datetime) -> list[Relation]:
    today = local_date(now)
    world = ctx.world(skip_note=note.id)
    relations: list[Relation] = []
    settled: set[str] = set()  # earlier statements this note has already answered or replaced
    pairs = 0
    for s in world.of_note(note.id):
        if s.kind not in CONTENT_KINDS:
            continue
        for old in _earlier(ctx, world, note, s):
            if old.id in settled:
                continue
            if pairs >= PAIRS_PER_NOTE:
                return relations
            pairs += 1

            def validate(out: RelationOut, old=old) -> None:
                if out.relation == "answers" and old.kind not in OPEN_KINDS:
                    raise ValueError('"answers" is only for an earlier question or action; choose another relation')

            user = "\n".join(
                [
                    today_el(now),
                    stmt_el(old, today, tag="earlier", with_id=False),
                    stmt_el(s, today, tag="later", with_id=False),
                ]
            )
            out = calls.ask("relate", user, RelationOut, fallback=RelationOut(relation="unrelated"), validate=validate)
            if out.relation == "unrelated":
                continue
            relations.append(Relation(src=s.id, dst=old.id, rel=out.relation))
            if out.relation != "repeats":
                settled.add(old.id)
                verb = "Answered" if out.relation == "answers" else "Replaced"
                calls.log(out.relation, f'{verb}: "{old.text}" by "{s.text}"', old.thread)
    return relations


# --- corrections -------------------------------------------------------------------


def _named_entities(world: World, text: str) -> list[str]:
    """Every entity, merged ones included, one of whose names appears in the text. Longest names first."""
    found: list[tuple[int, str]] = []
    for e in world.entities.values():
        best = max((len(n) for n in e.record.all_names() if len(n) > 2 and contains_name(n, text)), default=0)
        if best:
            found.append((best, e.id))
    return [eid for _, eid in sorted(found, key=lambda x: (-x[0], x[1]))]


def _shares_words(world: World, text: str) -> list[str]:
    """Surviving entities at least half of whose name's words appear in the text ("SRE hiring" for "SRE opening hiring")."""
    said = words(text)
    scored: list[tuple[float, str]] = []
    for e in world.roots(with_provisional=False):
        best = 0.0
        for n in world.names_of(e.id):
            w = words(n)
            if w:
                best = max(best, len(w & said) / len(w))
        if best >= 0.5:
            scored.append((best, e.id))
    return [eid for _, eid in sorted(scored, key=lambda x: (-x[0], x[1]))]


def _entity_candidates(
    ctx: PipelineContext, world: World, note: Note, s: Stmt, linked: list[str], op: str = ""
) -> list[str]:
    """Entities a correction may be about, most likely first.

    Named in it (longest name first), mentioned by it, the page it was written
    on, the threads of the note's other statements, entities whose names share
    words with it, the other names in the note, and the threads of the nearest
    earlier statements.
    """
    out: list[str] = []

    def add(eid: str | None) -> None:
        if eid and eid in world.entities and eid not in out:
            out.append(eid)

    for eid in _named_entities(world, s.text):
        add(eid)
    for eid in world.entities_of([s.id])[s.id]:
        add(eid)
    add(note.about)
    for other in world.of_note(note.id):
        add(other.thread)
    for eid in _shares_words(world, s.text):
        add(eid)
    for eid in linked:
        add(world.root(eid))
    vec = queries.vectors(ctx.conn, [s.id]).get(s.id)
    if vec is not None:
        hits = [h.item_id for h in search.nearest_blob(ctx.conn, vec, k=8) if h.item_type == "statement"]
        for near in world.by_id(hits).values():
            add(near.thread)
    if op in THREADS_ONLY:
        # Only a piece of work can be closed or reopened.
        out = [eid for eid in out if world.entities[eid].type == "thread"]
    return out[:ENTITY_CANDIDATES]


def _member_el(world: World, e: Ent) -> str:
    """A candidate that was merged into another entity, so it can be named in a 'not the same' instruction."""
    into = world.get(e.id)
    return (
        f'<candidate id="{attr(e.id)}" type="{e.type}" merged_into="{attr(into.name if into else "")}">\n'
        f"names: {'; '.join(e.record.all_names())}\n</candidate>"
    )


def _statement_candidates(ctx: PipelineContext, world: World, note: Note, s: Stmt, linked: list[str]) -> list[Stmt]:
    pool: dict[str, Stmt] = {}
    vec = queries.vectors(ctx.conn, [s.id]).get(s.id)
    if vec is not None:
        hits = [h.item_id for h in search.nearest_blob(ctx.conn, vec, k=20) if h.item_type == "statement"]
        pool.update(world.by_id(hits))
    for eid in _entity_candidates(ctx, world, note, s, linked):
        root = world.root(eid)
        if root:
            for other in world.of_entity(root)[-8:]:
                pool[other.id] = other
    cands = [
        o for o in pool.values() if o.note_id != note.id and o.created_at <= s.created_at and o.status != "retracted"
    ]
    return sorted(cands, key=lambda o: o.created_at, reverse=True)[:RETRACT_CANDIDATES]


@dataclass
class _Decision:
    statement: str  # the correction statement
    op: str
    targets: list[str]
    new_name: str | None = None


def _decide(ctx: PipelineContext, calls: Calls, note: Note, linked: list[str], now: datetime) -> list[_Decision]:
    """Ask the model what each correction statement asks for and what it applies to. Changes nothing."""
    today = local_date(now)
    world = ctx.world(skip_note=note.id)
    about = world.get(note.about)
    decided: list[_Decision] = []
    retracted: set[str] = set()  # by this note, so two sentences of one correction do not repeat each other
    for s in world.of_note(note.id):
        if s.kind != "correction":
            continue
        instruction = f"<instruction>{s.text}</instruction>"
        if about:
            instruction += f'\n<written_from_page id="{attr(about.id)}" name="{attr(about.name)}"/>'
        op = calls.ask("correction_op", instruction, OpOut, fallback=OpOut(op="none")).op
        if op == "none":
            calls.log("ignored", f'Could not tell what this correction asks for: "{s.text}"')
            continue
        if op == "retract":
            stmts = [o for o in _statement_candidates(ctx, world, note, s, linked) if o.id not in retracted]
            ids = [o.id for o in stmts]
            shown = [stmt_el(o, today, tag="candidate") for o in stmts]
        else:
            ids = _entity_candidates(ctx, world, note, s, linked, op)
            shown = [
                candidate_el(world, world.entities[i], today, recent=2)
                if world.root(i) == i
                else _member_el(world, world.entities[i])
                for i in ids
            ]

        def validate(out: TargetsOut, op=op, ids=ids) -> None:
            if not out.targets:
                return
            if any(t not in ids for t in out.targets) or len(set(out.targets)) != len(out.targets):
                raise ValueError("targets must be distinct candidate ids")
            if op in TWO_TARGETS and len(out.targets) != 2 and len(ids) >= 2:
                raise ValueError(f"{op} needs exactly two targets")
            if op in ONE_TARGET and len(out.targets) != 1:
                raise ValueError(f"{op} needs exactly one target")
            if op == "rename" and not (out.new_name or "").strip():
                raise ValueError("rename needs new_name")
            if op == "same_as":
                types = {world.entities[t].type for t in out.targets}
                if len(types) > 1 and types not in MERGEABLE:
                    raise ValueError(
                        f"a {' and a '.join(sorted(types))} cannot be the same thing; choose other targets"
                    )

        named = {r for i in _named_entities(world, s.text) if (r := world.root(i))}
        if op == "same_as" and len(named) == 1:
            # Layer 4 already took both names to mean one entity; there is nothing to merge.
            (only,) = named
            calls.log("noted", f"{world.entities[only].name}: these names already lead to one page", only)
            continue
        if not ids:
            calls.log("ignored", f'Could not tell what this correction applies to: "{s.text}"')
            continue
        user = "\n".join([today_el(now), f"<operation>{op}</operation>", instruction, *shown])
        out = calls.ask("correction_targets", user, TargetsOut, fallback=TargetsOut(targets=[]), validate=validate)
        if not out.targets:
            calls.log("ignored", f'Could not tell what this correction applies to: "{s.text}"')
            continue
        decided.append(_Decision(s.id, op, list(out.targets), out.new_name))
        if op == "retract":
            retracted.update(out.targets)
    return decided


def corrections(ctx: PipelineContext, calls: Calls, note: Note, linked: list[str], now: datetime) -> list[Operation]:
    """The operations this note's corrections ask for, applied in the order they were written."""
    ops: list[Operation] = []
    for d in _decide(ctx, calls, note, linked, now):
        applied = apply(ctx, calls, note, d.statement, d.op, d.targets, d.new_name)
        if applied is None:
            calls.log("ignored", "A correction in this note could not be applied: what it names is no longer there")
        else:
            ops.append(applied)
    return ops


# --- applying one operation --------------------------------------------------------


@dataclass
class _Apply:
    """What an operation needs: where to write, the note it came from, and the state before it."""

    ctx: PipelineContext
    calls: Calls
    note: Note
    world: World
    recs: list[EntityRecord]  # the target records, in the order the model gave them
    made: Operation
    new_name: str | None = None

    @property
    def origin(self) -> str:
        return f"note:{self.note.id}"

    def get(self, entity_id: str | None) -> EntityRecord | None:
        try:
            return self.ctx.vault.get_entity(entity_id or "")
        except NotFound:
            return None

    def survivor(self, rec: EntityRecord) -> EntityRecord:
        """The record a target now leads to, after merges."""
        return self.get(self.world.root(rec.id)) or rec

    def put(self, *recs: EntityRecord) -> None:
        for r in recs:
            self.ctx.vault.put_entity(r)

    def same_root(self, a: EntityRecord, b: EntityRecord) -> bool:
        return self.world.root(a.id) == self.world.root(b.id)

    def unmerge(self, rec: EntityRecord) -> EntityRecord:
        """Take a merged record out of the one it was merged into. Returns the record it leaves."""
        into = self.get(rec.merged_into)
        rec.split_from(into, self.origin)
        self.put(rec, *([into] if into else []))
        if into:
            self.calls.log("split", f"{rec.name} is kept apart from {into.name} again", rec.id)
        return into or rec

    def keep_apart(self, a: EntityRecord, b: EntityRecord) -> None:
        for x, y in ((a, b), (b, a)):
            if y.id not in x.not_same_as:
                x.not_same_as.append(y.id)
        self.put(a, b)


def _retract(a: _Apply) -> Operation | None:
    n = len(a.made.targets)
    a.calls.log("retracted", f"Retracted {n} earlier statement(s); they no longer appear on pages")
    return a.made


def _same_as(a: _Apply) -> Operation | None:
    if len(a.recs) < 2 or a.same_root(a.recs[0], a.recs[1]):
        return a.made  # both names already lead to one page
    keep, gone = a.survivor(a.recs[0]), a.survivor(a.recs[1])
    gone.merge_into(keep, a.origin)
    a.put(gone, keep)
    a.calls.log("merged", f"{gone.name} is the same as {keep.name}; merged", keep.id)
    a.made.targets = [keep.id, gone.id]
    return a.made


def _not_same_as(a: _Apply) -> Operation | None:
    if len(a.recs) < 2:
        return None
    x, y = a.recs
    if a.same_root(x, y):
        # They were merged with each other. A target merged into some third page stays there.
        for r in (x, y):
            if r.merged_into:
                a.unmerge(r)
        x, y = (a.get(x.id) or x), (a.get(y.id) or y)  # the survivor's names changed
    a.keep_apart(x, y)
    a.calls.log("kept_apart", f"{x.name} and {y.name} are different and will not be merged", x.id)
    return a.made


def _rename(a: _Apply) -> Operation | None:
    name = " ".join((a.new_name or "").split())
    if not name:
        return None
    rec = a.survivor(a.recs[0])
    old = rec.name
    rec.first_name = rec.first_name or old
    rec.names = [n for n in [old, *rec.names] if n.casefold() != name.casefold()]
    rec.name = name
    rec.set_by["name"] = a.origin
    a.put(rec)
    a.calls.log("renamed", f"{old} is now called {name}", rec.id)
    a.made.targets, a.made.name = [rec.id], name
    return a.made


def _set_status(a: _Apply) -> Operation | None:
    rec = a.survivor(a.recs[0])
    if rec.type != "thread":
        return None
    closing = a.made.op == "close"
    rec.status = "closed" if closing else "active"
    rec.closed_at = a.note.created if closing else None
    rec.set_by["status"] = a.origin
    a.put(rec)
    word = "closed" if closing else "reopened"
    a.calls.log(word, f"{rec.name}: {word}", rec.id)
    a.made.targets = [rec.id]
    return a.made


def _not_part_of(a: _Apply) -> Operation | None:
    """targets: the part, then the whole it was wrongly put in."""
    if len(a.recs) < 2:
        return None
    part_rec, whole_rec = a.recs
    world = a.world
    if part_rec.merged_into and a.same_root(part_rec, whole_rec):
        # The part was merged into the whole: take it out again.
        a.unmerge(part_rec)
        a.keep_apart(a.get(part_rec.id) or part_rec, a.get(whole_rec.id) or whole_rec)
        return a.made
    whole = world.entities[world.root(whole_rec.id) or whole_rec.id]
    part = world.entities[world.root(part_rec.id) or part_rec.id]
    a.keep_apart(part_rec, whole_rec)
    if whole.type != "thread":
        return a.made
    if part.type == "thread":
        dest = part.id
    else:
        # The part is a person, org, or topic: its statements move to a thread of the same name.
        known = world.by_name(part.name, "thread")
        dest = known[0].id if known else link_layer.create(a.ctx, a.calls, a.note, "thread", part.name, active=True).id
    dest_rec = a.get(dest)
    if dest_rec is not None and dest_rec.status == "provisional":
        dest_rec.status = "active"  # it now holds statements, so it needs a page
        a.put(dest_rec)
    mentioning = {s.id for s in world.of_entity(part.id)}
    a.made.moved = [s.id for s in world.of_entity(whole.id) if s.id in mentioning]
    a.made.targets = [dest, whole.id]
    a.calls.log("moved", f"{part.name} is not part of {whole.name}; moved {len(a.made.moved)} statement(s)", dest)
    return a.made


APPLY: dict[str, Callable[[_Apply], Operation | None]] = {
    "retract": _retract,
    "same_as": _same_as,
    "not_same_as": _not_same_as,
    "rename": _rename,
    "close": _set_status,
    "reopen": _set_status,
    "not_part_of": _not_part_of,
}


def apply(
    ctx: PipelineContext, calls: Calls, note: Note, sid: str, op: str, targets: list[str], new_name: str | None
) -> Operation | None:
    """Apply one operation to the entity records and return it for the note's state file. None if it cannot apply."""
    made = Operation(op=op, statement=sid, targets=list(targets))  # type: ignore[arg-type]
    env = _Apply(ctx, calls, note, ctx.world(skip_note=note.id), [], made, new_name)
    if op != "retract":
        found = [env.get(t) for t in targets]
        if any(r is None for r in found):
            return None
        env.recs = [r for r in found if r is not None]
    return APPLY[op](env)
