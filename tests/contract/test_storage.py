"""The storage contract. Every backend must pass all of these."""

from __future__ import annotations

import pytest

from brain.core.storage.base import NotFound, PreconditionFailed
from brain.core.storage.probe import probe_conditional_writes


def test_put_get_exists_delete(storage):
    info = storage.put("notes/2026/10/30/a.md", b"hello")
    assert info.key == "notes/2026/10/30/a.md" and info.size == 5 and info.etag
    assert storage.get("notes/2026/10/30/a.md") == b"hello"
    assert storage.exists("notes/2026/10/30/a.md")
    storage.delete("notes/2026/10/30/a.md")
    assert not storage.exists("notes/2026/10/30/a.md")
    storage.delete("notes/2026/10/30/a.md")  # deleting twice is not an error
    with pytest.raises(NotFound):
        storage.get("notes/2026/10/30/a.md")


def test_list_prefix_and_metadata(storage):
    for key in ("notes/2026/10/30/a.md", "notes/2026/10/31/b.md", "people/c.md", "notesX/d.md"):
        storage.put(key, key.encode())
    keys = [o.key for o in storage.list("notes/")]
    assert keys == ["notes/2026/10/30/a.md", "notes/2026/10/31/b.md"]
    assert {o.key for o in storage.list("")} >= {"people/c.md", "notesX/d.md"}
    assert [o.key for o in storage.list("notes/2026/10/3")] == keys
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


def test_rejects_invalid_keys(storage):
    for bad in ("", "/abs", "a/../b", "a//b", "a\\b", ".hidden", "x/.tmp"):
        with pytest.raises(ValueError):
            storage.put(bad, b"x")
