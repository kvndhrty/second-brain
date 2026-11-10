"""Layer 5: file each statement under a thread, start a new thread, or leave it unfiled.

Code finds the candidate threads: ones named in the note, ones already chosen
for this note, and the threads of the nearest earlier statements and of
statements about the same people and topics. The model picks one of them,
"new", or "none".
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from brain.core.formats.note import Note
from brain.core.formats.records import Statement, norm_name
from brain.core.index import queries, search
from brain.core.index.queries import World
from brain.core.llm.schemas import FileOut
from brain.core.pipeline import link as link_layer
from brain.core.pipeline.context import Calls, PipelineContext
from brain.core.pipeline.text import candidate_el, note_el
from brain.core.temporal import local_date, today_el

PROMPTS = ("file",)
CANDIDATES = 8
NEAREST = 20


def _candidates(ctx: PipelineContext, world: World, note: Note, s: Statement, chosen: list[str]) -> list[str]:
    body = norm_name(note.body)
    ranked: list[str] = []

    def add(thread_id: str | None) -> None:
        if thread_id and thread_id not in ranked:
            ranked.append(thread_id)

    for t in world.roots("thread"):
        if any(len(n) > 3 and norm_name(n) in body for n in world.names_of(t.id)):
            add(t.id)
    for t in chosen:
        add(world.root(t))
    votes: dict[str, float] = {}
    vec = queries.vectors(ctx.conn, [s.id]).get(s.id)
    if vec is not None:
        hits = [h for h in search.nearest_blob(ctx.conn, vec, k=NEAREST) if h.item_type == "statement"]
        near = world.by_id([h.item_id for h in hits])
        for h in hits:
            other = near.get(h.item_id)
            if other and other.note_id != note.id and other.thread:
                votes[other.thread] = votes.get(other.thread, 0.0) + h.score
    for ents in world.entities_of([s.id]).values():
        for e in ents:
            for other in world.of_entity(e)[-10:]:
                if other.note_id != note.id and other.thread:
                    votes[other.thread] = votes.get(other.thread, 0.0) + 0.5
    for t in sorted(votes, key=lambda i: (-votes[i], i)):
        add(t)
    return ranked[:CANDIDATES]


def file(
    ctx: PipelineContext, calls: Calls, note: Note, statements: list[Statement], kinds: Mapping[str, str], now: datetime
) -> dict[str, str | None]:
    today = local_date(now)
    head = f"{today_el(now)}\n{note_el(note, today)}"
    threads: dict[str, str | None] = {}
    chosen: list[str] = []
    for s in statements:
        if kinds.get(s.id) == "correction":
            threads[s.id] = None
            continue
        world = ctx.world()
        cands = _candidates(ctx, world, note, s, chosen)

        def validate(out: FileOut, cands=cands) -> None:
            if out.choice not in (*cands, "new", "none"):
                raise ValueError('choice must be one of the candidate ids, "new", or "none"')
            if out.choice == "new" and not 1 <= len((out.new_name or "").split()) <= 8:
                raise ValueError('with choice "new", new_name must be a short name of one to eight words')

        user = "\n".join(
            [
                head,
                f'<statement kind="{kinds.get(s.id, "fact")}">{s.text}</statement>',
                *[candidate_el(world, world.entities[c], today) for c in cands],
            ]
        )
        out = calls.ask("file", user, FileOut, fallback=FileOut(choice="none"), validate=validate)
        thread = None
        if out.choice == "new":
            name = " ".join((out.new_name or "").split())
            known = world.by_name(name, "thread") or world.similar(name, "thread", limit=1, floor=0.85)
            if known:
                thread = known[0].id
            else:
                thread = link_layer.create(ctx, calls, note, "thread", name, active=False).id
        elif out.choice != "none":
            thread = out.choice
        if thread:
            rec = link_layer.touch(ctx, calls, thread, note)
            if rec.status == "closed" and not rec.note_set("status"):
                # Dreaming closed it for inactivity; new activity opens it again.
                rec.status, rec.closed_at = "active", None
                rec.set_by.pop("status", None)
                ctx.vault.put_entity(rec)
                calls.log("reopened", f"{rec.name}: reopened by new activity", rec.id)
            if thread not in chosen:
                chosen.append(thread)
        threads[s.id] = thread
    return threads
