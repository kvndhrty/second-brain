"""The layers that run when a note arrives, against the scripted fake model.

The fake's rules (see brain.core.llm.fake): sentences become statements;
capitalized words are people; `[[Name]]` names a thread; a later statement
that shares most words with an open question answers it; one that shares
words with a decision and says "instead", "moved", or "now" supersedes it.
"""

from __future__ import annotations

from brain.core import keys
from brain.core.formats.records import LinksFile, SplitFile, StateFile
from brain.core.index.db import snapshot
from brain.core.llm.models import LLMError
from brain.core.llm.prompts import load_prompt
from tests.conftest import FAKE_LLM_SETTINGS, entity, make_brain, set_now, statements, write

KICKOFF = (
    "Met Jane Doe about the [[GPU quota migration]]. We decided to migrate in December. Does MIG work on new nodes?"
)


def kinds(brain, note_id):
    return [s.kind for s in statements(brain, note_id)]


def thread_page(brain, name="GPU quota migration"):
    return brain.page(name)


# --- layers 1 to 5 -------------------------------------------------------------------


def test_note_becomes_statements_names_and_a_thread(brain):
    nid = write(brain, KICKOFF)
    stmts = statements(brain, nid)
    assert [s.text for s in stmts] == [
        "Met Jane Doe about the GPU quota migration.",
        "We decided to migrate in December.",
        "Does MIG work on new nodes?",
    ]
    assert kinds(brain, nid) == ["fact", "decision", "question"]
    assert [s.id for s in stmts] == [f"s-{nid[2:]}-0{i}" for i in (1, 2, 3)]
    jane = entity(brain, "Jane Doe")
    assert jane.type == "person" and jane.status == "active"  # a full name gets a page at once
    thread = entity(brain, "GPU quota migration")
    assert thread.type == "thread" and thread.status == "provisional"  # one note is not yet a thread
    assert {s.thread for s in stmts} == {thread.id}
    assert entity(brain, "MIG").status == "provisional"
    assert brain.status()["pending"] == 0


def test_every_layer_file_is_written_with_its_prompt_stamp(brain):
    nid = write(brain, KICKOFF)
    for layer in keys.LAYERS:
        data = brain.storage.get(keys.layer_key(nid, layer))
        assert b'"input_hash"' in data and b'"prompts"' in data, layer
    split = SplitFile.parse(brain.storage.get(keys.layer_key(nid, "split")), where="split")
    assert split.prompts == [load_prompt("split").stamp] and split.model == "fake-small"
    assert all(s.quote in KICKOFF for s in split.statements)


def test_second_note_confirms_the_thread_and_gives_it_a_page(brain):
    write(brain, KICKOFF)
    assert not list(brain.storage.list("wiki/threads/"))
    write(brain, "Jane Doe needs to file the quota ticket for the [[GPU quota migration]].", 1)
    assert entity(brain, "GPU quota migration").status == "active"
    page = brain.storage.get("wiki/threads/gpu-quota-migration.md").decode()
    assert "# GPU quota migration" in page and "## Open" in page and "to do · Jane Doe needs to file" in page
    assert "[[jane-doe|Jane Doe]]" in page
    assert "GPU quota migration (thread) appeared in a second note" in " ".join(e.text for e in brain.log())


def test_short_name_links_to_the_known_person(brain):
    write(brain, "Met Jane Doe about budgets.")
    nid = write(brain, "Then Jane sent the numbers.", 1)
    links = LinksFile.parse(brain.storage.get(keys.layer_key(nid, "links")), where="links").links
    assert [(ln.surface, ln.entity, ln.how) for ln in links] == [("Jane", "jane-doe", "model")]
    assert "Jane" in brain.page("Jane Doe").names
    assert len(brain.world().roots("person")) == 1
    # The name is now known, so the next mention is settled by code, without a model call.
    asked = brain.llm.prompts_called().count("link")
    nid = write(brain, "Later Jane approved it.", 2)
    links = LinksFile.parse(brain.storage.get(keys.layer_key(nid, "links")), where="links").links
    assert links[0].how == "exact"
    assert brain.llm.prompts_called().count("link") == asked


def test_single_name_person_is_provisional_until_a_second_note(brain):
    write(brain, "Talked with Priya today.")
    assert entity(brain, "Priya").status == "provisional"
    assert brain.pages("person") == []
    write(brain, "Called Priya again.", 1)
    assert entity(brain, "Priya").status == "active"
    assert [p.name for p in brain.pages("person")] == ["Priya"]


def test_statement_without_a_thread_is_left_unfiled(brain):
    nid = write(brain, "The office is closed on Friday.")
    assert [s.thread for s in statements(brain, nid)] == [None]
    assert brain.world().roots("thread") == []


# --- layer 6: later notes change earlier ones -------------------------------------------


def test_later_note_answers_an_open_question(brain):
    write(brain, KICKOFF)
    assert [s.text for s in brain.world().open_items()] == ["Does MIG work on new nodes?"]
    write(brain, "Confirmed: MIG does work on new nodes for the [[GPU quota migration]].", 2)
    assert brain.world().open_items() == []
    q = next(s for s in thread_page(brain).history if s.kind == "question")
    assert q.status == "answered" and q.replaced_by.startswith("Confirmed: MIG does work")


def test_later_note_supersedes_a_decision(brain):
    write(brain, "We decided to migrate the [[GPU quota migration]] in December.")
    write(brain, "We decided to migrate the [[GPU quota migration]] in January instead.", 5)
    page = thread_page(brain)
    assert [s.text for s in page.decided] == ["We decided to migrate the GPU quota migration in January instead."]
    old = next(s for s in page.history if "December" in s.text)
    assert old.status == "superseded" and "January" in old.replaced_by
    md = brain.storage.get("wiki/threads/gpu-quota-migration.md").decode()
    assert "~~We decided to migrate the GPU quota migration in December.~~ Replaced by:" in md
    assert any(e.kind == "supersedes" for e in brain.log())


def test_repeated_statement_is_recorded_once_on_the_page(brain):
    write(brain, "The [[Vendor evaluation]] covers three storage vendors.")
    write(brain, "The [[Vendor evaluation]] covers three storage vendors.", 1)
    assert len(thread_page(brain, "Vendor evaluation").history) == 1


# --- layer 6b: corrections are notes ---------------------------------------------------


def test_same_person_note_merges_two_people(brain):
    write(brain, "Met Dana Kim about the audit.")
    write(brain, "Robin Kimura sent the audit report.", 1)
    assert len(brain.world().roots("person")) == 2
    nid = write(brain, "Dana Kim and Robin Kimura are the same person.", 2)
    assert kinds(brain, nid) == ["correction"]
    (person,) = brain.world().roots("person")
    assert person.name == "Dana Kim" and "Robin Kimura" in brain.page("Dana Kim").names
    assert len(brain.page("Robin Kimura").history) == 2  # both names lead to one page with both statements
    assert not brain.storage.exists("wiki/people/robin-kimura.md")
    assert brain.vault.get_entity("robin-kimura").set_by["merged_into"] == f"note:{nid}"


def test_not_the_same_note_undoes_a_merge_and_blocks_it(brain):
    write(brain, "Met Dana Kim about the audit.")
    write(brain, "Robin Kimura sent the audit report.", 1)
    write(brain, "Dana Kim and Robin Kimura are the same person.", 2)
    write(brain, "Dana Kim is different from Robin Kimura.", 3)
    assert sorted(p.name for p in brain.world().roots("person")) == ["Dana Kim", "Robin Kimura"]
    assert brain.vault.get_entity("dana-kim").not_same_as == ["robin-kimura"]
    assert [s.text for s in brain.page("Robin Kimura").history] == ["Robin Kimura sent the audit report."]


def test_rename_note_keeps_the_old_name(brain):
    write(brain, "Started the [[Quota thing]] today.")
    write(brain, "More on the [[Quota thing]] today.", 1)
    write(brain, "The Quota thing should be called GPU quota migration.", 2)
    page = brain.page("Quota thing")
    assert page.name == "GPU quota migration" and page.names == ["Quota thing"] and page.id == "quota-thing"
    assert brain.vault.get_entity("quota-thing").set_by["name"].startswith("note:")


def test_close_and_reopen_notes(brain):
    write(brain, KICKOFF)
    write(brain, "Jane Doe needs to file the quota ticket for the [[GPU quota migration]].", 1)
    write(brain, "The GPU quota migration is finished.", 2)
    page = thread_page(brain)
    assert page.status == "closed" and page.now is None and page.open == []
    assert brain.active_threads() == [] and brain.world().open_items() == []
    assert "Everything below is history" in brain.storage.get("wiki/threads/gpu-quota-migration.md").decode()
    # A thread a note closed stays closed when new statements arrive.
    write(brain, "One more remark on the [[GPU quota migration]].", 3)
    assert thread_page(brain).status == "closed"
    write(brain, "Reopen the GPU quota migration.", 4)
    assert thread_page(brain).status == "active"


def test_retract_note_hides_statements_but_keeps_the_note(brain):
    wrong = write(brain, "The [[Outage review]] found the cause was a bad disk.")
    write(brain, "The [[Outage review]] meets again on Monday.", 1)
    nid = write(brain, "Ignore what I wrote: the cause was a bad disk is wrong.", 2)
    state = StateFile.parse(brain.storage.get(keys.layer_key(nid, "state")), where="state")
    assert [op.op for op in state.operations] == ["retract"]
    assert [s.text for s in brain.page("Outage review").history] == ["The Outage review meets again on Monday."]
    assert "bad disk" not in brain.storage.get("wiki/threads/outage-review.md").decode()
    assert brain.note(wrong).body.startswith("The [[Outage review]] found")  # the note itself is untouched
    assert brain.note(wrong).statements[0].status == "retracted"
    answer = brain.ask("What was the cause of the outage?")
    assert all("bad disk" not in c.text for c in answer.claims)


def test_retracting_the_retraction_restores_the_statements(brain):
    write(brain, "The [[Outage review]] found the cause was a bad disk.")
    write(brain, "The [[Outage review]] meets again on Monday.", 1)
    write(brain, "Ignore what I wrote: the cause was a bad disk is wrong.", 2)
    write(brain, "Ignore the note that said ignore what I wrote the cause was a bad disk is wrong.", 3)
    assert any("bad disk" in s.text for s in brain.page("Outage review").history)


def test_not_part_of_note_moves_statements_to_their_own_thread(brain):
    write(brain, "The [[GPU quota migration]] needs the #labels work first.")
    write(brain, "The #labels schema for the [[GPU quota migration]] is drafted.", 1)
    write(brain, "The quota request for the [[GPU quota migration]] went in.", 2)
    write(brain, "The labels are not part of the GPU quota migration.", 3)
    assert [s.text for s in brain.page("GPU quota migration").history] == [
        "The quota request for the GPU quota migration went in."
    ]
    labels = [p for p in brain.pages("thread") if p.name == "labels"]
    assert labels and len(brain.page(labels[0].id).history) == 2


def test_this_is_wrong_note_is_read_as_a_correction_to_that_page(brain):
    write(brain, KICKOFF)
    write(brain, "Jane Doe needs to file the quota ticket for the [[GPU quota migration]].", 1)
    nid = write(brain, "This is finished.", 2, about="gpu-quota-migration")
    assert brain.note(nid).about == "gpu-quota-migration"
    assert thread_page(brain).status == "closed"


def test_a_correction_nobody_can_place_is_logged_not_applied(brain):
    write(brain, "Reopen the thing we talked about.")
    assert any(e.kind == "ignored" for e in brain.log())


# --- reliability ---------------------------------------------------------------------


def test_running_again_calls_no_model(brain):
    nid = write(brain, KICKOFF)
    before = len(brain.llm.calls)
    result = brain.process(nid).notes[0]
    assert result.layers == [] and result.error is None
    assert len(brain.llm.calls) == before


def test_invalid_model_output_falls_back_and_does_not_block_the_note(local_storage, state, tmp_path):
    bad = {
        "split": {"statements": [{"text": "Invented.", "quote": "words that are not in the note"}]},
        "classify": {"kind": "nonsense"},
        "names": {"names": [{"name": "Nobody Mentioned", "type": "person"}]},
        "file": {"choice": "not-a-candidate"},
    }
    b = make_brain(local_storage, state, tmp_path, fake_llm=True, responders=bad, llm=FAKE_LLM_SETTINGS)
    set_now(b)
    nid = write(b, "Budget review is on Friday. Send the numbers before then.")
    stmts = statements(b, nid)
    # Split fell back to one statement per sentence; classify to "fact"; names to none; filing to unfiled.
    assert [s.text for s in stmts] == ["Budget review is on Friday.", "Send the numbers before then."]
    assert {s.kind for s in stmts} == {"fact"} and {s.thread for s in stmts} == {None}
    assert b.world().roots() == []
    assert b.status()["pending"] == 0 and b.failures() == {}
    assert sum(e.kind == "fallback" for e in b.log()) >= 4
    b.close()


def test_escalation_retries_on_the_strong_model(local_storage, state, tmp_path):
    seen = []

    def classify(user: str) -> dict:
        seen.append(user)
        return {"kind": "nonsense"}

    cfg = FAKE_LLM_SETTINGS | {"strong_model": "fake-strong", "escalate": "strong"}
    b = make_brain(local_storage, state, tmp_path, fake_llm=True, responders={"classify": classify}, llm=cfg)
    set_now(b)
    write(b, "Budget review is on Friday.")
    models = {m for (_, m) in b.llm._models}  # (role, model name) of every model the run used
    assert "fake-strong" in models and "fake-small" in models
    b.close()


def test_endpoint_failure_leaves_the_note_pending(local_storage, state, tmp_path):
    def down(_user: str) -> dict:
        raise LLMError("gateway is down")

    b = make_brain(local_storage, state, tmp_path, fake_llm=True, responders={"classify": down}, llm=FAKE_LLM_SETTINGS)
    set_now(b)
    nid = b.jot("Budget review is on Friday.").note.id
    result = b.process(nid).notes[0]
    assert result.error and result.error.startswith("classify:")
    assert b.status()["pending"] == 1 and nid in b.failures()
    b.llm.responders.pop("classify")
    from brain.core.llm.fake import DEFAULT_RESPONDERS

    b.llm.responders["classify"] = DEFAULT_RESPONDERS["classify"]
    assert b.process_pending().failed == []
    assert b.status()["pending"] == 0 and b.failures() == {}
    b.close()


def test_page_sentence_that_fails_the_check_is_dropped(local_storage, state, tmp_path):
    responders = {"now": {"text": "UNSUPPORTED claim about Mars.", "citations": []}}

    def now(user: str) -> dict:
        import re

        note = re.search(r'note="([^"]+)"', user).group(1)
        return {"text": "UNSUPPORTED claim about Mars.", "citations": [note]}

    responders["now"] = now
    b = make_brain(local_storage, state, tmp_path, fake_llm=True, responders=responders, llm=FAKE_LLM_SETTINGS)
    set_now(b)
    write(b, KICKOFF)
    write(b, "Jane Doe needs to file the quota ticket for the [[GPU quota migration]].", 1)
    assert b.page("GPU quota migration").now is None  # the check layer rejected it, so nothing was written
    b.close()


# --- the wiki and the index ------------------------------------------------------------


def test_wiki_index_and_log_are_written(brain):
    write(brain, KICKOFF)
    write(brain, "Jane Doe needs to file the quota ticket for the [[GPU quota migration]].", 1)
    index = brain.storage.get(keys.WIKI_INDEX).decode()
    assert "[[gpu-quota-migration|GPU quota migration]]: " in index and "[[jane-doe|Jane Doe]]" in index
    log = brain.storage.get(keys.WIKI_LOG).decode()
    assert "New person: Jane Doe" in log and "## 2026-10-02" in log
    person = brain.storage.get("wiki/people/jane-doe.md").decode()
    assert person.startswith("---\nid: jane-doe\ntype: person\n")


def test_markdown_file_saved_into_notes_becomes_a_note(brain):
    brain.storage.put("notes/standup monday.md", b"Jane Doe is out this week.\n")
    report = brain.process_pending()
    assert len(report.notes) == 1 and report.failed == []
    (note,) = brain.recent_notes()
    assert note.label == "standup monday" and note.done
    assert brain.note(note.id).body == "Jane Doe is out this week.\n"
    assert not brain.storage.exists("notes/standup monday.md")
    assert entity(brain, "Jane Doe")


def test_reindex_gives_the_same_index(brain):
    write(brain, KICKOFF)
    write(brain, "Confirmed: MIG does work on new nodes for the [[GPU quota migration]].", 2)
    write(brain, "The GPU quota migration is finished.", 3)
    before = snapshot(brain.db)
    brain.reindex()
    assert snapshot(brain.db) == before


def test_rebuild_replays_every_note_and_keeps_page_ids(brain):
    write(brain, "Met Dana Kim about the audit.")
    write(brain, "Robin Kimura sent the audit report.", 1)
    write(brain, "Dana Kim and Robin Kimura are the same person.", 2)
    write(brain, KICKOFF, 3)
    write(brain, "Confirmed: MIG does work on new nodes for the [[GPU quota migration]].", 4)
    pages_before = sorted((p.id, p.name, p.status) for p in brain.pages())
    open_before = [s.text for s in brain.world().open_items()]
    report = brain.rebuild()
    assert report.notes == 5 and report.failed == [] and report.calls > 0
    assert sorted((p.id, p.name, p.status) for p in brain.pages()) == pages_before
    assert [s.text for s in brain.world().open_items()] == open_before
    assert len(brain.world().roots("person")) == 2  # Dana Kim (merged with Robin Kimura) and Jane Doe
    assert brain.storage.exists("wiki/people/dana-kim.md")
