"""Single-writer lease: which machine may write to the store, and until when.

The lease belongs to a machine, not a process. `brain start` and short CLI
commands on the same machine share it; they serialize their writes with a
local lock (LocalState.write_lock). Each process that uses the lease holds a
shared lock on a local file while it does, and the lease is deleted on exit
only by a process that finds no other holder of that lock. See
docs/architecture.md, section 6.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO

from brain.core import keys
from brain.core.storage.base import NotFound, PreconditionFailed, Storage


@dataclass
class LeaseRecord:
    holder_host: str
    machine_id: str
    pid: int
    command: str
    acquired_at: str
    renewed_at: str
    ttl_seconds: int
    token: str

    def expires_at(self) -> datetime:
        return datetime.fromisoformat(self.renewed_at) + timedelta(seconds=self.ttl_seconds)

    def expired(self, now: datetime) -> bool:
        return now >= self.expires_at()

    def age(self, now: datetime) -> timedelta:
        return now - datetime.fromisoformat(self.renewed_at)


class LeaseError(Exception):
    pass


class LeaseHeld(LeaseError):
    def __init__(self, record: LeaseRecord, now: datetime) -> None:
        self.record = record
        age = int(record.age(now).total_seconds())
        left = int((record.expires_at() - now).total_seconds())
        super().__init__(
            f"Another machine holds the write lease on this store: {record.holder_host} "
            f"(pid {record.pid}, `{record.command}`), last renewed {_fmt_secs(age)} ago, "
            f"expires in {_fmt_secs(left)}.\n"
            "If that machine is no longer using this store, re-run with --force to take the lease."
        )


def _fmt_secs(s: int) -> str:
    s = max(s, 0)
    if s < 120:
        return f"{s}s"
    if s < 7200:
        return f"{s // 60}m"
    return f"{s // 3600}h"


def read_lease(storage: Storage) -> tuple[LeaseRecord, str] | None:
    """Return (record, etag) or None if there is no lease object."""
    try:
        data = storage.get(keys.LEASE)
    except NotFound:
        return None
    etag = next((o.etag for o in storage.list(keys.LEASE) if o.key == keys.LEASE), "")
    try:
        return LeaseRecord(**json.loads(data)), etag
    except (TypeError, ValueError, json.JSONDecodeError):
        # Unreadable lease: treat as expired so it can be replaced.
        epoch = datetime(1970, 1, 1, tzinfo=UTC).isoformat()
        return LeaseRecord("?", "?", 0, "?", epoch, epoch, 0, "?"), etag


class Lease:
    def __init__(
        self,
        storage: Storage,
        *,
        machine_id: str,
        ttl_seconds: int,
        command: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
        settle_seconds: float = 2.0,
        pid: int | None = None,
        force: bool = False,
        local_lock: Path | None = None,
    ) -> None:
        self.storage = storage
        self.machine_id = machine_id
        self.ttl = ttl_seconds
        self.command = command
        self.clock = clock
        self.sleep = sleep
        self.settle = settle_seconds
        self.pid = os.getpid() if pid is None else pid
        self.force = force  # take the lease even if another machine holds it
        self.held = False
        self._last_check: datetime | None = None
        # A file every brain process on this machine locks (shared) while it uses this store's lease.
        self._lock_path = local_lock
        self._lock_file: IO[str] | None = None

    def _join_local(self) -> None:
        if self._lock_path is not None and self._lock_file is None:
            f = open(self._lock_path, "a")  # noqa: SIM115 - held until release
            fcntl.flock(f, fcntl.LOCK_SH)
            self._lock_file = f

    def _leave_local(self) -> bool:
        """Stop using the lease. True if no other process on this machine is using it.

        When True, the local lock is held exclusively until `_close_local`, so
        a process starting now waits until the lease has been deleted.
        """
        if self._lock_file is None:
            return False
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def _close_local(self) -> None:
        if self._lock_file is not None:
            self._lock_file.close()  # closing drops the lock
            self._lock_file = None

    def _new_record(self, token: str | None = None, acquired_at: str | None = None) -> LeaseRecord:
        now = self.clock().isoformat()
        return LeaseRecord(
            holder_host=socket.gethostname(),
            machine_id=self.machine_id,
            pid=self.pid,
            command=self.command,
            acquired_at=acquired_at or now,
            renewed_at=now,
            ttl_seconds=self.ttl,
            token=token or uuid.uuid4().hex,
        )

    @staticmethod
    def _encode(rec: LeaseRecord) -> bytes:
        return json.dumps(asdict(rec), indent=2).encode()

    def acquire(self, *, force: bool | None = None) -> None:
        force = self.force if force is None else force
        self._join_local()
        try:
            if self.storage.supports_conditional_writes:
                self._acquire_conditional(force)
            else:
                self._acquire_fallback(force)
        except BaseException:
            self._close_local()
            raise
        self.held = True
        self._last_check = self.clock()

    def _acquire_conditional(self, force: bool) -> None:
        for _ in range(3):
            rec = self._new_record()
            try:
                self.storage.put(keys.LEASE, self._encode(rec), if_absent=True)
                return
            except PreconditionFailed:
                pass
            current = read_lease(self.storage)
            if current is None:
                continue  # released between our two calls; retry
            cur, etag = current
            now = self.clock()
            if cur.machine_id == self.machine_id and not cur.expired(now):
                # Same machine: share it. Renew so it carries this process's pid.
                try:
                    self._write_renewal(cur, etag)
                    return
                except PreconditionFailed:
                    continue
            if not (cur.expired(now) or force):
                raise LeaseHeld(cur, now)
            try:
                self.storage.put(keys.LEASE, self._encode(rec), if_match=etag)
                return
            except PreconditionFailed:
                continue  # someone else changed it; look again
        current = read_lease(self.storage)
        if current:
            raise LeaseHeld(current[0], self.clock())
        raise LeaseError("could not acquire the lease after 3 attempts")

    def _acquire_fallback(self, force: bool) -> None:
        # Best effort for stores that do not enforce conditional writes:
        # write, wait, and read back to see whether another writer replaced it.
        current = read_lease(self.storage)
        now = self.clock()
        if current:
            cur, _ = current
            if cur.machine_id == self.machine_id and not cur.expired(now):
                self._write_renewal(cur, None)
                return
            if not (cur.expired(now) or force):
                raise LeaseHeld(cur, now)
        rec = self._new_record()
        self.storage.put(keys.LEASE, self._encode(rec))
        self.sleep(self.settle)
        after = read_lease(self.storage)
        if after is None or after[0].token != rec.token:
            if after:
                raise LeaseHeld(after[0], self.clock())
            raise LeaseError("lease disappeared immediately after being written")

    def _write_renewal(self, cur: LeaseRecord, etag: str | None) -> None:
        rec = self._new_record(token=cur.token, acquired_at=cur.acquired_at)
        if self.storage.supports_conditional_writes and etag is not None:
            self.storage.put(keys.LEASE, self._encode(rec), if_match=etag)
        else:
            self.storage.put(keys.LEASE, self._encode(rec))

    def ensure(self) -> None:
        """Make sure this machine holds the lease. Call before each write batch."""
        if not self.held:
            self.acquire()
            return
        now = self.clock()
        if self._last_check is None or (now - self._last_check).total_seconds() >= self.ttl / 3:
            self.renew()

    def renew(self) -> None:
        current = read_lease(self.storage)
        now = self.clock()
        if current is None:
            self.held = False
            self.acquire()
            return
        cur, etag = current
        if cur.machine_id != self.machine_id:
            self.held = False
            if cur.expired(now):
                self.acquire()
                return
            raise LeaseHeld(cur, now)
        try:
            self._write_renewal(cur, etag)
        except PreconditionFailed:
            self.held = False
            self.acquire()
            return
        self._last_check = now

    def release(self) -> None:
        """Delete the lease if no other process on this machine is using it.

        If another one is (`brain start` while `brain jot` exits), the lease
        stays: deleting it would let another machine write while that process
        still believes it holds the lease.
        """
        if not self.held:
            return
        self.held = False
        try:
            if self._lock_path is not None:
                last = self._leave_local()
            else:
                last = None  # no local lock: the pid on the lease is all there is to go by
            if last is False:
                return
            current = read_lease(self.storage)
            if current is None:
                return
            cur, _ = current
            if cur.machine_id == self.machine_id and (last or cur.pid == self.pid):
                self.storage.delete(keys.LEASE)
        finally:
            self._close_local()
