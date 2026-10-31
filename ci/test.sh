#!/usr/bin/env sh
# Run the test suite with a uv-managed Python (PYTHON, default 3.12).
# S3 contract tests use BRAIN_TEST_S3_ENDPOINT if set, else start MinIO with
# podman or docker if available; BRAIN_TEST_REQUIRE_S3=1 makes a missing S3
# server a failure instead of a skip.
set -eu
PYTHON="${PYTHON:-3.12}"
uv sync --locked --python "$PYTHON"
uv run --python "$PYTHON" pytest "$@"
