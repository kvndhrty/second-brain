"""Dreaming and the injected context, against the scripted fake LLM."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from brain.core import keys
from brain.core.config import DreamConfig
from brain.core.dream import is_due
from brain.core.formats.sidecars import DreamProposals, DreamRun, DreamState, ThreadNow
from brain.core.index.db import snapshot
from brain.core.review import AnswerDecision, SupersedeDecision
from brain.core.services import FragmentChange
from tests.conftest import FAKE_LLM_SETTINGS, make_brain

NOW = datetime(2026, 11, 8, 18, 0, tzinfo=UTC)


@pytest.fixture
def brain(local_storage, state, tmp_path):
    b = make_brain(local_storage, state, tmp_path, fake_llm=True, llm=FAKE_LLM_SETTINGS)
    b._clock = NOW
    yield b
    b.close()


def jot(b, text, days_ago):
    when = NOW - timedelta(days=days_ago)
    b.vault.clock = lambda: when
    entry = b.jot(text).entry
    report = b.process(entry.id)
    assert not report.failed, report.failed
    return entry.id


def frag(b, entry_id, contains):
    (fid,) = [
        r[0]
        for r in b.db.conn.execute("SELECT id, text FROM fragments WHERE entry_id = ?", (entry_id,))
        if contains in r[1]
    ]
    return fid


def dream(b, **kw):
    from brain.core import dream as dream_mod
    from brain.core.pipeline.context import PipelineContext

    b.sync()
    ctx = PipelineContext(b.settings, b.vault, b.db, b.llm, clock=lambda: NOW)
    d = dream_mod.Dreamer(ctx, kw.pop("cfg", b.settings.dream))
    return d.run(d.plan(**kw))


def kinds(b):
    return sorted(i.kind for i in b.review_items())


def test_supersession_is_proposed_and_accepting_marks_obe(brain):
    old = jot(brain, "We decided the [[Billing migration]] cutover is in March.", days_ago=10)
    new = jot(brain, "The [[Billing migration]] cutover moved to May instead of March.", days_ago=2)
    run = dream(brain)
    assert run.calls >= 2 and not run.errors and not run.budget_exhausted
    old_f, new_f = frag(brain, old, "March"), frag(brain, new, "May")
    assert run.proposed.count(f"{old_f} superseded by {new_f}") == 1
    (item,) = [i for i in brain.review_items() if i.kind == "dream_obe"]
    assert item.payload["superseded_by"] == new_f and item.entry_id == old
    assert item.context[0].startswith("2026-10-29: ")  # each side shows its date

    brain.resolve_review(item.id, SupersedeDecision(accept=True))
    status, reason, origin = brain.db.conn.execute(
        "SELECT status, obe_reason, status_origin FROM fragments WHERE id = ?", (old_f,)
    ).fetchone()
    assert (status, origin) == ("overcome_by_events", "user")
    assert new_f in reason
    assert "dream_obe" not in kinds(brain)

    # Nothing new since: the next pass has no supersession work for this thread.
    assert not [j for j in brain.dream_plan().jobs if j.kind == "supersede"]


def test_rejected_supersession_is_not_proposed_again(brain):
    old = jot(brain, "We decided the [[Billing migration]] cutover is in March.", days_ago=10)
    jot(brain, "The [[Billing migration]] cutover moved to May instead of March.", days_ago=2)
    dream(brain)
    (item,) = [i for i in brain.review_items() if i.kind == "dream_obe"]
    brain.resolve_review(item.id, SupersedeDecision(accept=False))
    assert tuple(
        brain.db.conn.execute(
            "SELECT status, status_origin FROM fragments WHERE id = ?", (frag(brain, old, "March"),)
        ).fetchone()
    ) == ("confirmed", "user")
    jot(brain, "Still waiting on the [[Billing migration]] vendor.", days_ago=1)
    dream(brain)
    assert "dream_obe" not in kinds(brain)


def test_now_line_feeds_the_injected_context(brain):
    jot(brain, "Met Jane Doe about the [[Billing migration]]. Who owns the cutover?", days_ago=3)
    jot(brain, "Jane will run the [[Billing migration]] cutover in May.", days_ago=1)
    before = brain.context_text(now=NOW)
    assert "Billing migration [billing-migration]" in before
    assert "Now:" in before and "[as of 2026-11-07]" in before  # the summary stands in until dreaming runs

    dream(brain)
    now = ThreadNow.parse(brain.storage.get(keys.thread_now_key("billing-migration")), where="now")
    assert "Jane will run" in now.line
    text = brain.context_text(now=NOW)
    assert text.startswith("# brain: 2026-11-08 (Sunday). Entries through 2026-11-07.")
    assert f"  Now: {now.line} [as of 2026-11-07]" in text
    assert "## Open items, newest first" in text and "2026-11-05 (3d) question" in text
    doc = brain.context(now=NOW)
    assert doc.threads[0].now_source == "dream"


def test_context_stays_within_its_limits(brain):
    for i in range(30):
        jot(brain, f"Open question number {i} about the [[Billing migration]] plan?", days_ago=30 - i)
    brain.settings.context.max_lines = 15
    brain.settings.context.open_items = 20
    text = brain.context_text(now=NOW)
    assert len(text.splitlines()) <= 15
    assert "more lines not shown" in text or "left out to keep this short" in text
    assert text.rstrip().endswith("get_entry <entry-id>.")
    brain.settings.context.max_bytes = 600
    assert len(brain.context_text(now=NOW).encode()) <= 600


def test_answers_found_later_go_to_review(brain):
    q = jot(brain, "Who owns the [[Billing migration]] cutover runbook?", days_ago=20)
    a = jot(brain, "Priya owns the [[Billing migration]] cutover runbook.", days_ago=2)
    qf, af = frag(brain, q, "Who owns"), frag(brain, a, "Priya")
    run = dream(brain)
    assert f"{af} answers {qf}" in run.proposed
    proposals = DreamProposals.parse(brain.storage.get(keys.dream_proposals_key(a)), where="p")
    assert [(p.fragment, p.other) for p in proposals.proposals if p.kind == "answer"] == [(af, qf)]
    # Write time may have proposed the same link; review shows it once.
    (item,) = [i for i in brain.review_items() if i.kind == "proposed_answer"]
    brain.resolve_review(item.id, AnswerDecision(accept=False))
    assert "proposed_answer" not in kinds(brain)
    jot(brain, "Booked a room for the [[Billing migration]] retro.", days_ago=1)
    run = dream(brain)
    assert f"{af} answers {qf}" not in run.proposed  # rejected: not proposed again
    assert "proposed_answer" not in kinds(brain)


def test_an_answered_question_is_not_also_proposed_as_overcome(brain):
    q = jot(brain, "Who owns the [[Billing migration]] cutover runbook now?", days_ago=20)
    a = jot(brain, "Priya now owns the [[Billing migration]] cutover runbook.", days_ago=2)
    qf, af = frag(brain, q, "Who owns"), frag(brain, a, "Priya")
    run = dream(brain)
    assert f"{qf} superseded by {af}" not in run.proposed
    assert "dream_obe" not in kinds(brain)
    assert "proposed_answer" in kinds(brain)


def test_now_line_is_clipped_to_whole_clauses():
    from brain.core.dream import _clip_words

    assert _clip_words("Short  and\nfine.", 35) == "Short and fine."
    words = " ".join(f"w{i}" for i in range(20))
    clipped = _clip_words(f"{words}. Then {words}.", 35)
    assert clipped == f"{words}."
    clipped = _clip_words(f"{words}, then {words}", 35)
    assert clipped == f"{words}."
    assert _clip_words(" ".join(["word"] * 50), 35) == " ".join(["word"] * 35) + "…"


def test_accepted_dream_answer_closes_the_question(brain):
    q = jot(brain, "Who owns the [[Billing migration]] cutover runbook?", days_ago=20)
    a = jot(brain, "Priya owns the [[Billing migration]] cutover runbook.", days_ago=2)
    dream(brain)
    (item,) = [i for i in brain.review_items() if i.kind == "proposed_answer"]
    brain.resolve_review(item.id, AnswerDecision(accept=True))
    qf = frag(brain, q, "Who owns")
    assert tuple(brain.db.conn.execute("SELECT state FROM fragments WHERE id = ?", (qf,)).fetchone()) == ("closed",)
    assert a


def test_sweep_gets_verdicts(brain):
    q = jot(brain, "Does the vendor support MIG on the [[GPU quota]] nodes?", days_ago=45)
    jot(brain, "Confirmed the vendor does support MIG on the [[GPU quota]] nodes.", days_ago=40)
    s = jot(brain, "Is the [[GPU quota]] review board still meeting?", days_ago=44)
    run = dream(brain)
    assert "sweep" in run.examined
    items = {i.fragment.id: i for i in brain.sweep(now=NOW) if i.fragment}
    qf, sf = frag(brain, q, "MIG"), frag(brain, s, "review board")
    assert items[qf].suggestion["verdict"] == "probably_resolved"
    assert items[qf].suggestion["evidence"] is not None
    assert items[sf].suggestion is None or items[sf].suggestion["verdict"] != "probably_resolved"
    # A decision in the sweep removes the verdict.
    brain.set_fragment(qf, FragmentChange(state="closed"))
    assert brain.sweep(now=NOW) == [i for i in brain.sweep(now=NOW) if not i.fragment or i.fragment.id != qf]


def test_budget_stops_a_pass_and_the_next_continues(brain):
    for name in ("Alpha", "Beta", "Gamma"):
        jot(brain, f"We decided the [[{name} project]] launch is in March.", days_ago=9)
        jot(brain, f"The [[{name} project]] launch moved to May instead of March.", days_ago=3)
    planned = brain.dream_plan().calls
    cfg = DreamConfig(max_calls=2)
    first = dream(brain, cfg=cfg)
    assert first.calls == 2 and first.budget_exhausted
    second = dream(brain, cfg=DreamConfig(max_calls=100))
    assert first.calls + second.calls == planned
    assert not second.budget_exhausted
    assert dream(brain).calls == 0  # nothing left to do


def test_dry_run_calls_no_model(brain):
    jot(brain, "We decided the [[Billing migration]] cutover is in March.", days_ago=4)
    calls = len(brain.llm.calls)
    plan = brain.dream_plan()
    assert plan.calls > 0 and {j.kind for j in plan.jobs} >= {"now"}
    assert len(brain.llm.calls) == calls
    assert not brain.storage.exists(keys.DREAM_STATE)


def test_run_log_state_and_reindex(brain):
    jot(brain, "We decided the [[Billing migration]] cutover is in March.", days_ago=10)
    jot(brain, "The [[Billing migration]] cutover moved to May instead of March.", days_ago=2)
    run = dream(brain)
    state = DreamState.parse(brain.storage.get(keys.DREAM_STATE), where="s")
    assert state.last_run == run.run_id and state.entries_at_last_run == 2
    assert DreamRun.parse(brain.storage.get(keys.dream_run_key(run.run_id)), where="r").calls == run.calls
    assert brain.last_dream().run_id == run.run_id
    incremental = snapshot(brain.db)
    brain.reindex()
    assert snapshot(brain.db) == incremental  # proposals and Now lines rebuild from files alone


def test_summary_rebuild_after_enough_entries(brain):
    brain.settings.dream.rebuild_after_updates = 3
    for i in range(3):
        jot(brain, f"Step {i} of the [[Billing migration]] is done.", days_ago=5 - i)
    assert any(j.kind == "rebuild" for j in brain.dream_plan().jobs)
    run = dream(brain)
    assert "rebuilt the summary of thread billing-migration" in run.applied
    assert not any(j.kind == "rebuild" for j in brain.dream_plan().jobs)


def test_is_due(brain):
    from brain.core.pipeline.context import PipelineContext

    ctx = PipelineContext(brain.settings, brain.vault, brain.db, brain.llm)
    cfg = DreamConfig(after_entries=2, nightly_at="")
    assert is_due(ctx, cfg, now=NOW) is None  # no entries yet
    jot(brain, "First [[Billing migration]] note.", days_ago=1)
    assert is_due(ctx, cfg, now=NOW) is None
    jot(brain, "Second [[Billing migration]] note.", days_ago=1)
    assert "2 new entries" in is_due(ctx, cfg, now=NOW)
    dream(brain)
    assert is_due(ctx, cfg, now=NOW) is None
    nightly = DreamConfig(after_entries=0, nightly_at="03:00")
    assert is_due(ctx, nightly, now=NOW + timedelta(days=1)) is None  # no new entries since the last pass
    jot(brain, "Third [[Billing migration]] note.", days_ago=0)
    assert "nightly" in is_due(ctx, nightly, now=NOW + timedelta(days=1))
    assert is_due(ctx, DreamConfig(enabled=False), now=NOW + timedelta(days=1)) is None


# --- regressions found in review ----------------------------------------------------


def _tid(b, name):
    return b.db.conn.execute("SELECT id FROM threads WHERE name = ?", (name,)).fetchone()[0]


def test_proposal_survives_a_pass_over_another_thread(brain):
    jot(brain, "[[Alpha]] kickoff notes about vendors and budgets.", days_ago=12)
    e1 = jot(brain, "We decided the [[Alpha]] [[Beta]] cutover is in March.", days_ago=10)
    jot(brain, "The [[Beta]] cutover moved to May instead of March.", days_ago=5)
    dream(brain)
    f1 = frag(brain, e1, "March")
    assert [i.payload["fragment"] for i in brain.review_items() if i.kind == "dream_obe"] == [f1]
    jot(brain, "[[Alpha]] vendor shortlist is ready for review.", days_ago=1)
    dream(brain)  # looks at Alpha only; it cannot remake Beta's proposal, so must not drop it
    assert [i.payload["fragment"] for i in brain.review_items() if i.kind == "dream_obe"] == [f1]


def test_merged_threads_are_not_redone_every_pass(brain):
    jot(brain, "[[Alpha]] note about the first vendor.", days_ago=10)
    jot(brain, "[[Alpha]] note about the second vendor.", days_ago=9)
    jot(brain, "[[Gamma]] note about the third vendor.", days_ago=2)
    brain.threads.merge(_tid(brain, "Gamma"), _tid(brain, "Alpha"))
    dream(brain)
    assert not [j for j in brain.dream_plan().jobs if j.thread_id == _tid(brain, "Alpha")]


def test_an_unfinished_pass_makes_the_next_one_due(brain):
    from brain.core.pipeline.context import PipelineContext

    for i in range(4):
        jot(brain, f"[[T{i}]] some work item number {i} about things.", days_ago=5 - i)
    cfg = DreamConfig(max_calls=1, after_entries=2, nightly_at="")
    assert dream(brain, cfg=cfg).budget_exhausted
    ctx = PipelineContext(brain.settings, brain.vault, brain.db, brain.llm, clock=lambda: NOW)
    assert "max_calls" in is_due(ctx, cfg, now=NOW)
    while dream(brain, cfg=cfg).budget_exhausted:
        pass
    assert is_due(ctx, cfg, now=NOW) is None


def test_a_single_thread_pass_leaves_the_schedule_alone(brain):
    from brain.core.pipeline.context import PipelineContext

    jot(brain, "First [[Billing migration]] note.", days_ago=2)
    jot(brain, "Second [[Billing migration]] note.", days_ago=1)
    cfg = DreamConfig(after_entries=2, nightly_at="")
    ctx = PipelineContext(brain.settings, brain.vault, brain.db, brain.llm, clock=lambda: NOW)
    dream(brain, thread="billing-migration")
    assert is_due(ctx, cfg, now=NOW) is not None  # the scheduled pass is still owed
    state = DreamState.parse(brain.storage.get(keys.DREAM_STATE), where="s")
    assert state.last_run_at is None and state.last_run


def test_context_leaves_out_archived_someday_and_keeps_one_line_items(brain):
    eid = jot(brain, "We decided the [[Old work]] plan is final.", days_ago=2)
    jot(brain, "Who owns the [[Billing migration]] runbook?", days_ago=1)
    brain.threads.set_state(_tid(brain, "Old work"), "archived")
    q = frag(brain, jot(brain, "Someday question about [[Billing migration]] robots?", days_ago=1), "robots")
    brain.set_fragment(q, FragmentChange(someday=True))
    text = brain.context_text(now=NOW)
    assert "Old work" not in text and "plan is final" not in text and "robots" not in text
    assert "(1)" in text  # one open item: the someday question is not counted
    assert eid
    doc = brain.context(now=NOW)
    doc.open_items[0].text = "line one\nline two\nline three"
    from brain.core.injected import render_markdown

    out = render_markdown(doc, max_lines=60, max_bytes=6000)
    assert "line one line two line three" in out


def test_dream_cli_unknown_thread_is_an_error_not_a_traceback(brain):
    with pytest.raises(KeyError):
        brain.dream_plan(thread="no-such-thread")
