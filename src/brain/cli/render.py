"""Terminal output for pipeline runs, answers, briefs, and lists."""

from __future__ import annotations

from collections.abc import Callable

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from brain.core.answers import Answer, Brief, Source
from brain.core.llm.schemas import Claim
from brain.core.pipeline.runner import ProcessReport
from brain.core.temporal import iso_day, to_local
from brain.core.views import EntitySummary, NoteView

SECTIONS = [
    ("where_things_stand", "Where things stand"),
    ("recent_decisions", "Recent decisions"),
    ("open_items", "Open items"),
    ("people", "People"),
    ("no_longer_applies", "No longer applies"),
    ("changed_since", "Changed since you last looked"),
]


def run_processing(out: Console, run: Callable[[Callable[[str], None]], ProcessReport]) -> ProcessReport | None:
    """Run the pipeline with a status line. Ctrl-C leaves the note saved and pending."""
    try:
        with out.status("processing") as status:
            report = run(lambda msg: status.update(msg))
    except KeyboardInterrupt:
        out.print("[yellow]stopped; the note is saved and will be processed by `brain process`[/yellow]")
        return None
    print_report(out, report)
    return report


def print_report(out: Console, report: ProcessReport) -> None:
    for n in report.notes:
        if n.error:
            out.print(
                f"[red]✗[/red] {n.note_id}: {escape(n.error)}  [dim](still pending; retry with `brain process`)[/dim]"
            )
        elif n.layers:
            pages = f"; updated {len(n.pages)} page(s)" if n.pages else ""
            out.print(f"[green]✓[/green] {n.note_id}: {n.calls} model call(s){pages}")
    if not report.notes:
        out.print("nothing to process")


class Citations:
    """Numbers sources in the order they are first cited."""

    def __init__(self, sources: dict[str, Source]) -> None:
        self.sources = sources
        self.order: list[str] = []

    def mark(self, claim: Claim) -> str:
        marks = []
        for c in claim.citations:
            if c not in self.order:
                self.order.append(c)
            marks.append(str(self.order.index(c) + 1))
        return f" [dim]\\[{','.join(marks)}][/dim]" if marks else ""

    def print(self, out: Console) -> None:
        if not self.order:
            return
        out.print("\n[bold]Sources[/bold]")
        for n, nid in enumerate(self.order, 1):
            s = self.sources.get(nid)
            label = s.label if s else nid
            label = label if len(label) <= 60 else label[:59].rstrip() + "…"
            out.print(f"  [dim]{n}.[/dim] {s.date if s else '?'}  {escape(label)}  [dim]{nid}[/dim]")


def print_answer(out: Console, answer: Answer) -> None:
    cites = Citations(answer.sources)
    if answer.insufficient:
        out.print("[yellow]The journal does not answer this directly.[/yellow]")
    for claim in answer.claims:
        out.print(f"• {escape(claim.text)}{cites.mark(claim)}")
    cites.print(out)


def print_brief(out: Console, brief: Brief) -> None:
    since = brief.since.date().isoformat() if brief.since else "never"
    names = ", ".join(t.name for t in brief.topics) or brief.request
    out.print(f"[bold]{escape(names)}[/bold]  [dim]last looked: {since}[/dim]")
    cites = Citations(brief.sources)
    for field, heading in SECTIONS:
        claims = getattr(brief.output, field)
        if not claims:
            continue
        out.print(f"\n[bold]{heading}[/bold]")
        for claim in claims:
            out.print(f"  • {escape(claim.text)}{cites.mark(claim)}")
    if not brief.output.all_claims():
        out.print("nothing in the journal about this yet")
    cites.print(out)
    if brief.topics and not brief.recorded:
        out.print("[dim]note: the brief time was not saved (another machine holds the write lease)[/dim]")


def print_threads(out: Console, threads: list[EntitySummary]) -> None:
    if not threads:
        out.print("no active threads")
        return
    for t in threads:
        last = iso_day(t.last_activity) if t.last_activity else "—"
        opened = f" · {t.open_items} open" if t.open_items else ""
        out.print(f"[bold]{escape(t.name)}[/bold]  [dim]{last}{opened} · {t.id}[/dim]")
        if t.now:
            out.print(f"  {escape(t.now)}")


def print_pages(out: Console, pages: list[EntitySummary]) -> None:
    if not pages:
        out.print("no pages yet")
        return
    table = Table(box=None, pad_edge=False)
    for col in ("page", "type", "status", "last activity", "notes", "id"):
        table.add_column(col, overflow="fold")
    for p in pages:
        table.add_row(
            escape(p.name),
            p.type,
            p.status,
            iso_day(p.last_activity) if p.last_activity else "—",
            str(p.note_count),
            f"[dim]{p.id}[/dim]",
        )
    out.print(table)


def print_note(out: Console, note: NoteView) -> None:
    state = "processed" if note.done else "pending"
    when = to_local(note.created).strftime("%Y-%m-%d %H:%M")
    out.print(f"[bold]{escape(note.title or note.id)}[/bold]  [dim]{when} · {note.id} · {state}[/dim]")
    for s in note.statements:
        flags = [s.kind] + ([s.status] if s.status != "current" else []) + ([s.thread_name] if s.thread_name else [])
        out.print(f"• {escape(s.text)}\n  [dim]{escape(' · '.join(flags))}[/dim]")
    if note.entities:
        out.print("[dim]mentions: " + escape(", ".join(f"{e.name} ({e.type})" for e in note.entities)) + "[/dim]")
    for ev in note.events:
        out.print(f"[dim]→ {escape(ev.text)}[/dim]")
