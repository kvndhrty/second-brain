"""Index built from files: correctness, incremental updates, and reindex equivalence."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from brain.core import keys
from brain.core.formats.decisions import Decisions, FragmentDecision
from brain.core.formats.entity import Org, Person, Theme, Thread
from brain.core.formats.entry import content_hash
from brain.core.formats.sidecars import (
    EmbeddingFile,
    EmbeddingItem,
    Extraction,
    ResolutionFile,
    ThreadSummary,
    encode_vector,
)
from brain.core.index import search
from brain.core.index.db import snapshot
from brain.core.llm.fake import fake_embedding
from tests.conftest import make_brain

STAMP = {"name": "extract", "version": 1, "sha256": "test"}
DIM = 8
BODY = (
    "Met Jane and Mike about the GPU quota migration. We decided to move the eval cluster in December. "
    "Open question: does the vendor support MIG? Someday: a self-serve quota portal."
)


def fragments(entry_id: str) -> list[dict]:
    return [
        {
            "id": f"f-{entry_id}-01",
            "kind": "decision",
            "text": "Move the eval cluster to the new quota pool in December.",
            "span": [44, 101],
            "quote": "We decided to move the eval cluster in December.",
            "mentions": [{"surface": "Jane", "type": "person"}, {"surface": "Mike", "type": "person"}],
            "themes": [{"surface": "capacity planning", "kind": "domain"}],
            "threads": [{"thread": "gpu-quota", "weight": 0.9}],
        },
        {
            "id": f"f-{entry_id}-02",
            "kind": "question",
            "text": "Does the vendor support MIG?",
            "mentions": [{"surface": "the vendor", "type": "org"}],
            "threads": [{"thread": "gpu-quota", "weight": 0.7}, {"new": {"name": "Vendor eval"}, "weight": 0.5}],
        },
        {
            "id": f"f-{entry_id}-03",
            "kind": "idea",
            "text": "A self-serve quota portal.",
            "status": "speculative",
            "dream": True,
        },
    ]


def seed(brain):
    """A store with entities, one processed entry, and one unprocessed entry."""
    v = brain.vault
    v.put_entity(
        Person(id="jane-doe", name="Jane Doe", aliases=["Jane"], memberships=[{"org": "platform", "from": "2025-03"}])
    )
    v.put_entity(Org(id="platform", name="Platform Team", aliases=["PT"]))
    v.put_entity(Theme(id="capacity-planning", name="Capacity planning", kind="domain"))
    v.put_entity(Thread(id="gpu-quota", name="GPU quota migration"))
    v.put_entity(Thread(id="vendor-eval", name="Vendor eval", state="proposed"))
    entry = brain.jot(BODY, title="Quota sync").entry
    eid = entry.id
    v.put_bytes(
        keys.extraction_key(eid),
        Extraction(
            entry_id=eid,
            entry_hash=entry.content_hash,
            model="fake",
            prompt=STAMP,
            created_at=datetime(2026, 10, 30, tzinfo=UTC),
            fragments=fragments(eid),
        ).to_bytes(),
    )
    v.put_bytes(
        keys.resolution_key(eid),
        ResolutionFile(
            entry_id=eid,
            extraction_hash="x",
            model="fake",
            prompt=STAMP,
            created_at=datetime(2026, 10, 30, tzinfo=UTC),
            mentions=[
                {"surface": "Jane", "type": "person", "decision": "link", "entity_id": "jane-doe", "confidence": 0.95},
                {
                    "surface": "Mike",
                    "type": "person",
                    "decision": "new",
                    "proposed": {"name": "Mike"},
                    "confidence": 0.4,
                },
                {
                    "surface": "the vendor",
                    "type": "org",
                    "decision": "pending",
                    "confidence": 0.3,
                    "candidates": [{"entity_id": "platform", "score": 0.2}],
                },
                {"surface": "capacity planning", "type": "theme", "decision": "link", "entity_id": "capacity-planning"},
                {"surface": "Vendor eval", "type": "thread", "decision": "link", "entity_id": "vendor-eval"},
            ],
        ).to_bytes(),
    )
    texts = {eid: entry.body} | {f["id"]: f["text"] for f in fragments(eid)}
    v.put_bytes(
        keys.embedding_key("embed-model", eid),
        EmbeddingFile(
            entry_id=eid,
            model="embed-model",
            dim=DIM,
            items=[
                EmbeddingItem(id=i, hash=content_hash(t), vec=encode_vector(fake_embedding(t, DIM)))
                for i, t in texts.items()
            ],
        ).to_bytes(),
    )
    other = brain.jot("Unrelated note about lunch.").entry
    return eid, other.id


@pytest.fixture
def brain(local_storage, state, tmp_path):
    b = make_brain(local_storage, state, tmp_path, llm={"embed_model": "embed-model"})
    yield b
    b.close()


def rows(brain, sql, *args):
    return [tuple(r) for r in brain.db.conn.execute(sql, args).fetchall()]


def test_index_reflects_files(brain):
    eid, other = seed(brain)
    assert rows(brain, "SELECT id, extraction FROM entries ORDER BY id") == [(eid, "current"), (other, "pending")]
    frags = {r[0]: r[1:] for r in rows(brain, "SELECT id, kind, status, dream, state FROM fragments")}
    assert frags[f"f-{eid}-01"] == ("decision", "confirmed", 0, None)
    assert frags[f"f-{eid}-02"] == ("question", "confirmed", 0, "open")
    assert frags[f"f-{eid}-03"] == ("idea", "speculative", 1, None)

    f1, f2 = f"frag:f-{eid}-01", f"frag:f-{eid}-02"
    edges = set(rows(brain, "SELECT src, dst, type, origin, confirmed FROM edges WHERE type != 'contains'"))
    assert (f1, "person:jane-doe", "mentions", "llm", 0) in edges
    assert (f1, "theme:capacity-planning", "about", "llm", 0) in edges
    assert (f1, "thread:gpu-quota", "in_thread", "llm", 0) in edges
    assert (f2, "thread:vendor-eval", "in_thread", "llm", 0) in edges
    assert ("person:jane-doe", "org:platform", "member_of", "user", 1) in edges
    assert not any(e[1] == "person:mike" for e in edges)

    review = {r[0]: json.loads(r[1]) for r in rows(brain, "SELECT kind, payload FROM review_items")}
    assert review["new_person"]["surface"] == "Mike"
    assert review["ambiguous_mention"]["surface"] == "the vendor"
    assert review["proposed_thread"]["thread"] == "vendor-eval"

    threads = dict(rows(brain, "SELECT id, last_activity FROM threads"))
    assert threads["gpu-quota"] is not None and threads["vendor-eval"] is not None

    assert rows(brain, "SELECT node_id FROM aliases WHERE alias = 'jane'") == [("person:jane-doe",)]
    hits = search.keyword(brain.db.conn, "vendor MIG?")
    assert hits[0].item_id == f"f-{eid}-02"
    near = search.nearest(brain.db.conn, fake_embedding("Does the vendor support MIG?", DIM), k=3)
    assert near[0].item_id == f"f-{eid}-02" and near[0].score == pytest.approx(1.0, abs=1e-5)


def test_decisions_override_extraction(brain):
    eid, _ = seed(brain)
    d = Decisions(entry=eid)
    d.fragments[f"f-{eid}-01"] = FragmentDecision(
        status="overcome_by_events",
        obe={"date": "2026-11-04", "reason": "vendor left"},
        threads={"add": ["vendor-eval"], "remove": ["gpu-quota"]},
    )
    d.fragments[f"f-{eid}-02"] = FragmentDecision(state="closed", closed_note="answered in sync")
    d.fragments[f"f-{eid}-03"] = FragmentDecision(deleted=True)
    d.fragments["f-gone"] = FragmentDecision(status="confirmed")
    d.mentions["person"] = {"Mike": "mike-chen"}
    d.mentions["org"] = {"the vendor": None}
    brain.vault.put_decisions(d)

    f1 = f"f-{eid}-01"
    assert rows(brain, "SELECT status, status_origin, obe_date, obe_reason FROM fragments WHERE id = ?", f1) == [
        ("overcome_by_events", "user", "2026-11-04", "vendor left")
    ]
    assert rows(brain, "SELECT state, closed_note FROM fragments WHERE id = ?", f"f-{eid}-02") == [
        ("closed", "answered in sync")
    ]
    assert rows(brain, "SELECT COUNT(*) FROM fragments WHERE id = ?", f"f-{eid}-03") == [(0,)]
    threads = rows(brain, "SELECT dst, origin FROM edges WHERE src = ? AND type = 'in_thread'", f"frag:{f1}")
    assert threads == [("thread:vendor-eval", "user")]
    assert ("frag:" + f1, "person:mike-chen", "user", 1) in rows(
        brain, "SELECT src, dst, origin, confirmed FROM edges WHERE type = 'mentions'"
    )
    kinds = {r[0] for r in rows(brain, "SELECT kind FROM review_items")}
    assert kinds == {"orphaned_decision", "proposed_thread"}


def test_editing_an_entry_marks_extraction_stale_and_drops_stale_vectors(brain):
    eid, _ = seed(brain)
    entry, key = brain.vault.get_entry(eid)
    assert rows(brain, "SELECT COUNT(*) FROM embeddings WHERE owner_id = ?", eid) == [(1,)]
    entry.body += "\nAdded a line later."
    brain.vault.update_entry(entry, key)
    assert rows(brain, "SELECT extraction FROM entries WHERE id = ?", eid) == [("stale",)]
    assert rows(brain, "SELECT COUNT(*) FROM embeddings WHERE owner_id = ?", eid) == [(0,)]
    assert rows(brain, "SELECT COUNT(*) FROM embeddings WHERE owner_id LIKE 'f-%'") == [(3,)]


def test_thread_summary_is_indexed(brain):
    seed(brain)
    brain.vault.put_bytes(
        keys.thread_summary_key("gpu-quota"),
        ThreadSummary(
            thread_id="gpu-quota",
            model="fake",
            prompt=STAMP,
            updated_at=datetime.now(UTC),
            summary_md="Moving in December.",
        ).to_bytes(),
    )
    assert rows(brain, "SELECT summary FROM threads WHERE id = 'gpu-quota'") == [("Moving in December.",)]


@pytest.mark.parametrize("backend", ["local", "moto", "minio"])
def test_reindex_from_scratch_is_equivalent(backend, request, state, tmp_path):
    storage = request.getfixturevalue(f"{backend}_storage")
    b = make_brain(storage, state, tmp_path, llm={"embed_model": "embed-model"})
    try:
        eid, _ = seed(b)
        d = Decisions(entry=eid, mentions={"person": {"Mike": "mike-chen"}})
        d.fragments[f"f-{eid}-01"] = FragmentDecision(status="speculative")
        b.vault.put_decisions(d)
        b.vault.delete(keys.entity_key("org", "platform"))
        incremental = snapshot(b.db)
        assert incremental["entries"] and incremental["vectors"]
        b.reindex()
        assert snapshot(b.db) == incremental
    finally:
        b.close()


def test_sync_skips_listing_when_generation_unchanged_but_full_catches_outside_edits(brain, local_storage):
    eid, other = seed(brain)
    assert not brain.sync().skipped  # a new index lists the store once
    key = rows(brain, "SELECT key FROM entries WHERE id = ?", other)[0][0]
    text = local_storage.get(key).decode().replace("lunch", "dinner")
    local_storage.put(key, text.encode())  # edited outside brain: no generation bump
    assert brain.sync().skipped
    report = brain.sync(full=True)
    assert report.changed == 1 and report.units == 1
    assert "dinner" in rows(brain, "SELECT body FROM entries WHERE id = ?", other)[0][0]
    local_storage.delete(key)
    brain.sync(full=True)
    assert rows(brain, "SELECT COUNT(*) FROM entries WHERE id = ?", other) == [(0,)]


def test_second_machine_sees_writes_through_generation(local_storage, state, tmp_path):
    from brain.core.localstate import LocalState

    a = make_brain(local_storage, state, tmp_path, index_name="a")
    a.jot("first")
    a.close()  # releases the lease so the other machine may write
    b = make_brain(local_storage, LocalState(tmp_path / "state-b"), tmp_path, index_name="b")
    assert not b.sync().skipped
    assert b.sync().skipped
    b.jot("second")
    b.close()
    a = make_brain(local_storage, state, tmp_path, index_name="a")
    report = a.sync()
    assert not report.skipped and report.changed == 1
    assert rows(a, "SELECT COUNT(*) FROM entries") == [(2,)]
    a.close()


def test_unparseable_file_is_reported_then_cleared(brain, local_storage):
    seed(brain)
    local_storage.put("people/broken.md", b"---\nname: [unclosed\n---\n")
    report = brain.sync(full=True)
    assert [p[0] for p in report.problems] == ["people/broken.md"]
    local_storage.put("people/broken.md", b"---\nid: broken\nname: Fixed\n---\n")
    report = brain.sync(full=True)
    assert report.problems == []
    assert rows(brain, "SELECT label FROM nodes WHERE id = 'person:broken'") == [("Fixed",)]


def test_changing_an_entity_id_moves_its_rows(brain, local_storage):
    seed(brain)
    key = keys.entity_key("person", "jane-doe")
    local_storage.put(key, local_storage.get(key).replace(b"id: jane-doe", b"id: jane-d"))
    brain.sync(full=True)
    ids = {r[0] for r in rows(brain, "SELECT id FROM nodes WHERE type = 'person'")}
    assert ids == {"person:jane-d"}


def test_embedding_model_change_rebuilds_index(local_storage, state, tmp_path):
    b = make_brain(local_storage, state, tmp_path, llm={"embed_model": "embed-model"})
    seed(b)
    path = b.db.path
    b.close()
    from brain.core.index.db import IndexDB

    db = IndexDB(path, embed_model="other-model")
    assert "embedding model changed" in (db.rebuilt_reason or "")
    db.close()
