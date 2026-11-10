"""Bring the index up to date with the store.

The store's `_system/generation.json` changes on every write made through
brain. If its token matches the one recorded in the index, listing the store
is skipped. Otherwise the store is listed, compared with the `objects`
manifest by ETag, and only the affected units are rebuilt. A full listing
also runs at least once a day, to pick up files written outside brain.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from brain.core import keys
from brain.core.formats.records import Generation
from brain.core.formats.yamlio import FormatError
from brain.core.index.build import UnitBuilder, UnitFile, UnitFiles, unit_of
from brain.core.index.db import IndexDB, get_meta, set_meta
from brain.core.storage.base import NotFound, ObjectInfo, Storage

FULL_LIST_INTERVAL = timedelta(hours=24)


@dataclass
class SyncReport:
    skipped: bool = False
    listed: int = 0
    changed: int = 0
    deleted: int = 0
    units: int = 0
    problems: list[tuple[str, str]] = field(default_factory=list)


def read_generation(storage: Storage) -> Generation | None:
    try:
        return Generation.parse(storage.get(keys.GENERATION), where=keys.GENERATION)
    except (NotFound, FormatError):
        return None


def _indexed(key: str) -> bool:
    return keys.classify(key) is not None


class Syncer:
    def __init__(
        self,
        db: IndexDB,
        storage: Storage,
        *,
        embed_model: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.db = db
        self.storage = storage
        self.builder = UnitBuilder(db, embed_model)
        self.clock = clock

    @property
    def conn(self) -> sqlite3.Connection:
        return self.db.conn

    def sync(self, *, full: bool = False) -> SyncReport:
        gen = read_generation(self.storage)
        if not full and self._up_to_date(gen):
            return SyncReport(skipped=True)
        listing = {o.key: o for o in self.storage.list("") if _indexed(o.key)}
        manifest = {r["key"]: (r["etag"], r["unit"]) for r in self.conn.execute("SELECT key, etag, unit FROM objects")}
        changed = [k for k, o in listing.items() if k not in manifest or manifest[k][0] != o.etag]
        deleted = [k for k in manifest if k not in listing]

        report = SyncReport(listed=len(listing), changed=len(changed), deleted=len(deleted))
        self.conn.execute("BEGIN IMMEDIATE")  # take the write lock now; another process may share this index
        try:
            cache: dict[str, bytes] = {}
            affected: set[str] = set()
            for k in deleted:
                if manifest[k][1]:
                    affected.add(manifest[k][1])
                self.conn.execute("DELETE FROM objects WHERE key = ?", (k,))
                self.conn.execute("DELETE FROM problems WHERE key = ?", (k,))
            for k in changed:
                old_unit = manifest.get(k, (None, None))[1]
                if old_unit:
                    affected.add(old_unit)
                unit = self._unit_for(k, listing[k], cache)
                if unit:
                    affected.add(unit)
            for unit in sorted(affected):
                self._rebuild(unit, cache)
            if gen:
                set_meta(self.conn, "generation", gen.token)
            set_meta(self.conn, "last_full_sync", self.clock().isoformat())
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        report.units = len(affected)
        report.problems = [(r[0], r[1]) for r in self.conn.execute("SELECT key, message FROM problems ORDER BY key")]
        return report

    def _up_to_date(self, gen: Generation | None) -> bool:
        if gen is None or get_meta(self.conn, "generation") != gen.token:
            return False
        last = get_meta(self.conn, "last_full_sync")
        return bool(last) and self.clock() - datetime.fromisoformat(last) < FULL_LIST_INTERVAL

    def _unit_for(self, key: str, info: ObjectInfo, cache: dict[str, bytes]) -> str | None:
        """Record the key in the manifest and return its unit (None if ignored or unparseable)."""
        self.conn.execute("DELETE FROM problems WHERE key = ?", (key,))
        kinfo = keys.classify(key)
        unit: str | None = None
        if kinfo is not None:
            if kinfo.unit:
                unit = kinfo.unit
            else:
                data = cache[key] if key in cache else self.storage.get(key)
                cache[key] = data
                try:
                    unit = unit_of(key, data)
                except (FormatError, UnicodeDecodeError) as e:
                    self.conn.execute("INSERT INTO problems(key, message) VALUES (?, ?)", (key, str(e)))
        self.conn.execute(
            "INSERT INTO objects(key, etag, size, mtime, unit) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET etag = excluded.etag, size = excluded.size, "
            "mtime = excluded.mtime, unit = excluded.unit",
            (key, info.etag, info.size, info.mtime.isoformat(), unit),
        )
        return unit

    def _rebuild(self, unit: str, cache: dict[str, bytes]) -> None:
        self.builder.clear(unit)
        files = UnitFiles(unit)
        for (key,) in self.conn.execute("SELECT key FROM objects WHERE unit = ? ORDER BY key", (unit,)).fetchall():
            kinfo = keys.classify(key)
            if kinfo is None:
                continue
            if key not in cache:
                try:
                    cache[key] = self.storage.get(key)
                except NotFound:
                    continue
            files.by_role.setdefault(kinfo.role, []).append(UnitFile(key, cache[key]))
        self.builder.build(files)

    def apply_write(
        self, key: str, info: ObjectInfo, data: bytes | None, *, prev_token: str | None, new_token: str
    ) -> None:
        """Update the index for a write this process just made (data=None for a delete).

        The recorded generation only advances if the index was in sync with the
        store before the write; otherwise the next sync does a full comparison.
        """
        if not _indexed(key):
            return
        self.conn.execute("BEGIN IMMEDIATE")  # take the write lock now; another process may share this index
        try:
            row = self.conn.execute("SELECT unit FROM objects WHERE key = ?", (key,)).fetchone()
            affected = {row[0]} if row and row[0] else set()
            cache: dict[str, bytes] = {}
            if data is None:
                self.conn.execute("DELETE FROM objects WHERE key = ?", (key,))
                self.conn.execute("DELETE FROM problems WHERE key = ?", (key,))
            else:
                cache[key] = data
                unit = self._unit_for(key, info, cache)
                if unit:
                    affected.add(unit)
            for unit in sorted(affected):
                self._rebuild(unit, cache)
            if prev_token is not None and get_meta(self.conn, "generation") == prev_token:
                set_meta(self.conn, "generation", new_token)
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
