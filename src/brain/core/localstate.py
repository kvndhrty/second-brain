"""Machine-local state that is not part of the store.

Holds the machine id used by the lease, cached probe results, and the local
lock that serializes writes between processes on the same machine.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from brain.core import paths


class LocalState:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or paths.state_dir()

    def _ensure(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    @property
    def machine_id(self) -> str:
        p = self._ensure() / "machine-id"
        try:
            value = p.read_text().strip()
            if value:
                return value
        except FileNotFoundError:
            pass
        value = uuid.uuid4().hex
        tmp = p.with_suffix(".tmp")
        tmp.write_text(value + "\n")
        try:
            # Another process may have created it first; keep whichever won.
            os.link(tmp, p)
        except FileExistsError:
            value = p.read_text().strip()
        finally:
            tmp.unlink(missing_ok=True)
        return value

    def read_json(self, name: str) -> dict[str, Any]:
        try:
            return json.loads((self.root / name).read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def write_json(self, name: str, data: dict[str, Any]) -> None:
        p = self._ensure() / name
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        os.replace(tmp, p)

    @property
    def drafts_dir(self) -> Path:
        d = self._ensure() / "drafts"
        d.mkdir(exist_ok=True)
        return d

    def lease_lock(self, store_ident: str) -> Path:
        """The file brain processes on this machine lock while they use one store's lease."""
        return self._ensure() / f"lease-{hashlib.sha256(store_ident.encode()).hexdigest()[:12]}.lock"

    @contextmanager
    def try_lock(self, name: str) -> Iterator[bool]:
        """Non-blocking exclusive lock; yields False if another process holds it."""
        with open(self._ensure() / f"{name}.lock", "a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    @contextmanager
    def write_lock(self, name: str = "write") -> Iterator[None]:
        """Exclusive lock shared by all brain processes on this machine."""
        with open(self._ensure() / f"{name}.lock", "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
