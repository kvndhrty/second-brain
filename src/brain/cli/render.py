"""Terminal output for pipeline runs, answers, briefs, and lists."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from brain.core.answers import Answer, Brief, Source
from brain.core.index.queries import FragmentRow, ThreadRow
from brain.core.llm.schemas import Claim
from brain.core.pipeline.runner import ProcessReport
from brain.core.temporal import iso_day

SECTIONS = [
    ("where_things_stand", "Where things stand"),
    ("recent_decisions", "Recent decisions"),
    ("open_items", "Open items"),
    ("people", "People"),
    ("speculative", "Speculative"),
    ("overcome_by_events", "Overcome by events"),
    ("changed_since", "Changed since you last looked"),
]


def run_processing(out: Console, run: Callable[[Callable[[str], None]], ProcessReport], brain) -> ProcessReport | None:
    """Run the pipeline with a status line. Ctrl-C leaves the entry saved and pending."""
    try:
        with out.status("processing") as status:
            report = run(lambda msg: status.update(msg))
    except KeyboardInterrupt:
        out.print("[yellow]stopped; the entry is saved and will be processed by `brain process`[/yellow]")
        return None
    print_report(out, report)
    pending = [i for i in brain.review_items() if i.kind in ("new_person", "new_org", "ambiguous_mention")]
    if pending:
        out.print(f"[yellow]{len(pending)} name(s) need review: run `brain review`[/yellow]")
    return report


def print_report(out: Console, report: ProcessReport) -> None:
    for e in report.entries:
        if e.error:
            out.print(
                f"[red]✗[/red] {e.entry_id}: {escape(e.error)}  [dim](still pending; retry with `brain process`)[/dim]"
            )
        elif e.steps:
            out.print(f"[green]✓[/green] {e.entry_id}: {', '.join(e.steps)}")
    if report.threads:
        out.print(
            f"[green]✓[/green] updated {len(report.threads)} thread summar{'y' if len(report.threads) == 1 else 'ies'}"
        )
    for tid, error in report.thread_errors.items():
        out.print(f"[red]✗[/red] thread {tid}: {escape(error)}")
    if not report.entries and not report.threads:
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
        for n, eid in enumerate(self.order, 1):
            s = self.sources.get(eid)
            label = s.label if s else eid
            label = label if len(label) <= 60 else label[:59].rstrip() + "…"
            out.print(f"  [dim]{n}.[/dim] {s.date if s else '?'}  {escape(label)}  [dim]{eid}[/dim]")


def print_answer(out: Console, answer: Answer) -> None:
    cites = Citations(answer.sources)
    if answer.insufficient:
        out.print("[yellow]The journal does not answer this directly.[/yellow]")
    for claim in answer.claims:
        out.print(f"• {escape(claim.text)}{cites.mark(claim)}")
    cites.print(out)


def print_brief(out: Console, brief: Brief) -> None:
    since = brief.since.date().isoformat() if brief.since else "never"
    out.print(
        f"[bold]{escape(brief.label)}[/bold]  [dim]{brief.type} · {brief.fragment_count} fragments · "
        f"last looked: {since}[/dim]"
    )
    for note in brief.notes:
        out.print(f"[dim]{escape(note)}[/dim]")
    cites = Citations(brief.sources)
    for field, heading in SECTIONS:
        claims = getattr(brief.output, field)
        if not claims:
            continue
        out.print(f"\n[bold]{heading}[/bold]")
        for claim in claims:
            out.print(f"  • {escape(claim.text)}{cites.mark(claim)}")
    if brief.open_items:
        out.print("\n[bold]Open questions and action items[/bold] [dim](close with `brain status ID --closed`)[/dim]")
        for f in brief.open_items:
            out.print(f"  • {escape(f.text)}  [dim]{f.kind} · {iso_day(f.created_at)} · {f.id}[/dim]")
    cites.print(out)
    if not brief.recorded:
        out.print("[dim]note: the brief time was not saved (another machine holds the write lease)[/dim]")


def print_active(out: Console, threads: list[ThreadRow], now: datetime) -> None:
    if not threads:
        out.print("no active threads")
        return
    table = Table(box=None, pad_edge=False)
    for col in ("thread", "last activity", "fragments", "open", "people", "id"):
        table.add_column(col, overflow="fold")
    for t in threads:
        name = escape(t.name) + (" [dim](proposed)[/dim]" if t.state == "proposed" else "")
        if t.pinned:
            name = "📌 " + name
        last = iso_day(t.last_activity) if t.last_activity else "—"
        if t.last_activity:
            days = (now - datetime.fromisoformat(t.last_activity)).days
            last += f" [dim]({days}d)[/dim]"
        table.add_row(
            name,
            last,
            str(t.fragments),
            str(t.open_items) if t.open_items else "",
            ", ".join(t.people),
            f"[dim]{t.id}[/dim]",
        )
    out.print(table)


def print_fragments(out: Console, fragments: list[FragmentRow]) -> None:
    for f in fragments:
        flags = [f.kind, f.status]
        if f.state:
            flags.append(f.state)
        if f.dream:
            flags.append("dream")
        out.print(f"• {escape(f.text)}\n  [dim]{' · '.join(flags)} · {f.id}[/dim]")
