"""The layers that run when a note arrives, against the scripted fake model.

The fake's rules (see brain.core.llm.fake): sentences become statements;
capitalized words are people; `[[Name]]` names a thread; a later statement
that shares most words with an open question answers it; one that shares
words with a decision and says "instead", "moved", or "now" supersedes it.
"""

from __future__ import annotations

from brain.core import keys
from brain.core.formats.records import LinksFile, StateFile
from brain.core.index.db import snapshot
from brain.core.llm.fake import DEFAULT_RESPONDERS
from brain.core.llm.models import LLMError
from brain.core.pipeline import state as state_layer
from brain.core.pipeline.context import Calls
from tests.conftest import FAKE_LLM_SETTINGS, drop_file, entity, make_brain, set_now, statements, write

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
    assert len({s.id for s in stmts}) == 3 and all(keys.note_of_statement(s.id) == nid for s in stmts)
    jane = entity(brain, "Jane Doe")
    assert jane.type == "person" and jane.status == "active"  # a full name gets a page at once
    thread = entity(brain, "GPU quota migration")
    assert thread.type == "thread" and thread.status == "provisional"  # one note is not yet a thread
    assert {s.thread for s in stmts} == {thread.id}
    assert entity(brain, "MIG").status == "provisional"
    assert brain.status()["pending"] == 0


def test_second_note_confirms_the_thread_and_gives_it_a_page(brain):
    write(brain, KICKOFF)
    assert not list(brain.storage.list("wiki/threads/"))
    write(brain, "Jane Doe needs to file the quota ticket for the [[GPU quota migration]].", 1)
    assert entity(brain, "GPU quota migration").status == "active"
    page = brain.storage.get("wiki/threads/gpu-quota-migration.md").decode()
    assert "# GPU quota migration" in page and "## Open" in page and "to do · Jane Doe needs to file" in page
    assert "[[jane-doe|Jane Doe]]" in page
    assert "GPU quota migration (thread) appeared in a second note" in " ".join(e.text for e in brain.log())
    # The same rule for a person written with one name: no page until a second note.
    write(brain, "Talked with Priya today.", 2)
    assert entity(brain, "Priya").status == "provisional" and "Priya" not in [p.name for p in brain.pages("person")]
    write(brain, "Called Priya again.", 3)
    assert entity(brain, "Priya").status == "active" and "Priya" in [p.name for p in brain.pages("person")]


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


def test_a_repeat_shares_the_fate_of_what_it_repeats(brain):
    write(brain, "We decided the [[Launch plan]] date is March 3.")
    write(brain, "We decided the [[Launch plan]] date is March 3.", 1)
    assert len(brain.page("Launch plan").history) == 1  # said twice, shown once
    write(brain, "We decided the [[Launch plan]] date is April 9 instead.", 2)
    page = brain.page("Launch plan")
    assert [s.text for s in page.decided] == ["We decided the Launch plan date is April 9 instead."]
    assert all(s.status == "superseded" for s in page.history if "March" in s.text)


def test_a_topic_name_can_mean_a_thread(brain):
    write(brain, "Started [[SRE]] hiring today.")
    nid = write(brain, "The SRE budget was approved.", 1)
    links = LinksFile.parse(brain.storage.get(keys.layer_key(nid, "links")), where="links").links
    assert [(ln.surface, ln.entity) for ln in links] == [("SRE", "sre")]
    assert [e.type for e in brain.world().roots()] == ["thread"]  # no second "SRE" was created as a topic


# --- layer 6b: corrections are notes ---------------------------------------------------


def test_same_person_note_about_names_already_linked_changes_nothing(brain):
    write(brain, "Met Jane Doe about budgets.")
    write(brain, "Jane Doe and Jane are the same person.", 1)
    kinds_seen = [e.kind for e in brain.log()]
    assert "noted" in kinds_seen and "ignored" not in kinds_seen and "merged" not in kinds_seen
    assert len(brain.world().roots("person")) == 1


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


def test_closing_a_thread_by_another_wording(brain):
    write(brain, "Started the [[Vendor evaluation]] today.")
    write(brain, "Dana Okafor signed the contract for the [[Vendor evaluation]]. Choosing a vendor is finished.", 1)
    assert brain.page("Vendor evaluation").status == "closed"  # found through the note's other statements


def test_an_answer_that_is_not_the_asked_shape_is_rejected():
    """In prompted mode a small model sometimes returns the schema itself. That must not read as an empty answer."""
    import pytest
    from pydantic import ValidationError

    from brain.core.llm.schemas import AskOutput, NamesOut, SplitOut, SummaryOut, TargetsOut

    echoed = {"properties": {"targets": {"type": "array"}}, "type": "object"}
    for schema in (TargetsOut, NamesOut, SplitOut, SummaryOut, AskOutput):
        with pytest.raises(ValidationError):
            schema.model_validate(echoed)


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


def test_a_correction_cannot_merge_different_kinds_of_page_or_close_a_person(brain):
    write(brain, "Met Dana Kim about the [[Audit]].")
    nid = write(brain, "Dana Kim sent the report for the [[Audit]].", 1)
    brain.llm.responders["correction_op"] = {"op": "same_as"}
    brain.llm.responders["correction_targets"] = {"targets": ["audit", "dana-kim"]}
    write(brain, "Fix: Dana Kim and the Audit are the same.", 2)
    assert brain.world().root("dana-kim") == "dana-kim" and any(e.kind == "ignored" for e in brain.log())
    note, _ = brain.vault.get_note(nid)
    assert state_layer.apply(brain._ctx(), Calls(brain.llm), note, "s-x", "close", ["dana-kim"], None) is None
    assert entity(brain, "Dana Kim").status == "active"


def test_retracted_text_leaves_the_summary_even_when_it_cannot_be_rewritten(brain):
    write(brain, "The [[Outage review]] found the cause was a bad disk.")
    write(brain, "The [[Outage review]] meets again on Monday.", 1)
    assert "bad disk" in " ".join(s.text for s in brain.page("Outage review").summary)
    brain.llm.responders["check"] = {"supported": False}  # nothing the model writes now passes the check
    write(brain, "Ignore what I wrote: the cause was a bad disk is wrong.", 2)
    assert "bad disk" not in brain.storage.get("wiki/threads/outage-review.md").decode()


def test_editing_a_note_does_not_move_a_later_retraction_to_other_words(brain):
    first = write(brain, "The [[Outage review]] meets again on Monday. The [[Outage review]] found a bad disk.")
    write(brain, "The [[Outage review]] has three members.", 1)
    write(brain, "Ignore what I wrote: the Outage review found a bad disk is wrong.", 2)
    status = lambda: {s.text.split()[-1]: s.status for s in statements(brain, first)}  # noqa: E731
    assert status() == {"Monday.": "current", "disk.": "retracted"}
    key = brain.vault.find_note_key(first)
    head, body = brain.storage.get(key).decode().rsplit("---\n", 1)
    brain.storage.put(key, f"{head}---\nThe [[Outage review]] room is B12. {body}".encode())
    brain.sync(full=True)
    assert brain.process_pending().failed == []
    assert status() == {"B12.": "current", "Monday.": "current", "disk.": "retracted"}


# --- reliability ---------------------------------------------------------------------


def test_running_again_calls_no_model(brain):
    nid = write(brain, KICKOFF)
    before = len(brain.llm.calls)
    assert brain.process(nid).notes == [] and brain.process_pending().notes == []
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
        return {"kind": "nonsense"} if len(seen) <= 3 else {"kind": "decision"}  # the small model's three tries fail

    cfg = FAKE_LLM_SETTINGS | {"strong_model": "fake-strong", "escalate": "strong"}
    b = make_brain(local_storage, state, tmp_path, fake_llm=True, responders={"classify": classify}, llm=cfg)
    set_now(b)
    nid = write(b, "We decided the budget review is on Friday.")
    assert len(seen) > 1 and [s.kind for s in statements(b, nid)] == ["decision"]  # not the fallback, "fact"
    b.close()


def test_a_failed_note_stays_pending_and_later_notes_wait_for_it(local_storage, state, tmp_path):
    def down(_user: str) -> dict:
        raise LLMError("gateway is down")

    b = make_brain(local_storage, state, tmp_path, fake_llm=True, responders={"classify": down}, llm=FAKE_LLM_SETTINGS)
    first = write(b, "We decided to migrate the [[GPU quota migration]] in December.", process=False)
    result = b.process(first).result(first)
    assert result.error and result.error.startswith("classify:")
    assert b.status()["pending"] == 1 and b.failures()[first]["attempts"] == 1
    # A later note is not processed ahead of it: what it replaces depends on the earlier note.
    b.llm.responders["classify"] = DEFAULT_RESPONDERS["classify"]
    b._pipeline()._note_failure(first, "classify", "gateway is down")  # as if it had just failed again
    second = write(b, "We decided to migrate the [[GPU quota migration]] in January instead.", 5, process=False)
    held = b.process_pending(wait_for_retry=True)  # the worker: the failed note is not due yet
    assert held.notes == [] and held.held == first and "waits for an earlier note" in held.result(second).error
    done = b.process_pending()  # `brain process`: try now
    assert [n.note_id for n in done.notes] == [first, second] and done.failed == []
    assert b.status()["pending"] == 0 and b.failures() == {}
    assert [s.status for s in statements(b, first)] == ["superseded"]
    b.close()


def test_a_failure_while_writing_pages_is_tried_again(brain):
    def down(_user: str) -> dict:
        raise LLMError("gateway is down")

    write(brain, "Started the [[Vendor evaluation]] today.")
    brain.llm.responders["now"] = down
    nid = write(brain, "More on the [[Vendor evaluation]] today.", 1, process=False)
    assert brain.process(nid).result(nid).error.startswith("pages:")
    assert brain.status()["pending"] == 1 and not brain.storage.exists("wiki/threads/vendor-evaluation.md")
    brain.llm.responders["now"] = DEFAULT_RESPONDERS["now"]
    assert brain.process_pending().failed == [] and brain.status()["pending"] == 0
    assert brain.storage.exists("wiki/threads/vendor-evaluation.md")


def test_a_note_that_cannot_be_processed_can_be_left_out(brain):
    nid = write(brain, "Started the [[Vendor evaluation]] today.", process=False)
    brain.skip(nid)
    assert brain.status()["pending"] == 0 and statements(brain, nid) == []
    assert any(e.kind == "skipped" for e in brain.log())
    assert brain.rebuild().failed == [] and len(statements(brain, nid)) == 1  # a rebuild tries it again


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


def test_saving_gives_each_note_its_own_id_and_an_index_failure_is_not_a_failed_save(brain, monkeypatch):
    first, second = brain.jot("First.").note.id, brain.jot("Second.").note.id  # in the same second
    assert first != second and len(list(brain.storage.list("notes/"))) == 2

    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(brain.syncer, "apply_write", boom)
    third = brain.jot("Third.")  # the file is in the store; saying "failed" would make the user save it twice
    monkeypatch.undo()
    assert brain.storage.exists(third.key) and brain.pending_drafts() == []
    brain.sync(full=True)
    assert brain.status()["notes"] == 3


def test_markdown_file_saved_into_notes_becomes_a_note(brain, local_storage):
    brain.storage.put("notes/standup monday.md", b"Jane Doe is out this week.\n")
    assert brain.process_pending().notes == []  # just saved: an editor may still be writing it
    drop_file(local_storage, "notes/standup monday.md", b"Jane Doe is out this week.\n")
    report = brain.process_pending()
    assert len(report.notes) == 1 and report.failed == []
    (note,) = brain.recent_notes()
    assert note.label == "standup monday" and note.done
    assert brain.note(note.id).body == "Jane Doe is out this week.\n"
    assert not brain.storage.exists("notes/standup monday.md")
    assert entity(brain, "Jane Doe")


def test_files_brain_cannot_read_do_not_stop_the_others(brain, local_storage):
    notes = local_storage.root / "notes"
    (notes / ".obsidian").mkdir(parents=True)
    (notes / ".obsidian" / "workspace.md").write_text("not a note")
    (notes / "back\\slash.md").write_text("a name brain does not use")
    drop_file(local_storage, "notes/latin1.md", "caf\u00e9".encode("latin-1"))
    drop_file(local_storage, "notes/fine.md", b"Jane Doe is out this week.\n")
    report = brain.process_pending()
    assert len(report.notes) == 1 and report.failed == []
    assert brain.sync(full=True) and [n.label for n in brain.recent_notes()] == ["fine"]


def test_complete_note_file_put_into_notes_is_processed_where_it_is(brain, local_storage):
    text = "---\r\nformat: 2\r\nid: n-20260901T080000Z\r\ncreated: 2026-09-01T08:00:00Z\r\n---\r\nJane Doe is out.\r\n"
    write(brain, "The office is closed on Friday.")
    brain.sync(full=True)  # the index is up to date, so only looking in notes/ can find the new file
    drop_file(local_storage, "notes/imported/old.md", text.encode())
    report = brain.process_pending()
    assert [n.note_id for n in report.notes] == ["n-20260901T080000Z"] and report.failed == []
    assert local_storage.exists("notes/imported/old.md") and len(brain.recent_notes()) == 2
    assert any(e.kind == "out_of_order" for e in brain.log())  # it is dated before a note already processed


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
