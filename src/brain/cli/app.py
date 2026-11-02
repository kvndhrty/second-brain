"""`brain` command line. Parses input, calls brain.core, formats output."""

from __future__ import annotations

import os
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
    no_args_is_help=True, add_completion=False, help="A work journal that builds its own knowledge graph."
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
    message: Annotated[str | None, typer.Option("--message", "-m", help="Entry text.")] = None,
    file: Annotated[Path | None, typer.Option("--file", "-f", help="Read the entry from a file.")] = None,
    title: Annotated[str | None, typer.Option("--title", "-t", help="Optional title.")] = None,
    tag: Annotated[list[str] | None, typer.Option("--tag", help="Tag (repeatable).")] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Take the write lease even if another machine holds it.")
    ] = False,
    process: Annotated[bool, typer.Option("--process/--no-process", help="Run extraction right after saving.")] = True,
) -> None:
    """Write a journal entry: -m TEXT, --file, stdin, or $EDITOR."""
    if message is not None:
        text = message
    elif file is not None:
        text = file.read_text(encoding="utf-8")
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        text = _from_editor(LocalState().drafts_dir)
    if not text.strip():
        raise _fail("empty entry; nothing saved")
    with _brain("brain jot", force=force) as b:
        try:
            result = b.jot(text, title=title, tags=tag)
        except JotFailed as e:
            raise _fail(str(e), 3 if isinstance(e.cause, LeaseError) else 1) from None
        out.print(f"saved [bold]{result.entry.id}[/bold] → {result.key}")
        if file is not None and file.parent == b.state.drafts_dir:
            file.unlink(missing_ok=True)
        drafts = b.pending_drafts()
        if drafts:
            out.print(f"[yellow]{len(drafts)} unsent draft(s) in {drafts[0].parent}[/yellow]")
        if process and _hand_to_server(b, result.entry.id):
            return
        if process and b.llm.configured:
            from brain.cli.render import run_processing

            run_processing(out, lambda progress: b.process(result.entry.id, progress=progress), b)
        elif process:
            out.print("[dim]no model endpoint configured; extraction is pending (see `brain doctor`)[/dim]")


def _hand_to_server(b: Brain, entry_id: str) -> bool:
    """If `brain start` is running for this store, let its worker process the entry."""
    import httpx2

    from brain.core import runtime

    info = runtime.read(b.state, b.storage.ident)
    if info is None:
        return False
    try:
        # trust_env=False: a localhost request must never go through HTTPS_PROXY.
        r = httpx2.post(f"{info.url}/api/worker/wake", json={"entry": entry_id}, timeout=3, trust_env=False)
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
        s.llm.chat_model = _ask("Chat model")
        s.llm.extract_model = _ask("Extraction model", s.llm.chat_model)
        s.llm.embed_model = _ask("Embedding model")
        s.llm.doc_model = _ask("Document-extraction model (blank for none)")

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


def _entry_progress(status_line):
    return lambda entry_id, msg: status_line(f"{entry_id}: {msg}" if entry_id else msg)


@app.command()
def process() -> None:
    """Run extraction for every entry that still needs it, and update stale thread summaries."""
    from brain.cli.render import run_processing

    with _brain("brain process") as b:
        run_processing(out, lambda progress: b.process_pending(_entry_progress(progress)), b)


@app.command()
def reextract(
    entry_ids: Annotated[list[str] | None, typer.Argument(help="Entry ids (default: all, or --since).")] = None,
    since: Annotated[str | None, typer.Option(help="Only entries on or after this date (YYYY-MM-DD).")] = None,
    stale_prompts: Annotated[
        bool, typer.Option("--stale-prompts", help="Only entries extracted with an older prompt version.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
) -> None:
    """Re-run extraction after a model or prompt change. Your decisions are kept."""
    from brain.cli.render import run_processing

    with _brain("brain reextract") as b:
        b.sync()
        if not entry_ids and not since and not stale_prompts and not yes:
            n = b.db.conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            if not typer.confirm(f"Re-extract all {n} entries? This calls the model for each one.", default=False):
                raise typer.Exit()
        run_processing(
            out,
            lambda progress: b.reextract(
                entry_ids, since=_parse_date(since), stale_prompts=stale_prompts, progress=_entry_progress(progress)
            ),
            b,
        )


@app.command()
def review() -> None:
    """Confirm new people, organizations, themes, threads, and proposed answers."""
    from brain.cli.review import run_review

    with _brain("brain review", sync=True) as b:
        run_review(out, b)


# --- reading -----------------------------------------------------------------


@app.command()
def active() -> None:
    """Threads with recent activity, plus pinned ones."""
    from brain.cli.render import print_active

    with _brain("brain active", sync=True) as b:
        now = datetime.now().astimezone()
        print_active(out, b.active_threads(now=now), now)


@app.command()
def brief(
    target: Annotated[str, typer.Argument(help="A thread, person, org, or theme (name or id), or free text.")],
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
                result = b.brief(target, since=_parse_date(since))
        except AmbiguousTarget as e:
            raise _fail(str(e), 2) from None
        print_brief(out, result)


@app.command()
def ask(question: Annotated[str, typer.Argument(help="Your question.")]) -> None:
    """Answer a question from the journal, citing dated entries."""
    from brain.cli.render import print_answer

    with _brain("brain ask", sync=True) as b, out.status("searching"):
        answer = b.ask(question)
    print_answer(out, answer)


@app.command()
def show(entry_id: Annotated[str, typer.Argument(help="Entry id, or 'latest'.")] = "latest") -> None:
    """Show an entry's fragments with their ids, status, and pipeline state."""
    from brain.cli.render import print_fragments
    from brain.core.index import queries

    with _brain("brain show", sync=True) as b:
        c = b.db.conn
        row = (
            c.execute("SELECT id, created_at, title, next_step FROM entries ORDER BY created_at DESC, id DESC LIMIT 1")
            if entry_id == "latest"
            else c.execute("SELECT id, created_at, title, next_step FROM entries WHERE id = ?", (entry_id,))
        ).fetchone()
        if row is None:
            raise _fail(f"no entry {entry_id}")
        eid, created, title, next_step = row
        state = "processed" if next_step == "done" else f"pending ({next_step})"
        out.print(f"[bold]{escape(title or eid)}[/bold]  [dim]{created[:16]} · {eid} · {state}[/dim]")
        frag_ids = [r[0] for r in c.execute("SELECT id FROM fragments WHERE entry_id = ? ORDER BY id", (eid,))]
        print_fragments(out, list(queries.fragments_by_id(c, frag_ids).values()))
        failure = b.failures().get(eid)
        if failure:
            out.print(f"[red]last error in {failure['step']}: {escape(failure['error'])}[/red]")


@app.command()
def status(
    fragment_id: Annotated[str, typer.Argument(help="Fragment id (see `brain show`).")],
    confirmed: Annotated[bool, typer.Option("--confirmed", help="Mark as confirmed.")] = False,
    speculative: Annotated[bool, typer.Option("--speculative", help="Mark as speculative.")] = False,
    obe: Annotated[bool, typer.Option("--obe", help="Mark as overcome by events.")] = False,
    reason: Annotated[str | None, typer.Option(help="Why it was overcome by events.")] = None,
    on: Annotated[str | None, typer.Option("--date", help="When it was overcome (default today).")] = None,
    open_: Annotated[bool, typer.Option("--open", help="Reopen a question or action item.")] = False,
    closed: Annotated[bool, typer.Option("--closed", help="Close a question or action item.")] = False,
    note: Annotated[str | None, typer.Option(help="Note on closing.")] = None,
    dream: Annotated[bool | None, typer.Option("--dream/--not-dream", help="Mark or unmark as a dream.")] = None,
) -> None:
    """Change a fragment's status. Stored in the entry's decisions file, so it survives re-extraction."""
    from brain.core.services import FragmentChange

    if sum([confirmed, speculative, obe]) > 1 or (open_ and closed):
        raise _fail("choose one status and at most one of --open/--closed", 2)
    status_value = "confirmed" if confirmed else "speculative" if speculative else "overcome_by_events" if obe else None
    when = _parse_date(on)
    change = FragmentChange(
        status=status_value,  # type: ignore[arg-type]
        obe_reason=reason,
        obe_date=when.date() if when else None,
        state="open" if open_ else "closed" if closed else None,
        closed_note=note,
        dream=dream,
    )
    if change == FragmentChange():
        raise _fail("nothing to change; see `brain status --help`", 2)
    with _brain("brain status", sync=True) as b:
        try:
            b.set_fragment(fragment_id, change)
        except KeyError as e:
            raise _fail(str(e.args[0])) from None
        out.print(f"updated {fragment_id}")


# --- threads and the OBE sweep -------------------------------------------------


def _register_thread_commands() -> None:
    from brain.cli.threads import thread_app

    app.add_typer(thread_app, name="thread")


_register_thread_commands()


@app.command()
def sweep(
    days: Annotated[int, typer.Option(help="Open or speculative items older than this many days.")] = 30,
    quiet_days: Annotated[int, typer.Option(help="Threads with no activity for this many days.")] = 60,
) -> None:
    """Go through items that may be overcome by events: old open questions and action items,
    old speculation, and threads that have gone quiet."""
    from brain.cli.threads import run_sweep

    with _brain("brain sweep", sync=True) as b:
        run_sweep(b, days=days, quiet_days=quiet_days)


# --- agents -----------------------------------------------------------------


@app.command()
def mcp() -> None:
    """Serve the journal to agents (e.g. Claude Code) over MCP on stdio."""
    from brain.core.shared import SharedBrain
    from brain.mcp.server import build_server

    loaded = _load()  # a config error stops here, before the client connects
    shared = SharedBrain(lambda: Brain(loaded, command="brain mcp", llm=_make_llm(loaded.settings)))
    try:
        build_server(shared).run("stdio")
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
    mcp_server = build_server(shared, process_entry=lambda entry_id: worker.process_now(entry_id, timeout=300))
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
