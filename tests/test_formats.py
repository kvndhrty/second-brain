from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from brain.core.formats.decisions import Decisions, FragmentDecision
from brain.core.formats.entity import Person, Thread, parse_entity
from brain.core.formats.entry import Entry, content_hash
from brain.core.formats.sidecars import EmbeddingFile, EmbeddingItem, Extraction, ThreadRef, encode_vector
from brain.core.formats.yamlio import FormatError, UnsupportedFormat

EST = timezone(timedelta(hours=-4))


def test_entry_roundtrip_keeps_offset_and_body():
    e = Entry(
        id="e-20261030T183205Z",
        created=datetime(2026, 10, 30, 14, 32, 5, tzinfo=EST),
        updated=datetime(2026, 10, 30, 14, 32, 5, tzinfo=EST),
        title="Vendor call",
        body="Line one.\n\n- a bullet: with colon\n---\nnot frontmatter\n",
    )
    text = e.to_text()
    back = Entry.parse(text)
    assert back.created == e.created and back.created.utcoffset() == timedelta(hours=-4)
    assert back.body == e.body
    assert back.title == "Vendor call"
    assert back.content_hash == e.content_hash
    assert Entry.parse(back.to_text()).to_text() == text


def test_entry_hand_edits_keep_comments_and_unknown_keys():
    text = (
        "---\nformat: 1\nid: e-1  # my id\ncreated: 2026-10-30T09:00:00-04:00\n"
        "updated: 2026-10-30T09:00:00-04:00\nmood: tired\n---\nBody\n"
    )
    e = Entry.parse(text)
    e.title = "Added later"
    out = e.to_text()
    assert "# my id" in out and "mood: tired" in out and "title: Added later" in out


def test_naive_timestamp_is_taken_as_local_time():
    e = Entry.parse("---\nid: e-1\ncreated: 2026-10-30 09:00:00\nupdated: 2026-10-30 09:00:00\n---\nx\n")
    assert e.created.tzinfo is not None


def test_content_hash_ignores_line_endings_and_trailing_newlines():
    assert content_hash("a\r\nb\n\n") == content_hash("a\nb")


@pytest.mark.parametrize("text", ["no frontmatter", "---\nid: x\nno close", "---\n- list\n---\n"])
def test_entry_parse_errors(text):
    with pytest.raises(FormatError):
        Entry.parse(text)


def test_newer_format_is_refused():
    with pytest.raises(UnsupportedFormat, match="Upgrade brain"):
        Entry.parse("---\nformat: 2\nid: e-1\ncreated: 2026-10-30T09:00:00Z\nupdated: 2026-10-30T09:00:00Z\n---\nx\n")


def test_person_roundtrip_preserves_comments_and_custom_keys():
    text = (
        "---\nformat: 1\nid: jane-doe\ntype: person\nname: Jane Doe\n"
        "aliases: [Jane, JD]  # how people refer to her\nrole: PM\nfavorite_tool: vim\n"
        "memberships:\n  - org: platform-team\n    from: 2025-03\n    to:\n---\nLikes written agendas.\n"
    )
    p = parse_entity(text, where="people/jane-doe.md")
    assert isinstance(p, Person)
    assert p.memberships[0].from_ == "2025-03"
    assert p.extra_fields() == {"favorite_tool": "vim"}
    p.aliases.append("J. Doe")
    out = p.to_text()
    assert "favorite_tool: vim" in out
    assert "Likes written agendas." in out
    assert "J. Doe" in out
    again = parse_entity(out, where="x")
    assert again.aliases == ["Jane", "JD", "J. Doe"]


def test_unchanged_list_keeps_its_comment():
    text = "---\nid: jane\ntype: person\nname: Jane\naliases: [Jane, JD]  # keep me\n---\n"
    p = parse_entity(text, where="x")
    p.role = "PM"
    assert "# keep me" in p.to_text()


def test_bare_year_membership_and_type_from_directory():
    p = parse_entity(
        "---\nid: bob\nname: Bob\nmemberships:\n  - org: x\n    from: 2024\n---\n",
        where="people/bob.md",
        default_type="person",
    )
    assert p.type == "person" and p.memberships[0].from_ == "2024"


def test_new_entity_text_is_readable():
    t = Thread(id="gpu-quota", name="GPU quota migration")
    text = t.to_text()
    assert text.startswith("---\nformat: 1\nid: gpu-quota\ntype: thread\nname: GPU quota migration\n")
    assert parse_entity(text, where="threads/gpu-quota.md").name == "GPU quota migration"


def test_entity_validation_error_names_the_file():
    with pytest.raises(FormatError, match="themes/x.md"):
        parse_entity("---\nid: x\ntype: theme\nname: X\nkind: vibes\n---\n", where="themes/x.md")


def test_decisions_roundtrip_and_typo_rejected():
    d = Decisions(entry="e-1")
    d.fragments["f-1"] = FragmentDecision(status="overcome_by_events", obe={"date": "2026-11-04", "reason": "gone"})
    d.mentions["person"] = {"Mike": "mike-chen", "the vendor": None}
    text = d.to_text()
    back = Decisions.parse(text)
    assert back.fragments["f-1"].obe.reason == "gone"
    assert back.mention_target("person", "the vendor") == (True, None)
    assert back.mention_target("person", "Sam") == (False, None)
    with pytest.raises(FormatError):
        Decisions.parse("entry: e-1\nfragments:\n  f-1:\n    stauts: confirmed\n")


def test_decisions_edit_keeps_comments():
    d = Decisions.parse("entry: e-1  # the standup\nfragments:\n  f-1:\n    dream: true  # someday\n")
    d.fragments["f-2"] = FragmentDecision(state="closed")
    out = d.to_text()
    assert "# the standup" in out and "# someday" in out and "state: closed" in out


def test_thread_ref_requires_exactly_one_target():
    with pytest.raises(ValueError):
        ThreadRef(weight=0.5)
    with pytest.raises(ValueError):
        ThreadRef(thread="a", new={"name": "b"})


def test_embedding_vectors_roundtrip_and_dim_checked():
    vec = [0.5, -0.25, 1.0]
    f = EmbeddingFile(
        entry_id="e-1", model="m", dim=3, items=[EmbeddingItem(id="e-1", hash="h", vec=encode_vector(vec))]
    )
    back = EmbeddingFile.parse(f.to_bytes(), where="x")
    assert back.items[0].vector() == vec
    with pytest.raises(FormatError):
        EmbeddingFile.parse(f.to_bytes().replace(b'"dim": 3', b'"dim": 4'), where="x")


def test_extraction_parse():
    x = Extraction(
        entry_id="e-1",
        entry_hash="sha256:x",
        model="m",
        prompt={"name": "extract", "version": 1, "sha256": "s"},
        created_at=datetime.now(UTC),
        fragments=[{"id": "f-1", "kind": "question", "text": "When?", "threads": [{"new": {"name": "T"}}]}],
    )
    back = Extraction.parse(x.to_bytes(), where="x")
    assert back.fragments[0].threads[0].surface == "T"
