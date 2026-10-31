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
