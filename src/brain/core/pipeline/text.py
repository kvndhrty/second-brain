"""Text helpers shared by the pipeline steps."""

from __future__ import annotations

import difflib
import html
import math


def attr(value: object) -> str:
    """Escape a value for use inside a double-quoted attribute in a prompt."""
    return html.escape(str(value), quote=True)


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
