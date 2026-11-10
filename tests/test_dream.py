"""Dreaming (layer 9): the offline pass across all notes, against the scripted fake model."""

from __future__ import annotations

from datetime import timedelta

from brain.core import dream
from tests.conftest import FAKE_LLM_SETTINGS, T0, entity, make_brain, set_now, write

KICKOFF = (
    "Met Jane Doe about the [[GPU quota migration]]. We decided to migrate in December. Does MIG work on new nodes?"
)


def test_nothing_to_do_on_an_empty_store(brain):
    assert brain.dream_plan().empty
    run = brain.dream()
    assert run.calls == 0 and run.events == [] and run.errors == []


def test_duplicate_people_are_merged(brain):
    # "Priya" came first, so "Priya Raman" was a candidate the fake's link rule did not take.
    write(brain, "Talked with Priya about storage.")
    write(brain, "Called Priya about storage again.", 1)
    brain.vault.create_entity(
        "Priya Raman",
        lambda eid: entity(brain, "Priya").record.model_copy(
            update={"id": eid, "name": "Priya Raman", "names": [], "status": "provisional"}
        ),
    )
    plan = brain.dream_plan()
    assert plan.duplicates == [("priya", "priya-raman")]
    run = brain.dream()
    assert [e.kind for e in run.events] == ["merged"]
    (person,) = brain.world().roots("person")
    assert person.id == "priya" and "Priya Raman" in brain.page("priya").names  # the one with notes is kept
    assert brain.vault.get_entity("priya-raman").set_by["merged_into"] == "dream"
    assert brain.dream_plan().duplicates == []  # the pair is remembered
    assert any(e.source.startswith("dream:") for e in brain.log())


def test_a_not_the_same_note_stops_dreaming_from_merging(brain):
    write(brain, "Talked with Priya about storage.")
    write(brain, "Called Priya about storage again.", 1)
    brain.vault.create_entity(
        "Priya Rao",
        lambda eid: entity(brain, "Priya").record.model_copy(
            update={"id": eid, "name": "Priya Rao", "names": [], "status": "active", "not_same_as": ["priya"]}
        ),
    )
    assert brain.dream_plan().duplicates == []
    brain.dream()
    assert len(brain.world().roots("person")) == 2


def test_old_open_item_is_settled_by_a_later_statement(local_storage, state, tmp_path):
    # Layer 6 is told every pair is unrelated, so the question is still open when dreaming looks.
    b = make_brain(
        local_storage,
        state,
        tmp_path,
        fake_llm=True,
        responders={"relate": {"relation": "unrelated"}},
        llm=FAKE_LLM_SETTINGS,
    )
    set_now(b)
    write(b, KICKOFF)
    write(b, "Confirmed: MIG does work on new nodes for the [[GPU quota migration]].", 2)
    assert len(b.world().open_items()) == 1
    set_now(b, 30)
    plan = b.dream_plan()
    assert len(plan.stale) == 1
    run = b.dream()
    assert [(r.rel, r.dst) for r in run.relations] == [("answers", plan.stale[0])]
    assert b.world().open_items() == []
    q = next(s for s in b.page("GPU quota migration").history if s.kind == "question")
    assert q.status == "answered"
    assert b.dream_plan().stale == []
    b.close()


def test_quiet_thread_is_closed_and_new_activity_reopens_it(brain):
    write(brain, "We decided to start the [[Vendor evaluation]].")
    write(brain, "The [[Vendor evaluation]] covers three vendors.", 1)
    set_now(brain, 100)
    assert brain.dream_plan().close == ["vendor-evaluation"]
    brain.dream()
    assert brain.page("Vendor evaluation").status == "closed"
    assert brain.vault.get_entity("vendor-evaluation").set_by["status"] == "dream"
    write(brain, "The [[Vendor evaluation]] picked a winner.", 101)
    assert brain.page("Vendor evaluation").status == "active"
    assert any(e.kind == "reopened" for e in brain.log())


def test_thread_with_open_items_is_not_closed(brain):
    write(brain, KICKOFF)
    write(brain, "More on the [[GPU quota migration]] today.", 1)
    set_now(brain, 100)
    assert brain.dream_plan().close == []


def test_profile_is_filled_in_from_statements(brain):
    write(brain, "Jane Doe is a program manager on the Platform Team.")
    write(brain, "Jane Doe approved the Platform Team budget.", 1)
    brain.dream()
    page = brain.page("Jane Doe")
    assert page.profile == {"role": "program manager", "org": "Platform Team"}
    assert "Role: program manager" in brain.storage.get("wiki/people/jane-doe.md").decode()
    assert brain.dream_plan().profiles == []


def test_pass_stops_at_the_call_budget(local_storage, state, tmp_path):
    b = make_brain(local_storage, state, tmp_path, fake_llm=True, llm=FAKE_LLM_SETTINGS, dream={"max_calls": 1})
    set_now(b)
    for i, name in enumerate(["Jane Doe", "Ravi Patel", "Mei Chen"]):
        write(b, f"{name} is a storage engineer.", i)
        write(b, f"{name} approved the plan.", i + 0.5)
    run = b.dream()
    assert run.budget_exhausted and run.calls == 1
    assert len(b.dream_plan().profiles) == 2  # the rest are looked at next time
    b.close()


def test_dreaming_uses_the_strong_model_when_configured(local_storage, state, tmp_path):
    cfg = FAKE_LLM_SETTINGS | {"strong_model": "fake-strong"}
    b = make_brain(local_storage, state, tmp_path, fake_llm=True, llm=cfg)
    set_now(b)
    write(b, "Jane Doe is a program manager.")
    write(b, "Jane Doe approved the budget.", 1)
    used = {m for (_, m) in b.llm._models}
    assert used == {"fake-small"}  # the per-note layers never use the strong model
    run = b.dream()
    assert run.model == "fake-strong"
    b.close()


def test_is_due_after_enough_notes_or_at_night(brain):
    ctx = brain._ctx()
    cfg = brain.settings.dream
    assert dream.is_due(ctx, cfg, now=T0) is None  # no notes
    write(brain, "The office is closed on Friday.")
    assert "nightly" in dream.is_due(ctx, cfg, now=T0 + timedelta(days=1))
    brain.dream()
    assert dream.is_due(ctx, cfg, now=T0 + timedelta(days=1)) is None
    for i in range(cfg.after_notes):
        write(brain, f"Remark number {i}.", 0.1, process=False)
    brain.sync()
    assert "new notes" in dream.is_due(ctx, cfg, now=T0 + timedelta(hours=1))
