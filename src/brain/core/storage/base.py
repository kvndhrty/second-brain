"""The storage interface every backend implements.

Keys are '/'-separated relative paths such as 'entries/2026/10/30/x.md'. A
backend maps them onto its own namespace (a directory, or a bucket prefix).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class StorageError(Exception):
    """A backend operation failed. The message is meant to be shown to the user."""


class NotFound(StorageError):
    pass


class PreconditionFailed(StorageError):
    """An if_absent or if_match condition did not hold."""


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    etag: str
    size: int
    mtime: datetime


class Storage(Protocol):
    # None until the runtime probe has run; see storage/probe.py.
    supports_conditional_writes: bool | None

    @property
    def ident(self) -> str:
        """Stable identity of the store, used to name the local index directory."""
        ...

    def describe(self) -> str:
        """Human-readable location for messages."""
        ...

    def get(self, key: str) -> bytes: ...

    def put(
        self,
        key: str,
        data: bytes,
        *,
        if_absent: bool = False,
        if_match: str | None = None,
    ) -> ObjectInfo: ...

    def list(self, prefix: str = "") -> Iterator[ObjectInfo]: ...

    def delete(self, key: str) -> None: ...

    def exists(self, key: str) -> bool: ...


def check_key(key: str) -> str:
    if not key or key.startswith("/") or "\\" in key:
        raise ValueError(f"invalid storage key: {key!r}")
    parts = key.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"invalid storage key: {key!r}")
    if any(p.startswith(".") for p in parts):
        # Dot-names are reserved for temp and lock files in the local backend.
        raise ValueError(f"storage key segments may not start with '.': {key!r}")
    return key
