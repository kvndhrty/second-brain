"""Runtime test of whether a store enforces conditional writes.

Some S3-compatible stores accept `If-None-Match` and `If-Match` headers and
ignore them, returning success. That cannot be detected from a single
response, so the probe creates an object and then checks that a second
create, and an update with a wrong ETag, are both rejected.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

from brain.core import keys
from brain.core.localstate import LocalState
from brain.core.storage.base import PreconditionFailed, Storage, StorageError

CACHE_FILE = "probe.json"
CACHE_SECONDS = 24 * 3600


@dataclass(frozen=True)
class ProbeResult:
    enforced: bool
    detail: str


def probe_conditional_writes(storage: Storage) -> ProbeResult:
    key = f"{keys.PROBE_PREFIX}{secrets.token_hex(8)}"
    try:
        try:
            first = storage.put(key, b"1", if_absent=True)
        except StorageError as e:
            # e.g. 501 NotImplemented or 400 for an unknown header.
            return ProbeResult(False, f"store rejected If-None-Match on a new object: {e}")
        try:
            storage.put(key, b"2", if_absent=True)
            return ProbeResult(False, "store ignored If-None-Match (overwrote an existing object)")
        except PreconditionFailed:
            pass
        except StorageError as e:
            return ProbeResult(False, f"unexpected error testing If-None-Match: {e}")
        wrong = "0" * 32 if first.etag != "0" * 32 else "1" * 32
        try:
            storage.put(key, b"3", if_match=wrong)
            return ProbeResult(False, "store ignored If-Match (accepted a write with a wrong ETag)")
        except PreconditionFailed:
            pass
        except StorageError as e:
            return ProbeResult(False, f"unexpected error testing If-Match: {e}")
        return ProbeResult(True, "If-None-Match and If-Match are enforced")
    finally:
        try:
            storage.delete(key)
        except StorageError:
            pass


def ensure_probed(storage: Storage, state: LocalState, *, now: float | None = None) -> ProbeResult:
    """Set storage.supports_conditional_writes, using a cached result if fresh."""
    now = time.time() if now is None else now
    cache = state.read_json(CACHE_FILE)
    entry = cache.get(storage.ident)
    if entry and now - entry.get("at", 0) < CACHE_SECONDS:
        result = ProbeResult(bool(entry["enforced"]), entry["detail"] + " (cached)")
    else:
        result = probe_conditional_writes(storage)
        cache[storage.ident] = {"enforced": result.enforced, "detail": result.detail, "at": now}
        state.write_json(CACHE_FILE, cache)
    storage.supports_conditional_writes = result.enforced
    return result
