"""Layer 4: decide which known entity each name refers to, or create a new one.

Code settles a name that matches exactly one known name. The model is asked
only about the rest, and only to pick one of the candidates code found or
"new". A new entity starts provisional: it has no page until a second note
mentions it, unless it is a person written with a full name.
"""

from __future__ import annotations

from datetime import date

from brain.core.formats.note import Note
from brain.core.formats.records import EntityRecord, Link, NameRef
from brain.core.index.queries import Ent
from brain.core.llm.schemas import ChoiceOut
from brain.core.pipeline.context import Calls, PipelineContext
from brain.core.pipeline.text import attr, candidate_el, sentence_with
from brain.core.temporal import dated, today_el

PROMPTS = ("link",)
CANDIDATES = 8
NAME_TYPES = ("person", "org", "topic")


def touch(ctx: PipelineContext, calls: Calls, entity_id: str, note: Note, *, name: str | None = None) -> EntityRecord:
    """Record that a note used an entity: add the name it was called by, and confirm a provisional one."""
    rec = ctx.vault.get_entity(entity_id)
    changed = bool(name) and rec.add_name(name or "")
    if rec.status == "provisional" and rec.created_from != note.id:
        rec.status = "active"
        changed = True
        calls.log("confirmed", f"{rec.name} ({rec.type}) appeared in a second note and now has a page", rec.id)
    if changed:
        ctx.vault.put_entity(rec)
    return rec


def create(ctx: PipelineContext, calls: Calls, note: Note, etype: str, name: str, *, active: bool) -> EntityRecord:
    rec = ctx.vault.create_entity(
        name,
        lambda eid: EntityRecord(
            id=eid,
            type=etype,  # type: ignore[arg-type]
            name=name,
            status="active" if active else "provisional",
            created_at=note.created,
            created_from=note.id,
        ),
    )
    calls.log("created", f"New {etype}: {name}" + ("" if active else " (provisional)"), rec.id)
    return rec


def _candidates(ctx: PipelineContext, ref: NameRef) -> tuple[Ent | None, list[Ent]]:
    """(the entity code is sure of, or None; otherwise the candidates to show the model)."""
    world = ctx.world()
    exact = world.by_name(ref.surface, ref.type)
    if len(exact) == 1:
        return exact[0], []
    if not exact:
        # The same name may have been typed differently in another note (a team as org, then as topic).
        other = [e for e in world.by_name(ref.surface) if e.type in NAME_TYPES]
        if len(other) == 1:
            return other[0], []
    return None, exact or world.similar(ref.surface, ref.type, limit=CANDIDATES)


def link(ctx: PipelineContext, calls: Calls, note: Note, names: list[NameRef], today: date) -> list[Link]:
    links = []
    # Longest names first, so "Jane Doe" exists before "Jane" is resolved.
    for ref in sorted(names, key=lambda n: (-len(n.surface), n.surface)):
        sure, cands = _candidates(ctx, ref)
        how = "exact"
        chosen = sure.id if sure else None
        if chosen is None and cands:
            world = ctx.world()
            ids = {c.id for c in cands}

            def validate(out: ChoiceOut, ids=ids) -> None:
                if out.choice != "new" and out.choice not in ids:
                    raise ValueError('choice must be one of the candidate ids or "new"')

            user = "\n".join(
                [
                    today_el(ctx.clock()),
                    f'<name type="{ref.type}" {dated(note.created, today)}>{attr(ref.surface)}</name>',
                    f"<sentence>{sentence_with(note.body, ref.surface)}</sentence>",
                    *[candidate_el(world, c, today) for c in cands],
                ]
            )
            out = calls.ask("link", user, ChoiceOut, fallback=ChoiceOut(choice="new"), validate=validate)
            if out.choice != "new":
                chosen, how = out.choice, "model"
        if chosen is None:
            full_name = ref.type == "person" and len(ref.surface.split()) >= 2
            chosen, how = create(ctx, calls, note, ref.type, ref.surface, active=full_name).id, "new"
        else:
            rec = touch(ctx, calls, chosen, note, name=ref.surface)
            if how == "model":
                calls.log("linked", f'"{ref.surface}" taken to mean {rec.name}', rec.id)
        links.append(Link(surface=ref.surface, type=ref.type, entity=chosen, how=how))  # type: ignore[arg-type]
    return links
