"""The storage contract. Every backend must pass all of these."""

from __future__ import annotations

import pytest

from brain.core.storage.base import NotFound, PreconditionFailed
from brain.core.storage.probe import probe_conditional_writes


def test_put_get_roundtrip(storage):
    info = storage.put("entries/2026/10/30/a.md", b"hello")
    assert info.key == "entries/2026/10/30/a.md"
    assert info.size == 5
    assert info.etag
    assert storage.get("entries/2026/10/30/a.md") == b"hello"


def test_get_missing_raises_not_found(storage):
    with pytest.raises(NotFound):
        storage.get("nope/missing.md")


def test_exists_and_delete(storage):
    storage.put("people/jane.md", b"x")
    assert storage.exists("people/jane.md")
    storage.delete("people/jane.md")
    assert not storage.exists("people/jane.md")
    storage.delete("people/jane.md")  # deleting twice is not an error


def test_list_prefix_and_metadata(storage):
    for key in ("entries/2026/10/30/a.md", "entries/2026/10/31/b.md", "people/c.md", "entriesX/d.md"):
        storage.put(key, key.encode())
    keys = [o.key for o in storage.list("entries/")]
    assert keys == ["entries/2026/10/30/a.md", "entries/2026/10/31/b.md"]
    assert {o.key for o in storage.list("")} >= {"people/c.md", "entriesX/d.md"}
    assert [o.key for o in storage.list("entries/2026/10/3")] == keys
    (obj,) = [o for o in storage.list("people/")]
    assert obj.size == len(b"people/c.md")
    assert obj.etag
    assert list(storage.list("missing/")) == []


def test_etag_changes_on_overwrite(storage):
    first = storage.put("k/a.json", b"one")
    second = storage.put("k/a.json", b"two!")
    assert first.etag != second.etag
    listed = next(iter(storage.list("k/a.json")))
    assert listed.etag == second.etag


def test_if_absent(storage):
    storage.put("_system/lease.json", b"1", if_absent=True)
    with pytest.raises(PreconditionFailed):
        storage.put("_system/lease.json", b"2", if_absent=True)
    assert storage.get("_system/lease.json") == b"1"


def test_if_match(storage):
    info = storage.put("_system/lease.json", b"1")
    with pytest.raises(PreconditionFailed):
        storage.put("_system/lease.json", b"2", if_match="0" * 32)
    new = storage.put("_system/lease.json", b"3", if_match=info.etag)
    assert storage.get("_system/lease.json") == b"3"
    with pytest.raises(PreconditionFailed):
        storage.put("_system/lease.json", b"4", if_match=info.etag)  # stale etag
    storage.put("_system/lease.json", b"5", if_match=new.etag)


def test_probe_reports_enforced(storage):
    result = probe_conditional_writes(storage)
    assert result.enforced, result.detail
    assert list(storage.list("_system/probe/")) == []


@pytest.mark.parametrize("bad", ["", "/abs", "a/../b", "a//b", "a\\b", ".hidden", "x/.tmp"])
def test_rejects_invalid_keys(storage, bad):
    with pytest.raises(ValueError):
        storage.put(bad, b"x")
