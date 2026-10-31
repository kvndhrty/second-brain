"""`brain review`: work through the review queue one item at a time."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.markup import escape

from brain.core import review as rv
from brain.core.services import Brain

KIND_LABEL = {
    "new_person": "New person",
    "new_org": "New organization",
    "ambiguous_mention": "Which one?",
    "proposed_theme": "Proposed theme",
    "proposed_thread": "Proposed thread",
    "proposed_answer": "Does this answer an earlier item?",
    "orphaned_decision": "A decision no longer matches any fragment",
}


def _choose_entity(out: Console, brain: Brain, etype: str, default: str, exclude: str | None = None) -> str | None:
    text = typer.prompt(f"Search {etype}s by name", default=default)
    choices = [c for c in brain.entity_choices(etype, text) if c.entity_id != exclude]
    if not choices:
        out.print(f"[yellow]no {etype} matches '{escape(text)}'[/yellow]")
        return None
    for n, c in enumerate(choices, 1):
        out.print(f"  {n}) {escape(c.name)} [dim]{c.entity_id}[/dim]")
    pick = typer.prompt("Number (blank to cancel)", default="", show_default=False).strip()
    if pick.isdigit() and 1 <= int(pick) <= len(choices):
        return choices[int(pick) - 1].entity_id
    return None


def _apply_all(item: rv.ReviewItem) -> bool:
    if item.others == 0:
        return False
    return typer.confirm(
        f"Apply to the {item.others} other entr{'y' if item.others == 1 else 'ies'} "
        f"that mention '{item.payload['surface']}'?",
        default=True,
    )


def _mention(out: Console, brain: Brain, item: rv.ReviewItem) -> rv.ReviewDecision | None:
    etype, surface = item.payload["type"], item.payload["surface"]
    cands = item.payload.get("candidates") or []
    for n, c in enumerate(cands, 1):
        out.print(f"  {n}) link to [bold]{escape(c['entity_id'])}[/bold] [dim](score {c['score']})[/dim]")
    noun = {"person": "person", "org": "organization", "theme": "theme"}[etype]
    keys = (
        "[c]reate" + (f"  [1-{len(cands)}] link" if cands else "") + "  [l]ink by name  "
        f"[i]gnore (not a {noun})  [s]kip  [q]uit"
    )
    choice = typer.prompt(keys, default="s").strip().lower()
    if choice == "q":
        raise typer.Exit()
    if choice.isdigit() and 1 <= int(choice) <= len(cands):
        return rv.LinkMention(cands[int(choice) - 1]["entity_id"], apply_to_all=_apply_all(item))
    if choice == "l":
        target = _choose_entity(out, brain, etype, surface)
        return rv.LinkMention(target, apply_to_all=_apply_all(item)) if target else None
    if choice == "c":
        proposed = (item.payload.get("proposed") or {}).get("name", surface)
        name = typer.prompt("Name", default=proposed).strip()
        role = org = None
        kind = "domain"
        if etype == "person":
            role = typer.prompt("Role (blank to skip)", default="", show_default=False).strip() or None
            org_text = typer.prompt("Organization (blank to skip)", default="", show_default=False).strip()
            if org_text:
                org = _choose_entity(out, brain, "org", org_text)
        if etype == "theme":
            kind = typer.prompt("Kind (domain or technique)", default="domain").strip()
        return rv.CreateEntity(name, role=role, org=org, kind=kind, apply_to_all=_apply_all(item))
    if choice == "i":
        return rv.IgnoreMention(apply_to_all=_apply_all(item))
    return None


def _proposed(out: Console, brain: Brain, item: rv.ReviewItem) -> rv.ReviewDecision | None:
    etype = "theme" if item.kind == "proposed_theme" else "thread"
    eid = item.payload[etype]
    choice = typer.prompt("[a]ccept  [r]ename  [m]erge into existing  [s]kip  [q]uit", default="a").strip().lower()
    if choice == "q":
        raise typer.Exit()
    if choice == "a":
        return rv.AcceptProposed()
    if choice == "r":
        return rv.AcceptProposed(rename=typer.prompt("New name", default=item.payload["name"]).strip())
    if choice == "m":
        target = _choose_entity(out, brain, etype, item.payload["name"], exclude=eid)
        return rv.MergeProposed(target) if target else None
    return None


def run_review(out: Console, brain: Brain) -> None:
    skipped: set[str] = set()
    done = 0
    while True:
        items = [i for i in brain.review_items() if i.id not in skipped]
        if not items:
            break
        item = items[0]
        label = item.entry_label or ""
        label = label if len(label) <= 50 else label[:49] + "…"
        where = f"{item.entry_date} · {escape(label)}" if item.entry_date else ""
        out.print(
            f"\n[bold]{KIND_LABEL.get(item.kind, item.kind)}[/bold]  [dim]{len(items)} left"
            + (f" · {where}" if where else "")
            + "[/dim]"
        )
        decision: rv.ReviewDecision | None = None
        if item.kind in rv.MENTION_KINDS:
            out.print(
                f"  name: [bold]{escape(item.payload['surface'])}[/bold] ({item.payload['type']})"
                + (
                    f"  [dim]also in {item.others} other entr{'y' if item.others == 1 else 'ies'}[/dim]"
                    if item.others
                    else ""
                )
            )
            for text in item.context[:3]:
                out.print(f"  [dim]>[/dim] {escape(text)}")
            decision = _mention(out, brain, item)
        elif item.kind in ("proposed_theme", "proposed_thread"):
            out.print(
                f"  [bold]{escape(item.payload['name'])}[/bold] [dim]{item.payload.get('theme') or item.payload.get('thread')}[/dim]"
            )
            decision = _proposed(out, brain, item)
        elif item.kind == "proposed_answer":
            if len(item.context) == 2:
                out.print(f"  earlier: {escape(item.context[0])}")
                out.print(f"  new:     {escape(item.context[1])}")
            choice = typer.prompt("[y]es (closes the earlier item)  [n]o  [s]kip  [q]uit", default="s").strip().lower()
            if choice == "q":
                raise typer.Exit()
            decision = rv.AnswerDecision(True) if choice == "y" else rv.AnswerDecision(False) if choice == "n" else None
        elif item.kind == "orphaned_decision":
            out.print(f"  fragment {item.payload['fragment']}: {item.payload['decision']}")
            choice = typer.prompt("[d]rop  [s]kip  [q]uit", default="s").strip().lower()
            if choice == "q":
                raise typer.Exit()
            decision = rv.DropDecision() if choice == "d" else None
        if decision is None:
            skipped.add(item.id)
            continue
        try:
            brain.resolve_review(item.id, decision)
            done += 1
        except rv.ReviewError as e:
            out.print(f"[red]{escape(str(e))}[/red]")
            skipped.add(item.id)
    out.print(f"\nreview done: {done} decided, {len(skipped)} skipped")
