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


# --- milestone 2 commands (scripted fake LLM) ------------------------------------


@pytest.fixture
def fake_cfg(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("BRAIN_FAKE_LLM", "1")
    p = tmp_path / "config.toml"
    p.write_text(
        f'[storage]\nbackend = "local"\npath = "{tmp_path / "store"}"\n'
        '[llm]\nbase_url = "https://fake/v1"\nchat_model = "fake-chat"\nembed_model = "fake-embed"\n'
    )
    return p


def test_jot_processes_and_points_to_review(fake_cfg):
    r = run(fake_cfg, "jot", "-m", "Met Jane Doe about the [[GPU quota migration]]. Does MIG work?")
    assert r.exit_code == 0, r.output
    assert "extract, embed, resolve" in r.output and "brain review" in r.output
    r = run(fake_cfg, "process")
    assert "nothing to process" in r.output


def test_jot_without_model_endpoint_leaves_extraction_pending(cfg):
    r = run(cfg, "jot", "-m", "hello")
    assert r.exit_code == 0 and "extraction is pending" in r.output


def test_show_status_active_ask_brief(fake_cfg):
    run(fake_cfg, "jot", "-m", "Kicked off the [[GPU quota migration]]. Does the vendor support MIG on the nodes?")
    r = run(fake_cfg, "show")
    assert r.exit_code == 0 and "processed" in r.output
    frag = next(w for w in r.output.split() if w.startswith("f-e-") and w.endswith("-02"))
    r = run(fake_cfg, "status", frag, "--obe", "--reason", "vendor left")
    assert r.exit_code == 0, r.output
    assert "overcome_by_events" in run(fake_cfg, "show").output
    r = run(fake_cfg, "status", frag)
    assert r.exit_code == 2 and "nothing to change" in r.output
    r = run(fake_cfg, "active")
    assert "GPU quota" in r.output and "proposed" in r.output
    r = run(fake_cfg, "ask", "does the vendor support MIG?")
    assert r.exit_code == 0 and "Sources" in r.output
    r = run(fake_cfg, "brief", "GPU quota migration")
    assert r.exit_code == 0, r.output
    assert "Where things stand" in r.output and "last looked: never" in r.output
    assert "last looked: 20" in run(fake_cfg, "brief", "GPU quota migration").output


def test_review_creates_person_and_accepts_thread(fake_cfg, tmp_path):
    run(fake_cfg, "jot", "-m", "Paired with Zed on the [[Cache rewrite]].")
    # Person: create "Zed Shaw" with a role and no org. Thread: accept.
    r = run(fake_cfg, "review", input="c\nZed Shaw\nStaff engineer\n\na\n")
    assert r.exit_code == 0, r.output
    assert "2 decided" in r.output
    person = (tmp_path / "store" / "people" / "zed-shaw.md").read_text()
    assert "role: Staff engineer" in person and "- Zed" in person
    assert "state: active" in (tmp_path / "store" / "threads" / "cache-rewrite.md").read_text()


def test_reextract_requires_confirmation(fake_cfg):
    run(fake_cfg, "jot", "-m", "Something happened.")
    r = run(fake_cfg, "reextract", input="n\n")
    assert "Re-extract all 1 entries" in r.output
    r = run(fake_cfg, "reextract", "-y")
    assert r.exit_code == 0 and "extract" in r.output


def test_thread_commands_and_sweep(fake_cfg, tmp_path):
    run(fake_cfg, "jot", "-m", "Started the [[Cache rewrite]]. Need to benchmark the [[Cache rewrite]].")
    r = run(fake_cfg, "thread", "list")
    assert "Cache rewrite" in r.output and "cache-rewrite" in r.output
    assert run(fake_cfg, "thread", "rename", "cache-rewrite", "Cache v2").exit_code == 0
    assert run(fake_cfg, "thread", "pin", "cache-rewrite").exit_code == 0
    r = run(fake_cfg, "thread", "show", "cache-rewrite")
    frag = next(w for w in r.output.split() if w.startswith("f-e-") and w.endswith("-02"))
    r = run(fake_cfg, "thread", "split", "cache-rewrite", frag, "--name", "Benchmarks")
    assert r.exit_code == 0 and "created thread benchmarks" in r.output
    r = run(fake_cfg, "thread", "merge", "benchmarks", "nope")
    assert r.exit_code == 1 and "no thread nope" in r.output
    r = run(fake_cfg, "thread", "obe", "benchmarks", "--reason", "dropped", "--cascade")
    assert "1 open items marked" in r.output
    assert "state: obe" in (tmp_path / "store" / "threads" / "benchmarks.md").read_text()
    assert "nothing stale" in run(fake_cfg, "sweep").output
    run(fake_cfg, "jot", "-m", "Does the cache need warming?")
    r = run(fake_cfg, "sweep", "--days", "0", "--quiet-days", "0", input="c\n")
    assert r.exit_code == 0 and "Still open" in r.output and "sweep done: 1 changed" in r.output
    assert "nothing stale" in run(fake_cfg, "sweep", "--days", "0", "--quiet-days", "0").output


def test_context_and_dream_commands(fake_cfg):
    import json

    run(fake_cfg, "jot", "-m", "We decided the [[Billing migration]] cutover is in March.")
    run(fake_cfg, "jot", "-m", "The [[Billing migration]] cutover moved to May instead of March.")
    r = run(fake_cfg, "context")
    assert r.exit_code == 0, r.output
    assert r.output.startswith("# brain: ") and "Billing migration [billing-migration]" in r.output
    r = run(fake_cfg, "context", "--format", "json")
    assert json.loads(r.output)["threads"][0]["id"] == "billing-migration"
    r = run(fake_cfg, "dream", "--dry-run")
    assert r.exit_code == 0 and "look for superseded fragments in Billing migration" in r.output
    r = run(fake_cfg, "dream")
    assert r.exit_code == 0, r.output
    assert "wrote the Now line for thread billing-migration" in r.output and "brain review" in r.output
    r = run(fake_cfg, "dream")
    assert "nothing to do" in r.output
    r = run(fake_cfg, "review", input="y\n")
    assert "Has this been overcome by events?" in r.output
    r = run(fake_cfg, "doctor")
    assert "last pass" in r.output
