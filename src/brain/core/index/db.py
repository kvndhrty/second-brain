"""SQLite connection, schema creation, and capability checks.

The index is derived, so there are no migrations: if the schema version or
the embedding model recorded in the database differs from what this code
expects, the database is deleted and rebuilt from the store.
"""

from __future__ import annotations

import math
import sqlite3
from array import array
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "7"
MIN_SQLITE = (3, 41, 0)


class IndexError_(Exception):
    """The index cannot be opened. The message says why and what to do."""


@dataclass(frozen=True)
class Capabilities:
    sqlite_version: str
    sqlite_ok: bool
    fts5: bool
    load_extension: bool
    vec_version: str | None
    vec_error: str | None


def capabilities() -> Capabilities:
    conn = sqlite3.connect(":memory:")
    try:
        try:
            conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
            fts5 = True
        except sqlite3.OperationalError:
            fts5 = False
        load_ext = hasattr(conn, "enable_load_extension")
        vec_version = vec_error = None
        try:
            _load_vec(conn)
            vec_version = conn.execute("SELECT vec_version()").fetchone()[0]
        except Exception as e:  # noqa: BLE001 - reported to the user verbatim
            vec_error = str(e)
        return Capabilities(
            sqlite_version=sqlite3.sqlite_version,
            sqlite_ok=sqlite3.sqlite_version_info >= MIN_SQLITE,
            fts5=fts5,
            load_extension=load_ext,
            vec_version=vec_version,
            vec_error=vec_error,
        )
    finally:
        conn.close()


def _load_vec(conn: sqlite3.Connection) -> None:
    if not hasattr(conn, "enable_load_extension"):
        raise IndexError_(
            "this Python's sqlite3 module cannot load extensions, which sqlite-vec needs. "
            "Install brain with a uv-managed Python: uv tool install --managed-python --python 3.12 ."
        )
    import sqlite_vec

    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _load_vec(conn)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _remove(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)


class IndexDB:
    def __init__(self, path: Path, *, embed_model: str) -> None:
        self.path = path
        self.embed_model = embed_model
        self.rebuilt_reason: str | None = None
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = self._open()

    def _open(self) -> sqlite3.Connection:
        if self.path.exists():
            conn = _connect(self.path)
            reason = self._mismatch(conn)
            if reason is None:
                return conn
            conn.close()
            _remove(self.path)
            self.rebuilt_reason = reason
        conn = _connect(self.path)
        script = resources.files("brain.core.index").joinpath("schema.sql").read_text()
        conn.executescript(f"BEGIN;\n{script}\nCOMMIT;")
        set_meta(conn, "schema_version", SCHEMA_VERSION)
        set_meta(conn, "embed_model", self.embed_model)
        return conn

    def _mismatch(self, conn: sqlite3.Connection) -> str | None:
        try:
            version = get_meta(conn, "schema_version")
            model = get_meta(conn, "embed_model")
        except sqlite3.DatabaseError as e:
            return f"index unreadable ({e})"
        if version != SCHEMA_VERSION:
            return f"index schema {version} != {SCHEMA_VERSION}"
        if model != self.embed_model:
            return f"embedding model changed ({model!r} -> {self.embed_model!r})"
        return None

    def close(self) -> None:
        self.conn.close()

    def delete(self) -> None:
        self.conn.close()
        _remove(self.path)

    # --- vectors -----------------------------------------------------------

    def ensure_vec(self, dim: int) -> bool:
        """Create vec_items for this dimension. False if a different one exists."""
        current = get_meta(self.conn, "embed_dim")
        if current is None:
            self.conn.execute(f"CREATE VIRTUAL TABLE vec_items USING vec0(embedding float[{int(dim)}])")
            set_meta(self.conn, "embed_dim", str(dim))
            return True
        return int(current) == dim

    def has_vec(self) -> bool:
        return get_meta(self.conn, "embed_dim") is not None


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def vec_blob(values: list[float]) -> bytes:
    """L2-normalize and pack as float32, so L2 distance ranks like cosine."""
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return array("f", (v / norm for v in values)).tobytes()


# Tables compared by the reindex equivalence check. Columns that depend on
# insertion order (rowids) are replaced by stable keys.
SNAPSHOT_QUERIES = {
    "objects": "SELECT key, etag, size, unit FROM objects",
    "problems": "SELECT key, message FROM problems",
    "notes": "SELECT * FROM notes",
    "statements": "SELECT * FROM statements",
    "entities": "SELECT * FROM entities",
    "names": "SELECT * FROM names",
    "mentions": "SELECT * FROM mentions",
    "relations": "SELECT * FROM relations",
    "operations": "SELECT note_id, statement_id, op, targets, name, moved, created_at, owner FROM operations",
    "events": "SELECT at, source, seq, kind, text, entity_id, owner FROM events",
    "pages": "SELECT * FROM pages",
    "embeddings": "SELECT owner_id, model, content_hash, owner FROM embeddings",
    "fts": "SELECT item_id, item_type, owner, title, body FROM fts",
}


def snapshot(db: IndexDB) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, sql in SNAPSHOT_QUERIES.items():
        rows = [tuple(r) for r in db.conn.execute(sql).fetchall()]
        out[name] = sorted(rows, key=repr)
    if db.has_vec():
        rows = db.conn.execute(
            "SELECT e.owner_id, v.embedding FROM embeddings e JOIN vec_items v ON v.rowid = e.rowid"
        ).fetchall()
        out["vectors"] = sorted((r[0], bytes(r[1])) for r in rows)
    out["meta"] = {k: get_meta(db.conn, k) for k in ("schema_version", "embed_model", "embed_dim")}
    return out
