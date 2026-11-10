"""Layer 6: how a note changes what earlier notes said, and layer 6b: correction notes.

Layer 6 compares each new statement with the few earlier statements it most
plausibly affects, one pair per call: does it answer the earlier one,
supersede it, repeat it, or none of these.

Layer 6b turns each correction statement into one operation from a fixed
list. One call picks the operation; a second picks what it applies to, from
candidates code found. The operation is applied at once and recorded with the
note, so `brain rebuild` replays it. An operation from a note always wins
over anything a model inferred.
"""

from __future__ import annotations

from array import array
from datetime import datetime

from brain.core.formats.note import Note
from brain.core.formats.records import OPEN_KINDS, EntityRecord, Operation, Relation, norm_name
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
    world = ctx.world()
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
    body = norm_name(text)
    found: list[tuple[int, str]] = []
    for e in world.entities.values():
        best = max((len(n) for n in e.record.all_names() if len(n) > 2 and norm_name(n) in body), default=0)
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
    if op in ("close", "reopen"):
        # Closing applies to pieces of work, so threads come first.
        out.sort(key=lambda eid: world.entities[eid].type != "thread")
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


def corrections(ctx: PipelineContext, calls: Calls, note: Note, linked: list[str], now: datetime) -> list[Operation]:
    today = local_date(now)
    ops: list[Operation] = []
    retracted: set[str] = set()  # by this note, so two sentences of one correction do not repeat each other
    for s in ctx.world().of_note(note.id):
        if s.kind != "correction":
            continue
        world = ctx.world()
        about = world.get(note.about)
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
        applied = apply(ctx, calls, note, s.id, op, out.targets, out.new_name) if out.targets else None
        if applied is None:
            calls.log("ignored", f'Could not tell what this correction applies to: "{s.text}"')
            continue
        ops.append(applied)
        if op == "retract":
            retracted.update(applied.targets)
    return ops


def _origin(note: Note) -> str:
    return f"note:{note.id}"


def _get(ctx: PipelineContext, entity_id: str) -> EntityRecord | None:
    try:
        return ctx.vault.get_entity(entity_id)
    except NotFound:
        return None


def _unmerge(ctx: PipelineContext, calls: Calls, note: Note, rec: EntityRecord) -> None:
    into = _get(ctx, rec.merged_into or "")
    rec.merged_into = None
    rec.set_by["merged_into"] = _origin(note)
    if into:
        # The survivor took these names at the merge; they go back with the entity.
        mine = {norm_name(n) for n in rec.all_names()}
        into.names = [n for n in into.names if norm_name(n) not in mine]
        ctx.vault.put_entity(into)
        calls.log("split", f"{rec.name} is kept apart from {into.name} again", rec.id)


def _keep_apart(ctx: PipelineContext, a: EntityRecord, b: EntityRecord) -> None:
    for x, y in ((a, b), (b, a)):
        if y.id not in x.not_same_as:
            x.not_same_as.append(y.id)
        ctx.vault.put_entity(x)


def apply(
    ctx: PipelineContext, calls: Calls, note: Note, sid: str, op: str, targets: list[str], new_name: str | None
) -> Operation | None:
    """Apply one operation to the entity records and return it for the note's state file. None if it cannot apply."""
    made = Operation(op=op, statement=sid, targets=list(targets))  # type: ignore[arg-type]
    if op == "retract":
        calls.log("retracted", f"Retracted {len(targets)} earlier statement(s); they no longer appear on pages")
        return made
    world = ctx.world()
    recs = [_get(ctx, t) for t in targets]
    if any(r is None for r in recs):
        return None
    a = recs[0]
    assert a is not None
    if op == "same_as":
        if len(recs) < 2:
            return made  # both names already point at the same entity
        b = recs[1]
        assert b is not None
        if world.root(a.id) == world.root(b.id):
            return made
        a, b = (_get(ctx, world.root(a.id) or a.id) or a), (_get(ctx, world.root(b.id) or b.id) or b)
        b.merged_into = a.id
        b.set_by["merged_into"] = _origin(note)
        for n in b.all_names():
            a.add_name(n)
        a.not_same_as = [x for x in a.not_same_as if x != b.id]
        b.not_same_as = [x for x in b.not_same_as if x != a.id]
        if a.status == "provisional":
            a.status = "active"
        ctx.vault.put_entity(b)
        ctx.vault.put_entity(a)
        calls.log("merged", f"{b.name} is the same as {a.name}; merged", a.id)
        made.targets = [a.id, b.id]
        return made
    if op == "not_same_as":
        if len(recs) < 2:
            return None
        b = recs[1]
        assert b is not None
        for r in (a, b):
            if r.merged_into:
                _unmerge(ctx, calls, note, r)
                ctx.vault.put_entity(r)
        a, b = (_get(ctx, a.id) or a), (_get(ctx, b.id) or b)  # the survivor's names changed
        _keep_apart(ctx, a, b)
        calls.log("kept_apart", f"{a.name} and {b.name} are different and will not be merged", a.id)
        return made
    if op == "rename":
        name = " ".join((new_name or "").split())
        if not name:
            return None
        a = _get(ctx, world.root(a.id) or a.id) or a
        old = a.name
        a.first_name = a.first_name or old
        a.names = [n for n in [old, *a.names] if norm_name(n) != norm_name(name)]
        a.name = name
        a.set_by["name"] = _origin(note)
        ctx.vault.put_entity(a)
        calls.log("renamed", f"{old} is now called {name}", a.id)
        made.targets, made.name = [a.id], name
        return made
    if op in ("close", "reopen"):
        a = _get(ctx, world.root(a.id) or a.id) or a
        a.status = "closed" if op == "close" else "active"
        a.closed_at = note.created if op == "close" else None
        a.set_by["status"] = _origin(note)
        ctx.vault.put_entity(a)
        calls.log(
            "closed" if op == "close" else "reopened", f"{a.name}: {'closed' if op == 'close' else 'reopened'}", a.id
        )
        made.targets = [a.id]
        return made
    if op == "not_part_of":
        if len(recs) < 2:
            return None
        b = recs[1]
        assert b is not None
        if a.merged_into and world.root(a.id) == world.root(b.id):
            _unmerge(ctx, calls, note, a)
            ctx.vault.put_entity(a)
            _keep_apart(ctx, _get(ctx, a.id) or a, _get(ctx, b.id) or b)
            return made
        whole = world.root(b.id) or b.id
        part = world.root(a.id) or a.id
        _keep_apart(ctx, a, b)
        if world.entities[whole].type != "thread":
            return made
        if world.entities[part].type == "thread":
            dest = part
        else:
            # The part is a person, org, or topic: its statements move to a thread of the same name.
            name = world.entities[part].name
            known = world.by_name(name, "thread")
            dest = known[0].id if known else link_layer.create(ctx, calls, note, "thread", name, active=True).id
        mentioning = {s.id for s in world.of_entity(part)}
        made.moved = [s.id for s in world.of_entity(whole) if s.id in mentioning]
        made.targets = [dest, whole]
        calls.log(
            "moved",
            f"{world.entities[part].name} is not part of {world.entities[whole].name}; "
            f"moved {len(made.moved)} statement(s)",
            dest,
        )
        return made
    return None
