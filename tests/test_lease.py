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


def test_acquire_and_release(storage):
    a = lease(storage)
    a.acquire()
    rec, _ = read_lease(storage)
    assert rec.machine_id == "m1" and rec.pid == 100
    a.release()
    assert read_lease(storage) is None


def test_other_machine_is_refused_with_clear_message(storage):
    clock = Clock()
    lease(storage, "m1", clock=clock).acquire()
    with pytest.raises(LeaseHeld) as e:
        lease(storage, "m2", clock=clock).acquire()
    assert "--force" in str(e.value)
    assert e.value.record.machine_id == "m1"


def test_force_takes_over(storage):
    clock = Clock()
    lease(storage, "m1", clock=clock).acquire()
    lease(storage, "m2", clock=clock, force=True).acquire()
    assert read_lease(storage)[0].machine_id == "m2"


def test_expired_lease_is_taken_over(storage):
    clock = Clock()
    lease(storage, "m1", clock=clock).acquire()
    clock.advance(601)
    lease(storage, "m2", clock=clock).acquire()
    assert read_lease(storage)[0].machine_id == "m2"


def test_same_machine_shares_and_only_last_renewer_releases(storage):
    clock = Clock()
    server = lease(storage, "m1", pid=1, clock=clock)
    server.acquire()
    cli = lease(storage, "m1", pid=2, clock=clock)
    cli.acquire()  # shares; renewal records pid 2
    assert read_lease(storage)[0].pid == 2
    server.renew()  # long-running process renews; record carries pid 1 again
    cli.release()  # pid 2 is no longer recorded, so the lease stays
    assert read_lease(storage) is not None
    server.release()
    assert read_lease(storage) is None


def test_renew_detects_lost_lease(storage):
    clock = Clock()
    a = lease(storage, "m1", clock=clock)
    a.acquire()
    clock.advance(700)
    lease(storage, "m2", clock=clock).acquire()  # m1 expired; m2 takes it
    with pytest.raises(LeaseHeld):
        a.renew()


def test_ensure_renews_after_a_third_of_ttl(storage):
    clock = Clock()
    a = lease(storage, "m1", clock=clock)
    a.ensure()
    first = read_lease(storage)[0].renewed_at
    clock.advance(100)
    a.ensure()
    assert read_lease(storage)[0].renewed_at == first
    clock.advance(150)
    a.ensure()
    assert read_lease(storage)[0].renewed_at != first


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


def test_fallback_mode_uses_read_back(local_storage):
    store = IgnoresConditions(local_storage)
    store.supports_conditional_writes = False
    clock = Clock()
    a = Lease(store, machine_id="m1", ttl_seconds=600, command="t", clock=clock, sleep=lambda s: None, pid=1)
    a.acquire()
    b = Lease(store, machine_id="m2", ttl_seconds=600, command="t", clock=clock, sleep=lambda s: None, pid=2)
    with pytest.raises(LeaseHeld):
        b.acquire()


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
