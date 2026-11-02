"""One Brain shared by the request handlers of a long-running front-end.

Web requests and MCP tool calls run on worker threads. The Brain and its
SQLite connection are used by one call at a time, so every call takes one
lock. The Brain opens on first use, so a storage problem is reported by the
request instead of stopping the server.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TypeVar

from brain.core.services import Brain

T = TypeVar("T")


class SharedBrain:
    def __init__(self, open_brain: Callable[[], Brain]) -> None:
        self._open = open_brain
        self._brain: Brain | None = None
        self.lock = threading.Lock()

    def call(self, fn: Callable[[Brain], T], *, sync: bool = True) -> T:
        with self.lock:
            if self._brain is None:
                self._brain = self._open()
            if sync:
                self._brain.sync()
            return fn(self._brain)

    def close(self) -> None:
        with self.lock:
            if self._brain is not None:
                self._brain.close()
                self._brain = None
