"""Text helpers shared by the layers: prompt elements, quote location, similarity."""

from __future__ import annotations

import difflib
import html
import math
import re
from collections.abc import Iterable
from datetime import date
from typing import TYPE_CHECKING

from brain.core.temporal import dated, last_mentioned

if TYPE_CHECKING:
    from brain.core.formats.note import Note
    from brain.core.index.queries import Ent, Stmt, World

NOTE_CHARS = 6000
# A sentence ends at . ! or ? followed by space, but not after an initial ("J. Doe").
SENTENCE_END = re.compile(r"(?<=[.!?])(?<!\b[A-Z]\.)\s+|\n+")


def attr(value: object) -> str:
    """Escape a value for use inside a double-quoted attribute in a prompt."""
    return html.escape(str(value), quote=True)


def note_el(note: Note, today: date) -> str:
    """The note as a prompt element, with its date and age."""
    body = note.body.strip()
    if len(body) > NOTE_CHARS:
        body = body[:NOTE_CHARS] + "\n[…]"
    about = f' about_page="{attr(note.about)}"' if note.about else ""
    return f'<note id="{note.id}" {dated(note.created, today)}{about}>\n{body}\n</note>'


def stmt_el(s: Stmt, today: date, *, tag: str = "statement", with_id: bool = True, extra: str = "") -> str:
    """A statement as a prompt element, always with its note, date, age, and kind."""
    head = f'id="{attr(s.id)}" ' if with_id else ""
    status = f' status="{s.status}"' if s.status != "current" else ""
    return (
        f'<{tag} {head}note="{s.note_id}" {dated(s.created_at, today)} kind="{s.kind}"{status}{extra}>{s.text}</{tag}>'
    )


def candidate_el(world: World, e: Ent, today: date, *, recent: int = 3) -> str:
    """An entity as a candidate: its names, when it was last active, and its newest statements."""
    names = world.names_of(e.id)
    stmts = [s for s in world.of_entity(e.id) if s.status == "current"][-recent:]
    role = e.record.profile.get("role")
    head = (
        f'<candidate id="{attr(e.id)}" type="{e.type}" status="{e.status}" '
        f"{last_mentioned(world.last_activity(e.id), today)}>"
    )
    lines = [head, f"names: {'; '.join(names)}"]
    if role:
        lines.append(f"role: {role}")
    lines += [stmt_el(s, today, with_id=False) for s in stmts]
    lines.append("</candidate>")
    return "\n".join(lines)


def check_citations(cited: Iterable[str], allowed: set[str], *, where: str = "the input") -> None:
    """Raise ValueError (which makes the model retry) if a citation is not a note id from its input."""
    bad = sorted(set(cited) - allowed)
    if bad:
        raise ValueError(f"citations must be note ids from {where}; not allowed: {', '.join(bad)}")


def locate_quote(body: str, quote: str) -> tuple[int, int] | None:
    """Character span of `quote` in `body`. Models often alter quotes slightly, so fall back to fuzzy matching."""
    quote = quote.strip()
    if not quote:
        return None
    i = body.find(quote)
    if i >= 0:
        return i, i + len(quote)
    lower = body.lower()
    i = lower.find(quote.lower())
    if i >= 0:
        return i, i + len(quote)
    m = difflib.SequenceMatcher(None, lower, quote.lower(), autojunk=False).find_longest_match(
        0, len(lower), 0, len(quote)
    )
    if m.size >= max(8, int(0.6 * len(quote))):
        start = max(0, m.a - m.b)
        return start, min(len(body), start + len(quote))
    return None


def sentences(text: str) -> list[str]:
    """Split text into sentences and lines; the fallback when the split layer gets no valid answer."""
    parts = SENTENCE_END.split(text)
    return [p.strip(" \t-*") for p in parts if len(p.strip(" \t-*")) >= 3]


def sentence_with(text: str, surface: str) -> str:
    """The sentence of `text` that contains `surface`, or the start of the text."""
    low = surface.casefold()
    for s in sentences(text):
        if low in s.casefold():
            return s
    return text.strip()[:300]


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{3,}", text.lower()))


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)
