"""`ask`, `brief`, and the injected context."""

from __future__ import annotations

import pytest

from brain.core.answers import AmbiguousTarget
from tests.conftest import set_now, write

KICKOFF = "We decided to migrate the [[GPU quota migration]] in December. Does MIG work on new nodes? Met Jane Doe."


def seed(brain) -> list[str]:
    return [
        write(brain, KICKOFF),
        write(brain, "Jane Doe needs to file the quota ticket for the [[GPU quota migration]].", 2),
        write(brain, "We decided to migrate the [[GPU quota migration]] in January instead.", 5),
    ]


def test_ask_cites_notes_from_its_input(brain):
    ids = seed(brain)
    answer = brain.ask("When do we migrate?")
    assert answer.claims and not answer.insufficient
    cited = {c for claim in answer.claims for c in claim.citations}
    assert cited <= set(ids) and set(answer.sources) == cited
    assert all(s.date.startswith("2026-10-") for s in answer.sources.values())


def test_ask_with_nothing_relevant_says_so_without_a_model_call(brain):
    before = len(brain.llm.calls)
    answer = brain.ask("What is the capital of France?")
    assert answer.insufficient and answer.claims == []
    assert len(brain.llm.calls) == before


def test_ask_rejects_citations_that_were_not_in_the_input(local_storage, state, tmp_path):
    from brain.core.llm.models import LLMError
    from tests.conftest import FAKE_LLM_SETTINGS, make_brain

    bad = {"ask": {"claims": [{"text": "Made up.", "citations": ["n-19990101T000000Z"]}], "insufficient": False}}
    b = make_brain(local_storage, state, tmp_path, fake_llm=True, responders=bad, llm=FAKE_LLM_SETTINGS)
    set_now(b)
    write(b, "The office party is on Friday.")
    with pytest.raises(LLMError, match="did not return valid output"):
        b.ask("When is the office party?")
    b.close()


def test_brief_by_name_needs_no_routing_call(brain):
    seed(brain)
    before = brain.llm.prompts_called().count("brief_route")
    brief = brain.brief("GPU quota migration")
    assert [t.id for t in brief.topics] == ["gpu-quota-migration"] and not brief.routed
    assert brain.llm.prompts_called().count("brief_route") == before
    out = brief.output
    assert any("January" in c.text for c in out.recent_decisions)
    assert any("quota ticket" in c.text for c in out.open_items)
    assert brief.recorded and brief.since is None


def test_brief_remembers_when_you_last_looked(brain):
    seed(brain)
    set_now(brain, 6)
    brain.brief("GPU quota migration")
    write(brain, "The quota ticket for the [[GPU quota migration]] was approved.", 7)
    set_now(brain, 8)
    brief = brain.brief("GPU quota migration")
    assert brief.since is not None and brief.since.date().isoformat() == "2026-10-07"
    assert [c.text for c in brief.output.changed_since] == [
        "The quota ticket for the GPU quota migration was approved."
    ]


def test_brief_in_plain_words_is_routed_by_the_model(brain):
    seed(brain)
    write(brain, "Started the [[Vendor evaluation]] with Jane Doe.", 6)
    write(brain, "The [[Vendor evaluation]] covers three vendors.", 7)
    brief = brain.brief("where are we on the gpu quota migration and the vendor evaluation")
    assert brief.routed and sorted(t.id for t in brief.topics) == ["gpu-quota-migration", "vendor-evaluation"]
    user = brain.llm.calls[-1][1]
    assert "<request>where are we on" in user and user.count("<topic ") == 2


def test_brief_asks_when_a_name_means_two_different_things(local_storage, state, tmp_path):
    from tests.conftest import FAKE_LLM_SETTINGS, make_brain

    route = {"targets": [], "clarify": "Which Jordan do you mean?"}
    b = make_brain(
        local_storage, state, tmp_path, fake_llm=True, responders={"brief_route": route}, llm=FAKE_LLM_SETTINGS
    )
    set_now(b)
    write(b, "Met Jordan Lee about hiring.")
    write(b, "Jordan Park sent the invoice.", 1)
    with pytest.raises(AmbiguousTarget, match="Which Jordan do you mean") as e:
        b.brief("Jordan")
    assert {m.id for m in e.value.matches} == {"jordan-lee", "jordan-park"}
    b.close()


def test_context_lists_threads_open_items_and_changes(brain):
    seed(brain)
    set_now(brain, 6)
    text = brain.context_text()
    assert text.startswith("# brain: the user's work journal, as of 2026-10-07 (Wednesday)")
    assert "- GPU quota migration [gpu-quota-migration]: last activity 2026-10-06 (1d), 2 open" in text
    assert "  Now: " in text
    assert "2026-10-03 (4d) to do · GPU quota migration: Jane Doe needs to file the quota ticket" in text
    assert "## Changed in the last 7 days" in text and "Replaced:" in text
    assert "(provisional)" not in text
    assert text.count("\n") <= brain.settings.context.max_lines


def test_context_stays_within_its_limits(local_storage, state, tmp_path):
    from tests.conftest import FAKE_LLM_SETTINGS, make_brain

    b = make_brain(
        local_storage,
        state,
        tmp_path,
        fake_llm=True,
        llm=FAKE_LLM_SETTINGS,
        context={"max_lines": 12, "max_bytes": 900},
    )
    set_now(b)
    for i in range(6):
        write(b, f"We need to plan step {i} of the [[Project {i} rollout]]. Is step {i} funded?", i * 0.1)
        write(b, f"More about the [[Project {i} rollout]].", i * 0.1 + 0.05)
    text = b.context_text()
    assert len(text.splitlines()) <= 12 and len(text.encode()) <= 900
    assert "more lines not shown" in text and text.rstrip().endswith("jot a note saying what is wrong.")
    b.close()
