"""Keyword (FTS5) and vector (sqlite-vec) lookups. Milestone 2 combines them for `ask`."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from brain.core.index.db import vec_blob


@dataclass(frozen=True)
class Hit:
    item_id: str
    item_type: str
    score: float


def _fts_query(text: str) -> str:
    # Quote each word so punctuation in free text is never parsed as FTS syntax.
    words = re.findall(r"\w+", text, flags=re.UNICODE)
    return " OR ".join(f'"{w}"' for w in words)


def keyword(conn: sqlite3.Connection, text: str, *, limit: int = 20, types: tuple[str, ...] | None = None) -> list[Hit]:
    q = _fts_query(text)
    if not q:
        return []
    sql = "SELECT item_id, item_type, bm25(fts) AS score FROM fts WHERE fts MATCH ?"
    params: list[object] = [q]
    if types:
        sql += f" AND item_type IN ({','.join('?' * len(types))})"
        params += list(types)
    sql += " ORDER BY score LIMIT ?"
    params.append(limit)
    return [Hit(r[0], r[1], -r[2]) for r in conn.execute(sql, params)]


def nearest(conn: sqlite3.Connection, vector: list[float], *, k: int = 20) -> list[Hit]:
    rows = conn.execute(
        """
        SELECT e.owner_id, CASE WHEN e.owner_id LIKE 'e-%' THEN 'entry' ELSE 'fragment' END, v.distance
        FROM (SELECT rowid, distance FROM vec_items WHERE embedding MATCH ? AND k = ?) v
        JOIN embeddings e ON e.rowid = v.rowid
        ORDER BY v.distance
        """,
        (vec_blob(vector), k),
    ).fetchall()
    # Vectors are unit length, so cosine similarity = 1 - d^2 / 2.
    return [Hit(r[0], r[1], 1 - (r[2] ** 2) / 2) for r in rows]
