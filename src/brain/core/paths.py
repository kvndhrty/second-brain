"""XDG base directories.

The XDG layout is used on macOS too (not ~/Library), so a config written on one
machine can be copied to another without moving it.
"""

from __future__ import annotations

import os
from pathlib import Path

APP = "brain"


def _xdg(var: str, default: str) -> Path:
    value = os.environ.get(var, "")
    # The XDG spec says relative paths must be ignored.
    if value and os.path.isabs(value):
        return Path(value)
    return Path.home() / default


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / APP


def cache_dir() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache") / APP


def data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share") / APP


def state_dir() -> Path:
    """Machine-local state: machine id, probe results, unsent drafts."""
    return _xdg("XDG_STATE_HOME", ".local/state") / APP


def default_config_path() -> Path:
    return config_dir() / "config.toml"
