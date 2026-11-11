"""`brain` command line. Parses input, calls brain.core, formats output."""

from __future__ import annotations

import os
import secrets
import shlex
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from brain import __version__
from brain.core import config as cfgmod
from brain.core.config import ConfigError, LoadedSettings
from brain.core.doctor import CheckResult, run_doctor
from brain.core.formats.yamlio import FormatError
from brain.core.index.db import IndexError_
from brain.core.llm.models import LLM, LLMError
from brain.core.localstate import LocalState
from brain.core.services import Brain, Busy, JotFailed
from brain.core.storage.base import StorageError
from brain.core.storage.lease import LeaseError

app = typer.Typer(
    no_args_is_help=True, add_completion=False, help="A work journal: you write notes, and the model keeps the wiki."
)
out = Console(soft_wrap=True)
err = Console(stderr=True, soft_wrap=True)


class _State:
    config: str | None = None


_state = _State()


def _version(value: bool) -> None:
    if value:
        out.print(f"brain {__version__}")
        raise typer.Exit()


@app.callback()
def main_callback(
    config: Annotated[
        str | None,
        typer.Option("--config", "-c", help="Config file (default: $BRAIN_CONFIG or ~/.config/brain/config.toml)."),
    ] = None,
    version: Annotated[
        bool, typer.Option("--version", callback=_version, is_eager=True, help="Show the version.")
    ] = False,
) -> None:
    _state.config = config


def _fail(message: str, code: int = 1) -> typer.Exit:
    err.print(f"[red]error:[/red] {escape(message)}")
    return typer.Exit(code)


def _load() -> LoadedSettings:
    try:
        return cfgmod.load(_state.config)
    except ConfigError as e:
        raise _fail(str(e), 2) from None


def _make_llm(settings) -> LLM | None:
    # BRAIN_FAKE_LLM=1 runs every model call against the scripted fake: for
    # trying the CLI without a model endpoint, and for front-end development.
    if os.environ.get("BRAIN_FAKE_LLM") == "1":
        from brain.core.llm.fake import FakeLLM

        return FakeLLM(settings)
    return None


@contextmanager
def _brain(command: str, *, force: bool = False, sync: bool = False) -> Iterator[Brain]:
    loaded = _load()
    try:
        with Brain(loaded, command=command, force_lease=force, llm=_make_llm(loaded.settings)) as b:
            if sync:
                b.sync()
            yield b
    except LeaseError as e:
        raise _fail(str(e), 3) from None
    except (StorageError, IndexError_, FormatError, LLMError, Busy) as e:
        raise _fail(str(e)) from None


# --- jot --------------------------------------------------------------------


def _from_editor(drafts: Path) -> str:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    path = drafts / f"editing-{os.getpid()}.md"
    path.write_text("", encoding="utf-8")
    try:
        result = subprocess.run([*shlex.split(editor), str(path)], check=False)
        if result.returncode != 0:
            raise _fail(
                f"editor `{editor}` exited with status {result.returncode}; nothing saved (draft kept at {path})"
            )
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise _fail(f"editor `{editor}` not found; set $EDITOR or use -m") from None
    path.unlink(missing_ok=True)
    return text


@app.command()
def jot(
    message: Annotated[str | None, typer.Option("--message", "-m", help="Note text.")] = None,
    file: Annotated[Path | None, typer.Option("--file", "-f", help="Read the note from a file.")] = None,
    title: Annotated[str | None, typer.Option("--title", "-t", help="Optional title.")] = None,
    about: Annotated[str | None, typer.Option("--about", help="A wiki page (id or name) this note corrects.")] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Take the write lease even if another machine holds it.")
    ] = False,
    process: Annotated[bool, typer.Option("--process/--no-process", help="Run the layers right after saving.")] = True,
) -> None:
    """Write a note: -m TEXT, --file, stdin, or $EDITOR.

    Notes are the only input. To correct the wiki, write a note that says what
    is wrong ("Jane Doe and J. Doe are the same person", "the vendor evaluation
    is finished", "ignore what I wrote about the outage cause")."""
    if message is not None:
        text = message
    elif file is not None:
        text = file.read_text(encoding="utf-8")
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        text = _from_editor(LocalState().drafts_dir)
    if not text.strip():
        raise _fail("empty note; nothing saved")
    # Text typed into the editor or piped in exists nowhere else, so it is kept in a file until it is saved.
    keep = None
    if message is None and file is None:
        keep = LocalState().drafts_dir / f"draft-{secrets.token_hex(4)}.md"
        keep.write_text(text, encoding="utf-8")
    try:
        _jot(text, title, about, force, process, file, keep)
    except BaseException:
        if keep is not None and keep.exists():
            err.print(f"your text is kept in {keep}; retry with `brain jot --file {keep}`")
        raise


def _jot(
    text: str, title: str | None, about: str | None, force: bool, process: bool, file: Path | None, keep: Path | None
) -> None:
    with _brain("brain jot", force=force, sync=about is not None) as b:
        if about is not None:
            about = _page_id(b, about)
        try:
            result = b.jot(text, title=title, about=about)
        except JotFailed as e:
            if keep is not None:
                keep.unlink(missing_ok=True)  # `Brain.jot` kept the text in the draft its message names
            raise _fail(str(e), 3 if isinstance(e.cause, LeaseError) else 1) from None
        if keep is not None:
            keep.unlink(missing_ok=True)
        out.print(f"saved [bold]{result.note.id}[/bold] → {result.key}")
        if file is not None and file.parent == b.state.drafts_dir:
            file.unlink(missing_ok=True)
        drafts = b.pending_drafts()
        if drafts:
            out.print(f"[yellow]{len(drafts)} unsent draft(s) in {drafts[0].parent}[/yellow]")
        if process and _hand_to_server(b, result.note.id):
            return
        if process and b.llm.configured:
            from brain.cli.render import run_processing

            run_processing(out, lambda progress: b.process(result.note.id, progress=progress))
        elif process:
            out.print("[dim]no model endpoint configured; the note is saved and pending (see `brain doctor`)[/dim]")


def _page_id(b: Brain, text: str) -> str:
    from brain.core import views

    try:
        return views.resolve(b.world(), text).id
    except views.NoSuchPage as e:
        raise _fail(str(e), 2) from None


def _hand_to_server(b: Brain, note_id: str) -> bool:
    """If `brain start` is running for this store, let its worker process the note."""
    import httpx2

    from brain.core import runtime

    info = runtime.read(b.state, b.storage.ident)
    if info is None:
        return False
    try:
        # trust_env=False: a localhost request must never go through HTTPS_PROXY.
        r = httpx2.post(f"{info.url}/api/worker/wake", json={"note": note_id}, timeout=3, trust_env=False)
        r.raise_for_status()
    except httpx2.HTTPError:
        return False
    out.print(f"processing in the background by `brain start` ({info.url})")
    return True


# --- index ------------------------------------------------------------------


def _print_sync(report, verb: str) -> None:
    if report.skipped:
        out.print("index is up to date")
        return
    out.print(
        f"{verb}: {report.listed} objects listed, {report.changed} changed, "
        f"{report.deleted} removed, {report.units} units rebuilt"
    )
    for key, message in report.problems:
        err.print(f"[yellow]problem:[/yellow] {escape(key)}: {escape(message.splitlines()[0])}")


@app.command()
def sync(
    full: Annotated[bool, typer.Option("--full", help="List the whole store even if nothing seems changed.")] = False,
) -> None:
    """Bring the local index up to date with the store."""
    with _brain("brain sync") as b:
        _print_sync(b.sync(full=full), "synced")


@app.command()
def reindex() -> None:
    """Delete the local index and rebuild it from the store (no model calls)."""
    with _brain("brain reindex") as b:
        _print_sync(b.reindex(), "rebuilt")


# --- doctor -----------------------------------------------------------------

_SYMBOL = {
    "ok": "[green]✓[/green]",
    "warn": "[yellow]![/yellow]",
    "fail": "[red]✗[/red]",
    "skip": "[dim]-[/dim]",
    "info": "[blue]i[/blue]",
}


def print_checks(results: list[CheckResult]) -> None:
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(width=1)
    table.add_column(style="bold", no_wrap=True)
    table.add_column(overflow="fold")
    for r in results:
        detail = escape(r.detail) if not r.fix else f"{escape(r.detail)}\n[dim]→ {escape(r.fix)}[/dim]"
        table.add_row(_SYMBOL[r.status], r.name, detail)
    out.print(table)


@app.command()
def doctor() -> None:
    """Check config, storage, lease, TLS, model endpoints, and the index."""
    results = run_doctor(_state.config)
    print_checks(results)
    failed = sum(r.status == "fail" for r in results)
    warned = sum(r.status == "warn" for r in results)
    out.print(f"\n{failed} failed, {warned} warnings")
    if failed:
        raise typer.Exit(1)


# --- init -------------------------------------------------------------------


def _ask(label: str, default: str = "") -> str:
    return typer.prompt(label, default=default, show_default=bool(default)).strip()


@app.command()
def init() -> None:
    """Write a config file interactively."""
    path, _ = cfgmod.resolve_config_path(_state.config)
    if path.exists() and not typer.confirm(f"{path} exists. Overwrite?", default=False):
        raise typer.Exit()
    s = cfgmod.Settings()
    out.print("[bold]Storage[/bold]")
    backend = typer.prompt("Backend (local or s3)", default="local").strip().lower()
    if backend == "s3":
        s.storage.backend = "s3"
        s.storage.bucket = typer.prompt("Bucket").strip()
        s.storage.prefix = _ask("Prefix", "brain/")
        s.storage.endpoint_url = _ask("Endpoint URL (blank for AWS)")
        s.storage.region = _ask("Region (blank for default)")
        s.storage.profile = _ask("AWS profile (blank for the standard credential chain)")
        if s.storage.endpoint_url:
            s.storage.addressing_style = _ask("Addressing style (auto, path, virtual)", "path")  # type: ignore[assignment]
    elif backend == "local":
        s.storage.path = _ask("Directory", str(s.storage.local_root()))
    else:
        raise _fail("backend must be 'local' or 's3'", 2)

    out.print("[bold]TLS[/bold] (used for both the model endpoint and S3)")
    s.tls.ca_bundle = _ask("CA bundle (blank for the OS trust store)")
    s.tls.client_cert = _ask("Client certificate (blank for none)")
    if s.tls.client_cert:
        s.tls.client_key = _ask("Client key (blank if inside the certificate file)")

    out.print("[bold]Models[/bold] (OpenAI-compatible endpoint)")
    s.llm.base_url = _ask("Base URL, e.g. https://gateway/v1 (blank to skip)")
    if s.llm.base_url:
        s.llm.api_key_env = _ask("Environment variable holding the API key", "BRAIN_API_KEY")
        s.llm.small_model = _ask("Small model (does the per-note work)")
        s.llm.strong_model = _ask("Strong model for dreaming (blank to use the small model)")
        s.llm.embed_model = _ask("Embedding model")

    try:
        validated = cfgmod.Settings.model_validate(s.model_dump())
    except Exception as e:  # noqa: BLE001
        raise _fail(f"invalid settings: {e}", 2) from None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cfgmod.to_toml(validated), encoding="utf-8")
    out.print(f"wrote {path}")
    if typer.confirm("Run `brain doctor` now?", default=True):
        _state.config = str(path)
        doctor()


# --- pipeline ---------------------------------------------------------------


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone()
    except ValueError:
        raise _fail(f"not a date: {value} (use YYYY-MM-DD)", 2) from None


def _note_progress(status_line):
    return lambda note_id, msg: status_line(f"{note_id}: {msg}" if note_id else msg)


@app.command()
def process(
    skip: Annotated[
        str | None,
        typer.Option("--skip", help="Leave this note out of the wiki, so the notes after it are processed."),
    ] = None,
) -> None:
    """Run the layers for every note that still needs them, including files saved into notes/ by hand.

    Notes are processed in date order. If one fails, the notes after it wait
    until it succeeds, or until it is left out with --skip NOTE_ID."""
    from brain.cli.render import run_processing

    with _brain("brain process") as b:
        if skip is not None:
            b.skip(skip)
            out.print(f"{skip} is left out of the wiki; `brain rebuild` tries it again")
        run_processing(out, lambda progress: b.process_pending(_note_progress(progress)))


@app.command()
def rebuild(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
) -> None:
    """Discard the wiki and everything generated, and run every note again in date order.

    Use after a prompt or model change. Notes are not touched; pages keep their ids."""
    with _brain("brain rebuild", sync=True) as b:
        n = b.status()["notes"]
        if not yes and not typer.confirm(f"Run all {n} note(s) through the model again?", default=False):
            raise typer.Exit()
        with out.status("rebuilding") as status:
            report = b.rebuild(_note_progress(lambda msg: status.update(msg)))
        out.print(f"rebuilt from {report.notes} note(s) with {report.calls} model call(s)")
        for line in report.failed:
            out.print(f"[red]✗[/red] {escape(line)}")
        if report.failed:
            out.print(
                f"stopped there, because later notes depend on it; {report.remaining} note(s) are still to do. "
                "`brain process` continues."
            )
            raise typer.Exit(1)


# --- reading -----------------------------------------------------------------


@app.command()
def active() -> None:
    """Threads with recent activity or open items, and where each stands."""
    from brain.cli.render import print_threads

    with _brain("brain active", sync=True) as b:
        print_threads(out, b.active_threads())


@app.command()
def wiki(
    page: Annotated[str | None, typer.Argument(help="A page: its id or exact name. Omit to list pages.")] = None,
    kind: Annotated[str | None, typer.Option("--type", help="List only: thread, person, org, or topic.")] = None,
) -> None:
    """Show a wiki page, or list the pages. The same pages are files under wiki/ in the store."""
    from brain.cli.render import print_pages
    from brain.core import views

    if kind and kind not in ("thread", "person", "org", "topic"):
        raise _fail("--type must be thread, person, org, or topic", 2)
    with _brain("brain wiki", sync=True) as b:
        if page is None:
            print_pages(out, b.pages(kind))
            return
        try:
            print(b.page_markdown(page), end="")
        except views.NoSuchPage as e:
            raise _fail(str(e), 2) from None


@app.command()
def log(
    limit: Annotated[int, typer.Option("--limit", "-n", help="How many lines.")] = 40,
) -> None:
    """What brain did after each note and in each dream pass, newest first."""
    from brain.core.temporal import iso_day

    with _brain("brain log", sync=True) as b:
        events = b.log(limit=limit)
        if not events:
            out.print("nothing yet")
        for ev in events:
            source = "dream" if ev.source.startswith("dream:") else ev.source
            out.print(f"{iso_day(ev.at)}  {escape(ev.text)}  [dim]{source}[/dim]")


@app.command()
def brief(
    request: Annotated[
        str, typer.Argument(help="A thread, person, org, or topic (name or id), or a request in plain words.")
    ],
    since: Annotated[
        str | None, typer.Option(help="Report changes since this date (default: your last brief).")
    ] = None,
) -> None:
    """A re-entry brief: where things stand, decisions, open items, people, and what changed."""
    from brain.cli.render import print_brief
    from brain.core.answers import AmbiguousTarget

    with _brain("brain brief", sync=True) as b:
        try:
            with out.status("writing brief"):
                result = b.brief(request, since=_parse_date(since))
        except AmbiguousTarget as e:
            raise _fail(str(e), 2) from None
        print_brief(out, result)


@app.command()
def ask(question: Annotated[str, typer.Argument(help="Your question.")]) -> None:
    """Answer a question from the journal, citing dated notes."""
    from brain.cli.render import print_answer

    with _brain("brain ask", sync=True) as b, out.status("searching"):
        answer = b.ask(question)
    print_answer(out, answer)


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Words to look for.")],
    limit: Annotated[int, typer.Option("--limit", "-n")] = 10,
) -> None:
    """Find pages, statements, and notes."""
    with _brain("brain search", sync=True) as b:
        hits = b.search(query, limit=limit)
        if not hits:
            out.print("nothing found")
        for h in hits:
            where = f"{h.date}  " if h.date else ""
            text = h.snippet or h.label
            label = f"[bold]{escape(h.label)}[/bold]  " if h.type not in ("statement",) else ""
            out.print(f"{where}{label}{escape(text)}  [dim]{h.type} · {h.note_id or h.id}[/dim]")


@app.command()
def show(note_id: Annotated[str, typer.Argument(help="Note id, or 'latest'.")] = "latest") -> None:
    """Show what the layers made of a note: its statements, where they were filed, and what changed."""
    from brain.cli.render import print_note
    from brain.core import views

    with _brain("brain show", sync=True) as b:
        if note_id == "latest":
            recent = b.recent_notes(limit=1)
            if not recent:
                raise _fail("no notes yet")
            note_id = recent[0].id
        try:
            print_note(out, b.note(note_id))
        except views.NoSuchPage as e:
            raise _fail(str(e)) from None
        failure = b.failures().get(note_id)
        if failure:
            out.print(f"[red]last error in {failure['step']}: {escape(failure['error'])}[/red]")


# --- injected context and dreaming --------------------------------------------


@app.command()
def context(
    fmt: Annotated[str, typer.Option("--format", help="markdown or json.")] = "markdown",
) -> None:
    """A short summary of current state for agent sessions: active threads, open items, recent changes.

    Meant to be loaded at the start of every session, for example by a Claude Code SessionStart hook."""
    import dataclasses
    import json

    if fmt not in ("markdown", "json"):
        raise _fail("--format must be markdown or json", 2)
    with _brain("brain context", sync=True) as b:
        if fmt == "json":
            print(json.dumps(dataclasses.asdict(b.context()), indent=2))
        else:
            print(b.context_text(), end="")


@app.command()
def dream(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what a pass would look at, without calling a model.")
    ] = False,
) -> None:
    """Dreaming: an offline pass across all notes. Merges duplicate people and things, settles old
    open items that later notes answered, closes quiet threads, fills in people's roles, and rebuilds
    worn summaries. Everything it does is in `brain log`; a note can reverse any of it."""
    with _brain("brain dream") as b:
        if dry_run:
            plan = b.dream_plan()
            if plan.empty:
                out.print("nothing to do")
                return
            for label, items in (
                ("pair(s) of possible duplicates to compare", plan.duplicates),
                ("old open item(s) to check", plan.stale),
                ("quiet thread(s) to close", plan.close),
                ("profile(s) to fill in", plan.profiles),
                ("summary(ies) to rebuild", plan.rebuilds),
            ):
                if items:
                    out.print(f"  • {len(items)} {label}")
            out.print(rf"a pass stops after {b.settings.dream.max_calls} model calls (\[dream] max_calls)")
            return
        run = b.dream(progress=lambda msg: err.print(f"[dim]{escape(msg)}[/dim]"))
        for ev in run.events:
            out.print(f"[green]✓[/green] {escape(ev.text)}")
        for e in run.errors:
            out.print(f"[red]✗[/red] {escape(e)}")
        if run.budget_exhausted:
            out.print(
                rf"[yellow]stopped after {run.calls} model calls (\[dream] max_calls); run again to continue[/yellow]"
            )
        if not (run.events or run.errors):
            out.print(f"nothing changed ({run.calls} model call(s))")


# --- agents -----------------------------------------------------------------


@app.command()
def mcp() -> None:
    """Serve the journal to agents (e.g. Claude Code) over MCP on stdio."""
    from brain.core.shared import SharedBrain
    from brain.mcp.server import build_server

    loaded = _load()  # a config error stops here, before the client connects
    shared = SharedBrain(lambda: Brain(loaded, command="brain mcp", llm=_make_llm(loaded.settings)))
    try:
        build_server(shared, context_in_instructions=True).run("stdio")
    finally:
        shared.close()


@app.command()
def start(
    port: Annotated[int | None, typer.Option(help="Port on 127.0.0.1 (default: server.port).")] = None,
    open_browser: Annotated[bool, typer.Option("--open", help="Open the web UI in a browser.")] = False,
) -> None:
    """Run the web UI, the HTTP API, MCP over HTTP at /mcp, and background processing."""
    import webbrowser

    import uvicorn

    from brain.core import runtime
    from brain.core.shared import SharedBrain
    from brain.core.worker import Worker
    from brain.mcp.server import build_server
    from brain.web.app import STATIC_DIR, create_app

    loaded = _load()
    settings = loaded.settings
    port = port or settings.server.port
    state = LocalState()

    def open_brain(command: str) -> Brain:
        return Brain(loaded, command=command, llm=_make_llm(settings), state=state)

    shared = SharedBrain(lambda: open_brain("brain start"))
    try:
        store = shared.call(lambda b: b.storage.ident)
    except (StorageError, IndexError_, FormatError) as e:
        raise _fail(str(e)) from None
    running = runtime.read(state, store)
    if running is not None and running.pid != os.getpid():
        shared.close()
        raise _fail(f"`brain start` is already running for this store (pid {running.pid}, {running.url})")
    worker = Worker(lambda: open_brain("brain start (worker)"), poll_seconds=settings.worker.poll_seconds)
    mcp_server = build_server(shared, process_note=lambda note_id: worker.process_now(note_id, timeout=300))
    stopped = False

    def shutdown() -> None:
        nonlocal stopped
        if stopped:
            return
        stopped = True
        worker.stop()
        shared.close()  # releases the lease
        runtime.clear(state, os.getpid())

    web = create_app(shared, worker=worker, mcp_server=mcp_server, port=port, on_shutdown=shutdown)
    url = f"http://127.0.0.1:{port}"
    runtime.write(state, runtime.ServerInfo(os.getpid(), port, store))
    worker.start()
    out.print(f"brain {__version__} at [bold]{url}[/bold]")
    out.print(f"MCP: {url}/mcp  [dim](claude mcp add --transport http brain {url}/mcp)[/dim]")
    if not (STATIC_DIR / "index.html").exists():
        out.print("[yellow]the web UI is not built; see frontend/README.md[/yellow]")
    if open_browser:
        webbrowser.open(url)
    try:
        uvicorn.run(web, host="127.0.0.1", port=port, log_level="warning")
    finally:
        shutdown()  # if the server never started (e.g. the port is taken)


@app.command(hidden=True)
def serve(
    port: Annotated[int | None, typer.Option()] = None,
    open_browser: Annotated[bool, typer.Option("--open")] = False,
) -> None:
    """Alias for `brain start`."""
    start(port=port, open_browser=open_browser)


def main() -> None:
    app()
