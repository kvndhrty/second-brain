"""`brain thread ...` and `brain sweep`."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

import typer
from rich.markup import escape

from brain.core.services import Brain, FragmentChange
from brain.core.temporal import iso_day
from brain.core.threads import ThreadError

thread_app = typer.Typer(no_args_is_help=True, help="Rename, pin, archive, merge, split, or retire a thread.")


def _run(fn) -> None:
    from brain.cli.app import _brain, _fail, out

    with _brain("brain thread", sync=True) as b:
        try:
            message = fn(b)
        except ThreadError as e:
            raise _fail(str(e)) from None
        if message:
            out.print(message)


@thread_app.command("list")
def list_threads(
    all_: Annotated[bool, typer.Option("--all", help="Include archived, OBE, and merged.")] = False,
) -> None:
    """Threads with their state and last activity."""
    from brain.cli.app import _brain, out
    from brain.core.index import queries

    with _brain("brain thread", sync=True) as b:
        states = ("active", "proposed", "archived", "obe", "merged") if all_ else ("active", "proposed")
        for t in queries.threads(b.db.conn, states=states):
            flags = ("📌 " if t.pinned else "") + (f"[dim]({t.state})[/dim] " if t.state != "active" else "")
            last = iso_day(t.last_activity) if t.last_activity else "—"
            out.print(f"{flags}{escape(t.name)}  [dim]{t.id} · last {last}[/dim]")


@thread_app.command()
def rename(thread_id: str, name: str) -> None:
    """Rename a thread. The old name is kept as an alias."""
    _run(lambda b: f"renamed {thread_id} to {b.threads.rename(thread_id, name).name}")


@thread_app.command()
def pin(thread_id: str) -> None:
    """Keep a thread in the active list even when it is quiet."""
    _run(lambda b: b.threads.pin(thread_id, True) and f"pinned {thread_id}")


@thread_app.command()
def unpin(thread_id: str) -> None:
    """Let a thread leave the active list again when it goes quiet."""
    _run(lambda b: b.threads.pin(thread_id, False) and f"unpinned {thread_id}")


@thread_app.command()
def archive(thread_id: str) -> None:
    """Archive a finished thread. It leaves the active list; its history stays searchable."""
    _run(lambda b: b.threads.set_state(thread_id, "archived") and f"archived {thread_id}")


@thread_app.command()
def activate(thread_id: str) -> None:
    """Make an archived, OBE, or proposed thread active again."""
    _run(lambda b: b.threads.set_state(thread_id, "active") and f"{thread_id} is active")


@thread_app.command()
def obe(
    thread_id: str,
    reason: Annotated[str | None, typer.Option(help="Why the whole thread no longer applies.")] = None,
    cascade: Annotated[bool, typer.Option(help="Also mark its open questions and action items OBE.")] = False,
) -> None:
    """Mark a whole thread overcome by events."""

    def run(b: Brain) -> str:
        n = b.threads.obe(thread_id, reason=reason, cascade=cascade)
        return f"{thread_id} is overcome by events" + (f"; {n} open items marked too" if cascade else "")

    _run(run)


@thread_app.command()
def merge(thread_id: str, into: str) -> None:
    """Merge THREAD_ID into INTO. Its fragments then count toward INTO everywhere."""
    _run(lambda b: b.threads.merge(thread_id, into) and f"merged {thread_id} into {into}")


@thread_app.command()
def split(
    thread_id: str,
    fragment_ids: Annotated[list[str], typer.Argument(help="Fragments to move (see `brain thread show`).")],
    name: Annotated[str, typer.Option("--name", help="Name of the new thread.")],
    description: Annotated[str | None, typer.Option(help="One line about the new thread.")] = None,
) -> None:
    """Start a new thread from some of a thread's fragments."""
    _run(lambda b: f"created thread {b.threads.split(thread_id, fragment_ids, name, description).id}")


@thread_app.command()
def move(
    from_thread: str,
    to_thread: str,
    fragment_ids: Annotated[list[str], typer.Argument(help="Fragments to move.")],
) -> None:
    """Move fragments from one thread to another."""
    _run(lambda b: f"moved {b.threads.move(fragment_ids, from_thread, to_thread)} fragments")


@thread_app.command()
def show(thread_id: str) -> None:
    """A thread's fragments with their ids, oldest first."""
    from brain.cli.app import _brain, out
    from brain.cli.render import print_fragments

    with _brain("brain thread", sync=True) as b:
        items = b.timeline(f"thread:{thread_id}")
        out.print(f"[bold]{escape(thread_id)}[/bold]  [dim]{len(items)} fragments[/dim]")
        print_fragments(out, [i.fragment for i in reversed(items)])


def run_sweep(b: Brain, *, days: int, quiet_days: int) -> None:
    from brain.cli.app import out

    items = b.sweep(days=days, quiet_days=quiet_days, now=datetime.now().astimezone())
    if not items:
        out.print("nothing stale")
        return
    done = 0
    for n, item in enumerate(items, 1):
        out.print(f"\n[bold][{n}/{len(items)}][/bold] ", end="")
        if item.kind == "quiet_thread":
            out.print(f"Thread [bold]{escape(item.thread_name or '')}[/bold] has been quiet for {item.age_days} days")
            choice = typer.prompt("[a]rchive  [o]be  [p]in (keep active)  [s]kip  [q]uit", default="s").lower()
            if choice == "q":
                break
            tid = item.thread_id or ""
            if choice == "a":
                b.threads.set_state(tid, "archived")
            elif choice == "o":
                reason = typer.prompt("Reason (blank for none)", default="", show_default=False) or None
                b.threads.obe(tid, reason=reason, cascade=typer.confirm("Mark its open items OBE too?", default=True))
            elif choice == "p":
                b.threads.pin(tid, True)
            else:
                continue
            done += 1
            continue
        f = item.fragment
        assert f is not None
        what = "Still open" if item.kind == "stale_open" else "Still unconfirmed"
        where = f" · {', '.join(item.threads)}" if item.threads else ""
        out.print(f"{what} after {item.age_days} days[dim]{escape(where)}[/dim]\n  {escape(f.text)}  [dim]{f.id}[/dim]")
        if item.suggestion:
            verdict = item.suggestion["verdict"].replace("_", " ")
            out.print(f"  [cyan]dream: {verdict}[/cyan] [dim]{escape(item.suggestion['reason'])}[/dim]")
        keys = "[o]be  " + ("[c]lose  [k]eep open" if item.kind == "stale_open" else "[c]onfirm  [k]eep speculative")
        choice = typer.prompt(f"{keys}  [s]kip  [q]uit", default="s").lower()
        if choice == "q":
            break
        if choice == "o":
            reason = typer.prompt("Reason (blank for none)", default="", show_default=False) or None
            change = FragmentChange(status="overcome_by_events", obe_reason=reason)
        elif choice == "c":
            change = FragmentChange(state="closed") if item.kind == "stale_open" else FragmentChange(status="confirmed")
        elif choice == "k":
            change = FragmentChange(state="open") if item.kind == "stale_open" else FragmentChange(status="speculative")
        else:
            continue
        b.set_fragment(f.id, change)
        done += 1
    out.print(f"\nsweep done: {done} changed")
