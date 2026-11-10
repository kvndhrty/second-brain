from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from brain.cli import app as cli
from brain.core.localstate import LocalState
from brain.core.storage.lease import Lease
from brain.core.storage.local import LocalStorage

runner = CliRunner()


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(f'[storage]\nbackend = "local"\npath = "{tmp_path / "store"}"\n')
    return p


def run(cfg: Path, *args: str, input: str | None = None):
    return runner.invoke(cli.app, ["--config", str(cfg), *args], input=input)


def test_jot_message_then_reindex(cfg, tmp_path):
    r = run(cfg, "jot", "-m", "Talked to Jane about quotas.", "--title", "Quota")
    assert r.exit_code == 0, r.output
    assert "saved n-" in r.output
    files = list((tmp_path / "store" / "notes").rglob("*.md"))
    assert len(files) == 1 and files[0].name.endswith("-quota.md")
    assert files[0].read_text().endswith("---\nTalked to Jane about quotas.\n")
    r = run(cfg, "reindex")
    assert r.exit_code == 0 and "1 units rebuilt" in r.output
    r = run(cfg, "sync")
    assert "up to date" in r.output


def test_jot_from_stdin(cfg, tmp_path):
    r = run(cfg, "jot", input="from a pipe\n")
    assert r.exit_code == 0, r.output
    (f,) = (tmp_path / "store" / "notes").rglob("*.md")
    assert f.name.endswith("-from-a-pipe.md")


def test_jot_empty_is_refused(cfg):
    r = run(cfg, "jot", "-m", "   ")
    assert r.exit_code == 1 and "empty" in r.output


def test_jot_from_editor(tmp_path, monkeypatch):
    script = tmp_path / "fake-editor.sh"
    script.write_text("#!/bin/sh\nprintf 'written in the editor\\n' > \"$1\"\n")
    script.chmod(0o755)
    monkeypatch.delenv("VISUAL", raising=False)  # VISUAL takes precedence over EDITOR
    monkeypatch.setenv("EDITOR", str(script))
    assert cli._from_editor(tmp_path) == "written in the editor\n"
    assert not list(tmp_path.glob("editing-*"))


def test_lease_held_by_other_machine_exits_3_and_force_works(cfg, tmp_path):
    store = LocalStorage(tmp_path / "store")
    store.supports_conditional_writes = True
    Lease(store, machine_id="other-machine", ttl_seconds=900, command="brain start").acquire()
    r = run(cfg, "jot", "-m", "hello")
    assert r.exit_code == 3
    assert "Another machine holds the write lease" in r.output and "--force" in r.output
    assert not list((tmp_path / "store").rglob("notes/**/*.md"))
    assert "--force" in r.output and "jot --file" in r.output  # the text is kept as a draft
    r = run(cfg, "jot", "-m", "hello", "--force")
    assert r.exit_code == 0, r.output


def test_failed_save_keeps_draft(cfg, tmp_path, monkeypatch):
    from brain.core import vault

    def boom(self, *a, **k):
        raise OSError("disk full")

    with monkeypatch.context() as m:  # undo only this patch, not the env isolation
        m.setattr(vault.Vault, "save_note", boom)
        r = run(cfg, "jot", "-m", "precious words")
    assert r.exit_code == 1
    assert "jot --file" in r.output
    drafts = list(LocalState().drafts_dir.glob("draft-*.md"))
    assert [d.read_text() for d in drafts] == ["precious words"]
    r = run(cfg, "jot", "--file", str(drafts[0]))
    assert r.exit_code == 0, r.output
    assert not drafts[0].exists()


def test_config_error_exits_2(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text("[storage]\nbackend = 's3'\n")
    r = runner.invoke(cli.app, ["--config", str(bad), "jot", "-m", "x"])
    assert r.exit_code == 2 and "storage.bucket is required" in r.output


def test_doctor_exit_code(cfg):
    r = run(cfg, "doctor")
    assert r.exit_code == 0, r.output
    assert "put/get/list/delete work" in r.output


def test_init_writes_config(tmp_path):
    target = tmp_path / "new.toml"
    answers = "\n".join(["local", str(tmp_path / "s"), "", "", "", "n"]) + "\n"
    r = runner.invoke(cli.app, ["--config", str(target), "init"], input=answers)
    assert r.exit_code == 0, r.output
    from brain.core.config import load

    s = load(target).settings
    assert s.storage.backend == "local" and s.storage.path == str(tmp_path / "s")


def test_core_does_not_import_front_end_libraries():
    code = (
        "import sys, brain.core.services, brain.core.doctor, brain.core.llm.client, brain.core.llm.fake;"
        "bad = sorted({m.split('.')[0] for m in sys.modules}"
        " & {'typer','click','rich','fastapi','starlette','uvicorn','mcp'});"
        "print(','.join(bad))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[2] / "src")},
    )
    assert out.stdout.strip() == ""


# --- commands that call a model (scripted fake LLM) ------------------------------


@pytest.fixture
def fake_cfg(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("BRAIN_FAKE_LLM", "1")
    p = tmp_path / "config.toml"
    p.write_text(
        f'[storage]\nbackend = "local"\npath = "{tmp_path / "store"}"\n'
        '[llm]\nbase_url = "https://fake/v1"\nsmall_model = "fake-small"\nembed_model = "fake-embed"\n'
    )
    return p


def test_jot_processes_the_note(fake_cfg):
    r = run(fake_cfg, "jot", "-m", "Met Jane Doe about the [[GPU quota migration]]. Does MIG work?")
    assert r.exit_code == 0, r.output
    assert "model call(s)" in r.output
    r = run(fake_cfg, "process")
    assert "nothing to process" in r.output


def test_jot_without_model_endpoint_leaves_the_note_pending(cfg):
    r = run(cfg, "jot", "-m", "hello")
    assert r.exit_code == 0 and "saved and pending" in r.output


def test_show_active_wiki_ask_brief_search_log(fake_cfg):
    run(fake_cfg, "jot", "-m", "Kicked off the [[GPU quota migration]]. Does the vendor support MIG on the nodes?")
    run(fake_cfg, "jot", "-m", "Jane Doe needs to file the ticket for the [[GPU quota migration]].")
    r = run(fake_cfg, "show")
    assert r.exit_code == 0 and "processed" in r.output and "action · GPU quota migration" in r.output
    r = run(fake_cfg, "active")
    assert r.exit_code == 0 and "GPU quota migration" in r.output and "2 open" in r.output
    r = run(fake_cfg, "wiki")
    assert r.exit_code == 0 and "gpu-quota-migration" in r.output and "jane-doe" in r.output
    r = run(fake_cfg, "wiki", "GPU quota migration")
    assert r.exit_code == 0 and r.output.startswith("---\nid: gpu-quota-migration") and "## Open" in r.output
    assert run(fake_cfg, "wiki", "nothing like this").exit_code == 2
    r = run(fake_cfg, "ask", "does the vendor support MIG?")
    assert r.exit_code == 0 and "Sources" in r.output
    r = run(fake_cfg, "brief", "GPU quota migration")
    assert r.exit_code == 0, r.output
    assert "Where things stand" in r.output and "last looked: never" in r.output
    assert "last looked: 20" in run(fake_cfg, "brief", "GPU quota migration").output
    r = run(fake_cfg, "search", "vendor MIG")
    assert r.exit_code == 0 and "Does the vendor support MIG on the nodes?" in r.output
    r = run(fake_cfg, "log")
    assert r.exit_code == 0 and "New person: Jane Doe" in r.output


def test_correction_from_the_command_line(fake_cfg):
    run(fake_cfg, "jot", "-m", "Kicked off the [[GPU quota migration]].")
    run(fake_cfg, "jot", "-m", "More on the [[GPU quota migration]].")
    r = run(fake_cfg, "jot", "-m", "This is finished.", "--about", "GPU quota migration")
    assert r.exit_code == 0, r.output
    assert "status: closed" in run(fake_cfg, "wiki", "gpu-quota-migration").output
    assert "no active threads" in run(fake_cfg, "active").output
    assert run(fake_cfg, "jot", "-m", "x", "--about", "no such page").exit_code == 2


def test_file_saved_into_notes_is_picked_up_by_process(fake_cfg, tmp_path):
    notes = tmp_path / "store" / "notes"
    notes.mkdir(parents=True)
    (notes / "from my editor.md").write_text("Jane Doe is out this week.\n")
    r = run(fake_cfg, "process")
    assert r.exit_code == 0 and "model call(s)" in r.output
    assert "Jane Doe is out this week." in run(fake_cfg, "show").output


def test_rebuild_requires_confirmation(fake_cfg):
    run(fake_cfg, "jot", "-m", "Something happened with the [[Cache rewrite]].")
    r = run(fake_cfg, "rebuild", input="n\n")
    assert r.exit_code == 0 and "rebuilt" not in r.output
    r = run(fake_cfg, "rebuild", "-y")
    assert r.exit_code == 0 and "rebuilt from 1 note(s)" in r.output


def test_context_and_dream_commands(fake_cfg):
    run(fake_cfg, "jot", "-m", "We decided the [[Billing migration]] cutover is in March.")
    run(fake_cfg, "jot", "-m", "We decided the [[Billing migration]] cutover is in May instead.")
    r = run(fake_cfg, "context")
    assert r.exit_code == 0 and r.output.startswith("# brain: ") and "Billing migration [billing-migration]" in r.output
    r = run(fake_cfg, "context", "--format", "json")
    assert r.exit_code == 0 and '"threads"' in r.output
    assert run(fake_cfg, "context", "--format", "xml").exit_code == 2
    r = run(fake_cfg, "dream", "--dry-run")
    assert r.exit_code == 0, r.output
    r = run(fake_cfg, "dream")
    assert r.exit_code == 0 and ("nothing changed" in r.output or "✓" in r.output)
