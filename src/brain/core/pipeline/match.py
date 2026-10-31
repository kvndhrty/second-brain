"""Keep fragment ids stable across re-extraction.

Your decisions are keyed by fragment id, so when an entry is re-extracted the
new fragments are matched to the old ones and reuse their ids. Ids that are
not matched are never reused for a different fragment.
"""

from __future__ import annotations

import re

from brain.core.formats.sidecars import Extraction
from brain.core.pipeline.text import similarity

MATCH_THRESHOLD = 0.6


def _overlap(a: tuple[int, int] | None, b: tuple[int, int] | None) -> float:
    if not a or not b:
        return 0.0
    inter = max(0, min(a[1], b[1]) - max(a[0], b[0]))
    union = max(a[1], b[1]) - min(a[0], b[0])
    return inter / union if union else 0.0


def fragment_number(fragment_id: str) -> int:
    m = re.search(r"-(\d+)$", fragment_id)
    return int(m.group(1)) if m else 0


def assign_ids(
    entry_id: str,
    new: list[tuple[str, str, tuple[int, int] | None]],
    previous: Extraction | None,
) -> list[str]:
    """Ids for new fragments given as (kind, text, span), in order."""
    ids: list[str | None] = [None] * len(new)
    if previous is not None:
        scored = []
        for i, (kind, text, span) in enumerate(new):
            for old in previous.fragments:
                if old.kind != kind:
                    continue
                score = max(similarity(text, old.text), _overlap(span, old.span))
                if score >= MATCH_THRESHOLD:
                    scored.append((score, i, old.id))
        used_old: set[str] = set()
        for _score, i, old_id in sorted(scored, reverse=True):
            if ids[i] is None and old_id not in used_old:
                ids[i] = old_id
                used_old.add(old_id)
    taken = [fragment_number(f.id) for f in previous.fragments] if previous else []
    next_n = max(taken, default=0) + 1
    out = []
    for i in ids:
        if i is None:
            i = f"f-{entry_id}-{next_n:02d}"
            next_n += 1
        out.append(i)
    return out
