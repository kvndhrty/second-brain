"""Checks against real endpoints. Run with:

    BRAIN_LIVE_TESTS=1 BRAIN_TEST_LIVE_CONFIG=~/.config/brain/config.toml uv run pytest tests/live

The config's API key variable and AWS credentials come from your environment.
"""

from __future__ import annotations

import os

import pytest

from brain.core.doctor import run_doctor

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("BRAIN_LIVE_TESTS") != "1", reason="set BRAIN_LIVE_TESTS=1 to run"),
]


@pytest.fixture
def live_config() -> str:
    path = os.environ.get("BRAIN_TEST_LIVE_CONFIG")
    if not path:
        pytest.skip("set BRAIN_TEST_LIVE_CONFIG to a config file")
    return os.path.expanduser(path)


def test_doctor_has_no_failures(live_config):
    results = run_doctor(live_config)
    failures = [f"{r.name}: {r.detail}" for r in results if r.status == "fail"]
    assert not failures, "\n".join(failures)


def test_pipeline_end_to_end_on_real_models(live_config, tmp_path):
    """Process one note with the real models into a throwaway local store, then ask about it."""
    from brain.core.config import LoadedSettings, load
    from brain.core.services import Brain

    settings = load(live_config).settings
    settings.storage.backend = "local"
    settings.storage.path = str(tmp_path / "store")
    settings.index.path = str(tmp_path / "index.db")
    with Brain(LoadedSettings(settings=settings, path=None, env_overrides=[]), command="pytest-live") as b:
        note = b.jot(
            "Met Priya from the platform team about moving the eval cluster to the new GPU quota pool. "
            "We decided to migrate in December. Open question: does the new pool support MIG partitioning?"
        ).note
        report = b.process(note.id)
        assert not report.failed, report.failed[0].error
        kinds = {r[0] for r in b.db.conn.execute("SELECT kind FROM statements")}
        assert "decision" in kinds and "question" in kinds
        answer = b.ask("When is the eval cluster migrating?")
        assert answer.claims and note.id in {c for claim in answer.claims for c in claim.citations}
