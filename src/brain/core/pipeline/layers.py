"""Layers 1 to 3: split a note into statements, label each one, and list the names in the note."""

from __future__ import annotations

from datetime import date, datetime

from brain.core import keys
from brain.core.formats.note import Note
from brain.core.formats.records import Kind, NameRef, Statement, contains_name, norm_name
from brain.core.llm.schemas import KindOut, NamesOut, SplitItem, SplitOut
from brain.core.pipeline.context import Calls
from brain.core.pipeline.text import locate_quote, note_el, sentences
from brain.core.temporal import local_date, today_el

MAX_STATEMENTS = 60
SPLIT_PROMPTS = ("split",)
CLASSIFY_PROMPTS = ("classify",)
NAMES_PROMPTS = ("names",)


def split(calls: Calls, note: Note, today: date) -> list[Statement]:
    body = note.body

    def validate(out: SplitOut) -> None:
        if not out.statements:
            raise ValueError("the note is not empty; return at least one statement")
        if len(out.statements) > MAX_STATEMENTS:
            raise ValueError(f"at most {MAX_STATEMENTS} statements")
        bad = [s.quote[:60] for s in out.statements if locate_quote(body, s.quote) is None]
        if bad:
            raise ValueError("each quote must be copied exactly from the note; not found: " + " | ".join(bad))

    fallback = SplitOut(statements=[SplitItem(text=s, quote=s) for s in sentences(body)[:MAX_STATEMENTS]])
    out = calls.ask("split", note_el(note, today), SplitOut, fallback=fallback, validate=validate)
    seen: set[str] = set()
    ids: set[str] = set()
    result = []
    for item in out.statements:
        text = " ".join(item.text.split())
        if not text or norm_name(text) in seen:
            continue
        seen.add(norm_name(text))
        quote = item.quote.strip()
        # The quote is copied from the note, so it is the same on a second run; two statements
        # taken from one sentence are told apart by their text.
        sid = keys.statement_id(note.id, quote or text)
        if sid in ids:
            sid = keys.statement_id(note.id, f"{quote}\n{text}")
        ids.add(sid)
        result.append(Statement(id=sid, text=text, quote=quote))
    return result


def classify(calls: Calls, note: Note, statements: list[Statement], now: datetime) -> dict[str, Kind]:
    head = f"{today_el(now)}\n{note_el(note, local_date(now))}\n"
    kinds: dict[str, Kind] = {}
    for s in statements:
        out = calls.ask("classify", f"{head}<statement>{s.text}</statement>", KindOut, fallback=KindOut(kind="fact"))
        kinds[s.id] = out.kind
    return kinds


def names(calls: Calls, note: Note, today: date) -> list[NameRef]:
    def validate(out: NamesOut) -> None:
        bad = [n.name for n in out.names if not contains_name(n.name, note.body)]
        if bad:
            raise ValueError("return names exactly as written in the note; not in the note: " + ", ".join(bad))

    out = calls.ask("names", note_el(note, today), NamesOut, fallback=NamesOut(names=[]), validate=validate)
    seen: set[str] = set()
    result = []
    for n in out.names:
        surface = " ".join(n.name.split())
        if len(surface) < 2 or norm_name(surface) in seen:
            continue
        seen.add(norm_name(surface))
        result.append(NameRef(surface=surface, type=n.type))
    return result
