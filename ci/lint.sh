#!/usr/bin/env sh
# Lint and type-check. Used by GitHub Actions; call it the same way from GitLab CI.
set -eu
uv sync --locked
uv run ruff check src tests
uv run ruff format --check src tests
uv run pyright
