"""Where a running `brain start` can be found, so CLI commands can hand work to it.

`brain start` writes `$XDG_STATE_HOME/brain/server.json` with its pid, port,
and store. `brain jot` reads it and, if that process is alive and serves the
same store, asks it to process the new note instead of doing it in the
foreground.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass

from brain.core.localstate import LocalState

FILE = "server.json"


@dataclass(frozen=True)
class ServerInfo:
    pid: int
    port: int
    store: str

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def write(state: LocalState, info: ServerInfo) -> None:
    state.write_json(FILE, asdict(info))


def read(state: LocalState, store: str) -> ServerInfo | None:
    data = state.read_json(FILE)
    try:
        info = ServerInfo(int(data["pid"]), int(data["port"]), str(data["store"]))
    except (KeyError, TypeError, ValueError):
        return None
    if info.store != store or not _alive(info.pid):
        return None
    return info


def clear(state: LocalState, pid: int) -> None:
    data = state.read_json(FILE)
    if data.get("pid") == pid:
        (state.root / FILE).unlink(missing_ok=True)
