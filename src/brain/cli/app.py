"""`brain` command line. Parses input, calls brain.core, formats output."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
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
from brain.core.localstate import LocalState
from brain.core.services import Brain, JotFailed
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


@contextmanager
def _brain(command: str, *, force: bool = False) -> Iterator[Brain]:
    loaded = _load()
    try:
        with Brain(loaded, command=command, force_lease=force) as b:
            yield b
    except LeaseError as e:
        raise _fail(str(e), 3) from None
    except (StorageError, IndexError_, FormatError) as e:
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


def main() -> None:
    app()
