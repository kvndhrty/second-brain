"""Text helpers shared by the pipeline steps."""

from __future__ import annotations

import difflib
import html
import math
from collections.abc import Iterable
from datetime import date
from typing import TYPE_CHECKING

from brain.core.temporal import dated

if TYPE_CHECKING:
    from brain.core.index.queries import FragmentRow, ThreadRow


def attr(value: object) -> str:
    """Escape a value for use inside a double-quoted attribute in a prompt."""
    return html.escape(str(value), quote=True)


def fragment_el(
    f: FragmentRow, today: date, *, tag: str = "fragment", with_id: bool = True, status: bool = True, extra: str = ""
) -> str:
    """A fragment as a prompt element, always with its entry, date, age, and kind.

    with_id adds the fragment id (for outputs that refer to fragments); status
    adds its status and open/closed state; extra is appended to the attributes.
    """
    head = f'id="{attr(f.id)}" ' if with_id else ""
    tail = ""
    if status:
        tail = f' status="{f.status}"' + (f' state="{f.state}"' if f.state else "")
    return (
        f'<{tag} {head}entry="{f.entry_id}" {dated(f.created_at, today)} kind="{f.kind}"{tail}{extra}>{f.text}</{tag}>'
    )


def check_citations(cited: Iterable[str], allowed: set[str], *, where: str = "the input") -> None:
    """Raise ValueError (which makes the model retry) if a citation is not an entry id from its input."""
    bad = sorted(set(cited) - allowed)
    if bad:
        raise ValueError(f"citations must be entry ids from {where}; not allowed: {', '.join(bad)}")


def thread_el(t: ThreadRow) -> str:
    return f'<thread id="{attr(t.id)}" name="{attr(t.name)}" description="{attr(t.description or "")}"/>'


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


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)
