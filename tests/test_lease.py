from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from brain.core import keys
from brain.core.storage.lease import Lease, LeaseHeld, read_lease
from brain.core.storage.probe import ensure_probed, probe_conditional_writes


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class IgnoresConditions:
    """A store that accepts conditional headers and ignores them, like some S3-compatible servers."""

    supports_conditional_writes = None

    def __init__(self, inner) -> None:
        self.inner = inner

    @property
    def ident(self):
        return "ignoring:" + self.inner.ident

    def describe(self):
        return "ignoring store"

    def put(self, key, data, *, if_absent=False, if_match=None):
        return self.inner.put(key, data)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def lease(storage, machine="m1", pid=100, clock=None, **kw) -> Lease:
    storage.supports_conditional_writes = (
        True if storage.supports_conditional_writes is None else storage.supports_conditional_writes
    )
    return Lease(
        storage,
        machine_id=machine,
        ttl_seconds=600,
        command="test",
        clock=clock or Clock(),
        pid=pid,
        sleep=lambda s: None,
        **kw,
    )


def test_acquire_and_release(local_storage):
    a = lease(local_storage)
    a.acquire()
    rec, _ = read_lease(local_storage)
    assert rec.machine_id == "m1" and rec.pid == 100
    a.release()
    assert read_lease(local_storage) is None


def test_other_machine_is_refused_with_clear_message(storage):
    clock = Clock()
    lease(storage, "m1", clock=clock).acquire()
    with pytest.raises(LeaseHeld) as e:
        lease(storage, "m2", clock=clock).acquire()
    assert "--force" in str(e.value)
    assert e.value.record.machine_id == "m1"


def test_forced_or_expired_lease_is_taken_over(local_storage):
    clock = Clock()
    lease(local_storage, "m1", clock=clock).acquire()
    lease(local_storage, "m2", clock=clock, force=True).acquire()
    assert read_lease(local_storage)[0].machine_id == "m2"
    clock.advance(601)
    lease(local_storage, "m3", clock=clock).acquire()
    assert read_lease(local_storage)[0].machine_id == "m3"


def test_short_command_does_not_delete_a_lease_another_process_still_uses(local_storage, tmp_path):
    clock = Clock()
    lock = tmp_path / "lease.lock"
    server = lease(local_storage, "m1", pid=1, clock=clock, local_lock=lock)
    server.acquire()
    cli = lease(local_storage, "m1", pid=2, clock=clock, local_lock=lock)
    cli.acquire()  # shares the lease; the record now carries pid 2
    cli.release()  # the short command exits while the server is still running
    with pytest.raises(LeaseHeld):
        lease(local_storage, "m2", clock=clock).acquire()
    server.release()  # the last process on this machine
    assert read_lease(local_storage) is None


def test_renew_detects_lost_lease(local_storage):
    clock = Clock()
    a = lease(local_storage, "m1", clock=clock)
    a.acquire()
    clock.advance(700)
    lease(local_storage, "m2", clock=clock).acquire()  # m1 expired; m2 takes it
    with pytest.raises(LeaseHeld):
        a.renew()


def test_ensure_renews_after_a_third_of_ttl(local_storage):
    clock = Clock()
    a = lease(local_storage, "m1", clock=clock)
    a.ensure()
    first = read_lease(local_storage)[0].renewed_at
    clock.advance(100)
    a.ensure()
    assert read_lease(local_storage)[0].renewed_at == first
    clock.advance(150)
    a.ensure()
    assert read_lease(local_storage)[0].renewed_at != first


def test_probe_detects_ignored_conditions(local_storage):
    result = probe_conditional_writes(IgnoresConditions(local_storage))
    assert not result.enforced
    assert "ignored" in result.detail


def test_probe_result_is_cached(local_storage, state):
    first = ensure_probed(local_storage, state, now=1000.0)
    second = ensure_probed(local_storage, state, now=2000.0)
    assert first.enforced and second.enforced
    assert "(cached)" in second.detail
    third = ensure_probed(local_storage, state, now=1000.0 + 25 * 3600)
    assert "(cached)" not in third.detail


def test_fallback_detects_concurrent_writer(local_storage):
    store = IgnoresConditions(local_storage)
    store.supports_conditional_writes = False
    clock = Clock()
    other = Lease(store, machine_id="m2", ttl_seconds=600, command="t", clock=clock, pid=2, sleep=lambda s: None)

    def racing_sleep(_seconds: float) -> None:
        # While we wait to read back, another machine overwrites the lease.
        other.acquire(force=True)

    a = Lease(store, machine_id="m1", ttl_seconds=600, command="t", clock=clock, sleep=racing_sleep, pid=1)
    with pytest.raises(LeaseHeld):
        a.acquire()


def test_garbage_lease_is_replaced(local_storage):
    local_storage.put(keys.LEASE, b"not json")
    lease(local_storage).acquire()
    assert read_lease(local_storage)[0].machine_id == "m1"
