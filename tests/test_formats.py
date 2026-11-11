"""Format 2: notes, generated records, and the keys they are stored under."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from brain.core import keys
from brain.core.formats.note import Note
from brain.core.formats.records import EntityRecord, SplitFile, Statement, contains_name
from brain.core.formats.yamlio import FORMAT_VERSION, FormatError, UnsupportedFormat

NOW = datetime(2026, 11, 9, 14, 32, 5, tzinfo=UTC)


def test_note_round_trips_and_keeps_the_body_exactly():
    note = Note(id="n-20261109T143205Z", created=NOW, title="Vendor call", body="Line one.\n\n- a list\n")
    text = note.to_text()
    assert text.startswith("---\nformat: 2\nid: n-20261109T143205Z\n")
    back = Note.parse(text)
    assert back.body == "Line one.\n\n- a list\n" and back.title == "Vendor call" and back.about is None
    assert back.content_hash == note.content_hash and back.to_text() == text


def test_note_about_a_page_changes_its_hash():
    plain = Note(id="n-1", created=NOW, body="This is finished.")
    about = Note(id="n-1", created=NOW, body="This is finished.", about="gpu-quota-migration")
    assert plain.content_hash != about.content_hash
    assert "about: gpu-quota-migration" in about.to_text()


def test_note_from_a_newer_format_is_refused():
    with pytest.raises(UnsupportedFormat, match="Upgrade brain"):
        Note.parse(f"---\nformat: {FORMAT_VERSION + 1}\nid: n-1\ncreated: 2026-11-09T14:32:05Z\n---\nx\n")
    with pytest.raises(FormatError, match="frontmatter"):
        Note.parse("no frontmatter here")


def test_keys_and_ids():
    nid = keys.note_id_for(NOW)
    assert nid == "n-20261109T143205Z" and keys.note_id_for(NOW, 2) == "n-20261109T143205Z-2"
    assert keys.note_key(NOW, nid, "vendor-call") == "notes/2026/11/09/20261109T143205Z-vendor-call.md"
    sid = keys.statement_id(nid, "We  decided on December.")
    assert sid == keys.statement_id(nid, "we decided on december.") != keys.statement_id(nid, "We decided on January.")
    assert sid.startswith("s-20261109T143205Z-") and keys.note_of_statement(sid) == nid
    assert keys.note_of_statement(keys.statement_id(keys.note_id_for(NOW, 2), "x")) == "n-20261109T143205Z-2"
    assert keys.wiki_key("person", "jane-doe") == "wiki/people/jane-doe.md"
    assert keys.classify(keys.layer_key(nid, "split")) == keys.KeyInfo("layer", f"note:{nid}", layer="split")
    assert keys.classify(keys.entity_key("jane-doe")).unit == "entity:jane-doe"
    assert keys.classify(keys.page_key("jane-doe")).unit == "entity:jane-doe"
    assert keys.classify(keys.embedding_key("bge/small", nid)) == keys.KeyInfo(
        "embedding", f"note:{nid}", model_slug="bge_small"
    )
    assert keys.classify(keys.dream_run_key("20261109T030000Z")).unit == "dream:20261109T030000Z"
    for ignored in ("wiki/index.md", "wiki/people/jane-doe.md", keys.BRIEF_STATE, keys.LEASE, keys.DREAM_STATE):
        assert keys.classify(ignored) is None, ignored


def test_records_round_trip_and_reject_unknown_fields():
    split = SplitFile(
        note_id="n-1", input_hash="sha256:x", created_at=NOW, statements=[Statement(id="s-1-01", text="A.", quote="A.")]
    )
    assert SplitFile.parse(split.to_bytes(), where="split") == split
    with pytest.raises(FormatError, match="Extra inputs"):
        SplitFile.parse(split.to_bytes().replace(b'"note_id"', b'"surprise": 1, "note_id"'), where="split")
    with pytest.raises(FormatError, match="invalid JSON"):
        SplitFile.parse(b"{", where="split")


def test_entity_names():
    e = EntityRecord(id="jane-doe", type="person", name="Jane Doe", created_at=NOW)
    assert e.add_name("JD") and not e.add_name("jane  doe") and not e.add_name("jd")
    assert e.all_names() == ["Jane Doe", "JD"]
    e.set_by["status"] = "note:n-1"
    assert e.note_set("status") and not e.note_set("name")
    assert contains_name("Ann", "Ann's plan") and not contains_name("Ann", "Annual planning")
    # A merge gives the survivor the other's names; undoing it takes back only those.
    other = EntityRecord(id="j-doe", type="person", name="J. Doe", names=["JD"], created_at=NOW)
    other.merge_into(e, "note:n-2")
    assert e.all_names() == ["Jane Doe", "JD", "J. Doe"] and other.merged_into == "jane-doe"
    other.split_from(e, "note:n-3")
    assert e.all_names() == ["Jane Doe", "JD"] and other.merged_into is None
