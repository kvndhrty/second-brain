"""The full pipeline against the scripted fake LLM: no network, no model."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from brain.core import keys, review
from brain.core.formats.entity import Person, Thread
from brain.core.formats.sidecars import Extraction, ThreadSummary
from brain.core.index.db import snapshot
from brain.core.llm.fake import heuristic_extract
from brain.core.llm.models import LLMError
from brain.core.pipeline.match import assign_ids
from brain.core.pipeline.text import locate_quote
from brain.core.services import FragmentChange
from tests.conftest import FAKE_LLM_SETTINGS, make_brain

FIRST = (
    "Met Jane Doe and Mike from PT about the [[GPU quota migration]]. "
    "We decided to move the eval cluster in December #capacity-planning. "
    "Does the vendor support MIG on the new nodes?"
)
SECOND = "Confirmed the vendor does support MIG on the new nodes. Talked with Mike about the [[GPU quota migration]]."


@pytest.fixture
def brain(local_storage, state, tmp_path):
    b = make_brain(local_storage, state, tmp_path, fake_llm=True, llm=FAKE_LLM_SETTINGS)
    yield b
    b.close()


def jot(b, text):
    entry = b.jot(text).entry
    report = b.process(entry.id)
    assert not report.failed, report.failed
    return entry.id


def rows(b, sql, *args):
    return [tuple(r) for r in b.db.conn.execute(sql, args).fetchall()]


def review_kinds(b):
    return sorted((i.kind, i.payload.get("surface") or i.payload.get("name") or "") for i in b.review_items())


def test_full_pipeline(brain):
    eid = jot(brain, FIRST)
    assert rows(brain, "SELECT next_step FROM entries WHERE id = ?", eid) == [("done",)]
    kinds = [r[0] for r in rows(brain, "SELECT kind FROM fragments WHERE entry_id = ? ORDER BY id", eid)]
    assert kinds == ["observation", "decision", "question"]
    assert rows(brain, "SELECT id, state FROM threads") == [("gpu-quota-migration", "proposed")]
    assert ("theme:capacity-planning",) in rows(brain, "SELECT id FROM nodes WHERE type = 'theme'")
    assert review_kinds(brain) == [
        ("new_org", "MIG"),
        ("new_org", "PT"),
        ("new_person", "Jane Doe"),
        ("new_person", "Mike"),
        ("proposed_theme", "capacity planning"),
        ("proposed_thread", "GPU quota migration"),
    ]
    summary = ThreadSummary.parse(brain.storage.get(keys.thread_summary_key("gpu-quota-migration")), where="x")
    assert summary.through_entry == eid and summary.citations == [eid]
    assert rows(brain, "SELECT summary_through, last_entry FROM threads") == [(eid, eid)]
    assert brain.llm.prompts_called() == ["extract", "thread_summary"]


def test_processing_again_does_nothing(brain):
    eid = jot(brain, FIRST)
    calls = len(brain.llm.calls)
    report = brain.process(eid)
    assert report.entries[0].steps == [] and report.threads == []
    assert len(brain.llm.calls) == calls
    assert brain.process_pending().entries == []


def test_failure_leaves_entry_pending_and_resumes(brain, monkeypatch):
    entry = brain.jot(FIRST).entry

    def broken(texts):
        raise LLMError("embedding endpoint is down")

    monkeypatch.setattr(brain.llm, "_embedder", broken)
    report = brain.process(entry.id)
    assert report.entries[0].steps == ["extract"]
    assert "embedding endpoint is down" in report.entries[0].error
    assert rows(brain, "SELECT next_step FROM entries") == [("embed",)]
    assert brain.failures()[entry.id]["step"] == "embed"
    monkeypatch.undo()
    extract_calls = brain.llm.prompts_called().count("extract")
    report = brain.process_pending()
    assert report.entries[0].steps == ["embed", "resolve"]
    assert brain.llm.prompts_called().count("extract") == extract_calls  # not extracted twice
    assert brain.failures() == {}


def test_edit_keeps_fragment_ids_and_decisions(brain):
    eid = jot(brain, FIRST)
    question = f"f-{eid}-03"
    brain.set_fragment(question, FragmentChange(status="overcome_by_events", obe_reason="vendor left"))
    entry, key = brain.vault.get_entry(eid)
    entry.body = entry.body.replace("Met Jane Doe", "Met with Jane Doe") + "\nWe will revisit quotas in January."
    brain.vault.update_entry(entry, key)
    assert rows(brain, "SELECT next_step FROM entries") == [("extract",)]
    report = brain.process(eid)
    assert report.entries[0].steps == ["extract", "embed", "resolve"]
    frags = dict(rows(brain, "SELECT id, text FROM fragments WHERE entry_id = ?", eid))
    assert set(frags) == {f"f-{eid}-01", f"f-{eid}-02", question, f"f-{eid}-04"}
    assert "Met with Jane Doe" in frags[f"f-{eid}-01"]
    assert rows(brain, "SELECT status, obe_reason FROM fragments WHERE id = ?", question) == [
        ("overcome_by_events", "vendor left")
    ]
    assert not [i for i in brain.review_items() if i.kind == "orphaned_decision"]


def test_reextract_reuses_ids_and_reports_orphans(brain):
    eid = jot(brain, FIRST)
    brain.set_fragment(f"f-{eid}-02", FragmentChange(dream=True))
    before = sorted(r[0] for r in rows(brain, "SELECT id FROM fragments"))
    brain.reextract([eid])
    assert sorted(r[0] for r in rows(brain, "SELECT id FROM fragments")) == before
    assert rows(brain, "SELECT dream FROM fragments WHERE id = ?", f"f-{eid}-02") == [(1,)]
    # A model that now finds nothing: the decision has no fragment to apply to.
    brain.llm.responders["extract"] = {"fragments": []}
    brain.reextract([eid])
    assert [i.payload["fragment"] for i in brain.review_items() if i.kind == "orphaned_decision"] == [f"f-{eid}-02"]


def test_known_alias_links_without_review(brain):
    brain.vault.put_entity(Person(id="jane-doe", name="Jane Doe", aliases=["Jane"]))
    eid = jot(brain, "Talked with Jane about the rollout.")
    assert rows(brain, "SELECT dst, origin, confirmed FROM edges WHERE type = 'mentions'") == [
        ("person:jane-doe", "llm", 0)
    ]
    assert not [i for i in brain.review_items() if i.entry_id == eid]
    assert "resolve" not in brain.llm.prompts_called()  # an exact alias needs no model call


def test_shared_first_name_goes_to_review(brain):
    brain.vault.put_entity(Person(id="mike-chen", name="Mike Chen"))
    brain.vault.put_entity(Person(id="mike-ross", name="Mike Ross"))
    jot(brain, "Paired with Mike on the flaky test.")
    assert "resolve" in brain.llm.prompts_called()
    (item,) = [i for i in brain.review_items() if i.kind == "ambiguous_mention"]
    assert {c["entity_id"] for c in item.payload["candidates"]} == {"mike-chen", "mike-ross"}
    assert not rows(brain, "SELECT * FROM edges WHERE type = 'mentions'")


def test_model_must_pick_top_candidate_confidently(local_storage, state, tmp_path):
    def picks_second(user):
        return {"choices": [{"key": "m1", "choice": "mike-ross", "confidence": 0.99, "reason": "?"}]}

    b = make_brain(
        local_storage, state, tmp_path, fake_llm=True, responders={"resolve": picks_second}, llm=FAKE_LLM_SETTINGS
    )
    try:
        b.vault.put_entity(Person(id="mike-chen", name="Mike Chen", aliases=["Mikey"]))
        b.vault.put_entity(Person(id="mike-ross", name="Mike Ross"))
        jot(b, "Paired with Mikey C on the flaky test.")
        res = json.loads(b.storage.get(keys.resolution_key(b.db.conn.execute("SELECT id FROM entries").fetchone()[0])))
        decisions = {(m["surface"], m["decision"]) for m in res["mentions"]}
        assert ("Mikey", "pending") in decisions or ("Mikey C", "pending") in decisions
    finally:
        b.close()


def test_create_person_applies_to_all_entries_and_teaches_alias(brain):
    e1 = jot(brain, "Lunch with Zed about the cache.")
    e2 = jot(brain, "Reviewed the cache design with Zed.")
    item = next(i for i in brain.review_items() if i.payload.get("surface") == "Zed")
    assert item.others == 1
    brain.resolve_review(item.id, review.CreateEntity("Zed Shaw", role="Staff engineer"))
    person = brain.vault.get_entity("person", "zed-shaw")
    assert person.aliases == ["Zed"] and person.role == "Staff engineer"
    assert not [i for i in brain.review_items() if i.payload.get("surface") == "Zed"]
    assert {r[0] for r in rows(brain, "SELECT entry_id FROM edges WHERE dst = 'person:zed-shaw'")} == {e1, e2}
    jot(brain, "Zed fixed it.  Then Zed shipped.")
    assert len(rows(brain, "SELECT * FROM edges WHERE dst = 'person:zed-shaw'")) == 3  # new entry auto-links


def test_ignore_mention(brain):
    jot(brain, "Spoke with Nobody about it.")
    item = next(i for i in brain.review_items() if i.payload.get("surface") == "Nobody")
    brain.resolve_review(item.id, review.IgnoreMention())
    assert not [i for i in brain.review_items() if i.payload.get("surface") == "Nobody"]
    d = brain.vault.get_decisions(item.entry_id)
    assert d.mentions["person"] == {"Nobody": None}


def test_proposed_thread_merge_and_brief_follows_merge(brain):
    jot(brain, "Started the [[Quota work]] today.")
    jot(brain, "More progress on [[GPU quotas]].")
    items = {i.payload["name"]: i for i in brain.review_items() if i.kind == "proposed_thread"}
    brain.resolve_review(items["GPU quotas"].id, review.AcceptProposed(rename="GPU quota migration"))
    brain.resolve_review(items["Quota work"].id, review.MergeProposed("gpu-quotas"))
    target = brain.vault.get_entity("thread", "gpu-quotas")
    assert target.name == "GPU quota migration" and "Quota work" in target.aliases and target.state == "active"
    assert brain.vault.get_entity("thread", "quota-work").state == "merged"
    b = brain.brief("GPU quota migration")
    assert b.target == "thread:gpu-quotas" and b.fragment_count == 2
    assert [t.id for t in brain.active_threads()] == ["gpu-quotas"]


def test_answer_proposal_accept_closes_question(brain):
    jot(brain, FIRST)
    e2 = jot(brain, SECOND)
    (item,) = [i for i in brain.review_items() if i.kind == "proposed_answer"]
    assert item.entry_id == e2
    brain.resolve_review(item.id, review.AnswerDecision(accept=True))
    q = item.payload["answers"]
    assert rows(brain, "SELECT state FROM fragments WHERE id = ?", q) == [("closed",)]
    assert rows(brain, "SELECT type, origin FROM edges WHERE dst = ? AND type != 'contains'", f"frag:{q}") == [
        ("answers", "user")
    ]
    assert not [i for i in brain.review_items() if i.kind == "proposed_answer"]


def test_reject_answer_is_not_asked_again(brain):
    jot(brain, FIRST)
    jot(brain, SECOND)
    (item,) = [i for i in brain.review_items() if i.kind == "proposed_answer"]
    brain.resolve_review(item.id, review.AnswerDecision(accept=False))
    assert not [i for i in brain.review_items() if i.kind == "proposed_answer"]
    assert rows(brain, "SELECT state FROM fragments WHERE id = ?", item.payload["answers"]) == [("open",)]


def test_status_changes_survive_reindex(brain):
    eid = jot(brain, FIRST)
    brain.set_fragment(f"f-{eid}-03", FragmentChange(state="closed", closed_note="asked the vendor"))
    brain.set_fragment(f"f-{eid}-02", FragmentChange(status="speculative"))
    before = snapshot(brain.db)
    brain.reindex()
    assert snapshot(brain.db) == before
    assert rows(brain, "SELECT state, closed_note FROM fragments WHERE id = ?", f"f-{eid}-03") == [
        ("closed", "asked the vendor")
    ]


def test_reindex_after_pipeline_needs_no_model(brain):
    jot(brain, FIRST)
    jot(brain, SECOND)
    before = snapshot(brain.db)
    calls = len(brain.llm.calls), len(brain.llm.embed_calls)
    brain.reindex()
    assert snapshot(brain.db) == before
    assert (len(brain.llm.calls), len(brain.llm.embed_calls)) == calls


def test_extract_retries_on_unknown_thread_id(local_storage, state, tmp_path):
    attempts = []

    def flaky(user):
        attempts.append(user)
        out = heuristic_extract(user)
        if len(attempts) == 1:
            out["fragments"][0]["threads"] = [{"thread_id": "no-such-thread", "weight": 0.5}]
        return out

    b = make_brain(local_storage, state, tmp_path, fake_llm=True, responders={"extract": flaky}, llm=FAKE_LLM_SETTINGS)
    try:
        jot(b, "Something happened.")
        assert len(attempts) == 2
    finally:
        b.close()


def test_ask_cites_sources_and_rejects_invented_citations(local_storage, state, tmp_path):
    b = make_brain(local_storage, state, tmp_path, fake_llm=True, llm=FAKE_LLM_SETTINGS)
    try:
        e1 = jot(b, FIRST)
        answer = b.ask("does the vendor support MIG?")
        assert answer.claims and all(c.citations for c in answer.claims)
        assert e1 in answer.sources and answer.sources[e1].date
        b.llm.responders["ask"] = {"claims": [{"text": "made up", "citations": ["e-bogus"]}]}
        with pytest.raises(LLMError):
            b.ask("does the vendor support MIG?")
    finally:
        b.close()


def test_brief_records_time_and_marks_new_fragments(brain):
    jot(brain, FIRST)
    first = brain.brief("GPU quota migration")
    assert first.since is None and first.recorded
    later = datetime.now().astimezone() + timedelta(minutes=5)
    brain.vault.clock = lambda: later  # the second entry is written after the first brief
    jot(brain, SECOND)
    second = brain.brief("gpu-quota-migration")
    assert second.since is not None
    sent = brain.llm.calls[-1][1]
    assert 'new="yes"' in sent and 'new="no"' in sent
    assert second.output.changed_since


def test_brief_on_person_and_ambiguous_target(brain):
    brain.vault.put_entity(Person(id="jane-doe", name="Jane Doe", aliases=["Jane"]))
    brain.vault.put_entity(Thread(id="jane", name="Jane"))
    jot(brain, "Talked with Jane Doe about evals.")
    from brain.core.answers import AmbiguousTarget

    with pytest.raises(AmbiguousTarget):
        brain.brief("Jane")
    b = brain.brief("person:jane-doe")
    assert b.type == "person" and b.fragment_count == 1


def test_active_threads_decay_unless_pinned(brain):
    jot(brain, "Started [[Old work]].")
    (t,) = brain.active_threads()
    later = datetime.fromisoformat(t.last_activity) + timedelta(days=30)
    assert brain.active_threads(now=later) == []
    thread = brain.vault.get_entity("thread", "old-work")
    thread.pinned = True
    brain.vault.put_entity(thread)
    assert [x.id for x in brain.active_threads(now=later)] == ["old-work"]


def test_free_text_brief(brain):
    jot(brain, FIRST)
    b = brain.brief("vendor nodes support")
    assert b.type == "text" and b.fragment_count >= 1


def test_locate_quote_and_assign_ids():
    body = "We decided to move the eval cluster in December."
    assert locate_quote(body, "move the eval cluster") == (14, 35)
    assert locate_quote(body, "Move The Eval cluster") == (14, 35)
    assert locate_quote(body, "we decidd to move the eval cluster") is not None
    assert locate_quote(body, "nothing like it at all") is None
    prev = Extraction(
        entry_id="e-1",
        entry_hash="h",
        model="m",
        prompt={"name": "x", "version": 1, "sha256": "s"},
        created_at="2026-10-30T00:00:00Z",
        fragments=[
            {"id": "f-e-1-01", "kind": "decision", "text": "Move the cluster in December."},
            {"id": "f-e-1-07", "kind": "question", "text": "Does MIG work?"},
        ],
    )
    ids = assign_ids(
        "e-1",
        [
            ("question", "Does MIG work on new nodes?", None),
            ("decision", "Move the cluster in December.", None),
            ("idea", "A portal.", None),
        ],
        prev,
    )
    assert ids == ["f-e-1-07", "f-e-1-01", "f-e-1-08"]
