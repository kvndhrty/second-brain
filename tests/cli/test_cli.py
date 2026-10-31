from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from brain.cli import app as cli
from brain.core import keys
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
    r = run(cfg, "jot", "-m", "Talked to Jane about quotas.", "--title", "Quota", "--tag", "gpu")
    assert r.exit_code == 0, r.output
    assert "saved e-" in r.output
    files = list((tmp_path / "store" / "entries").rglob("*.md"))
    assert len(files) == 1 and files[0].name.endswith("-quota.md")
    assert "tags:\n  - gpu" in files[0].read_text()
    r = run(cfg, "reindex")
    assert r.exit_code == 0 and "1 units rebuilt" in r.output
    r = run(cfg, "sync")
    assert "up to date" in r.output


def test_jot_from_stdin(cfg, tmp_path):
    r = run(cfg, "jot", input="from a pipe\n")
    assert r.exit_code == 0, r.output
    (f,) = (tmp_path / "store" / "entries").rglob("*.md")
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
    assert not list((tmp_path / "store").rglob("entries/**/*.md"))
    assert "--force" in r.output and "jot --file" in r.output  # the text is kept as a draft
    r = run(cfg, "jot", "-m", "hello", "--force")
    assert r.exit_code == 0, r.output


def test_failed_save_keeps_draft(cfg, tmp_path, monkeypatch):
    from brain.core import vault

    def boom(self, *a, **k):
        raise OSError("disk full")

    with monkeypatch.context() as m:  # undo only this patch, not the env isolation
        m.setattr(vault.Vault, "save_entry", boom)
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


def test_keys_layout():
    from datetime import UTC, datetime

    dt = datetime(2026, 10, 30, 23, 30, 5, tzinfo=UTC)
    eid = keys.entry_id_for(dt)
    assert keys.entry_key(dt, eid, "vendor-call") == "entries/2026/10/30/20261030T233005Z-vendor-call.md"
    assert keys.classify("generated/embeddings/text-embed_3/e-1.json").model_slug == "text-embed_3"
    assert keys.classify("decisions/e-1.yaml").unit == "entry:e-1"
    assert keys.classify("threads/gpu.md").entity_type == "thread"
    assert keys.classify("_system/lease.json") is None
    assert keys.slugify("Ünïcode & Punctuation!! 2026 plan") == "unicode-punctuation-2026-plan"
