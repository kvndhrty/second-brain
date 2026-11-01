"""Typed access to the files in the store.

Every write takes the local write lock, makes sure this machine holds the
lease, writes the object, bumps `_system/generation.json`, and then tells the
index about the change so it does not have to re-list the store.
"""

from __future__ import annotations

import socket
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

from brain.core import keys
from brain.core.formats.decisions import Decisions
from brain.core.formats.entity import ENTITY_MODELS, Entity, parse_entity
from brain.core.formats.entry import Entry
from brain.core.formats.sidecars import Generation, StoreFormat
from brain.core.formats.yamlio import FORMAT_VERSION, FormatError, check_format
from brain.core.index.sync import read_generation
from brain.core.localstate import LocalState
from brain.core.storage.base import NotFound, ObjectInfo, PreconditionFailed, Storage
from brain.core.storage.lease import Lease

# (key, info, data or None for a delete, previous generation token, new token)
WriteHook = Callable[[str, ObjectInfo, bytes | None, str | None, str], None]


class Vault:
    def __init__(
        self,
        storage: Storage,
        lease: Lease,
        state: LocalState,
        *,
        on_write: WriteHook | None = None,
        prepare: Callable[[], object] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        self.storage = storage
        self.lease = lease
        self.state = state
        self.on_write = on_write
        self._prepare = prepare  # runs once before the first write (the conditional-write probe)
        self.clock = clock
        self._format_checked = False
        self._counter = 0

    # --- store format --------------------------------------------------------

    def store_format(self) -> StoreFormat | None:
        """Raises UnsupportedFormat if the store was written by a newer brain."""
        try:
            raw = self.storage.get(keys.FORMAT)
        except NotFound:
            return None
        fmt = StoreFormat.parse(raw, where=keys.FORMAT)
        check_format(fmt.model_dump(), keys.FORMAT)
        return fmt

    def _ensure_format_marker(self) -> None:
        if self._format_checked:
            return
        if self.store_format() is None:
            marker = StoreFormat(format=FORMAT_VERSION, created_at=datetime.now(UTC), created_by=socket.gethostname())
            try:
                self.storage.put(
                    keys.FORMAT, marker.to_bytes(), if_absent=bool(self.storage.supports_conditional_writes)
                )
            except PreconditionFailed:
                self.store_format()
        self._format_checked = True

    # --- low-level writes ----------------------------------------------------

    def _bump_generation(self) -> tuple[str | None, str]:
        prev = read_generation(self.storage)
        self._counter = (prev.counter if prev else 0) + 1
        new = Generation(token=uuid.uuid4().hex, counter=self._counter, host=socket.gethostname(), at=datetime.now(UTC))
        self.storage.put(keys.GENERATION, new.to_bytes())
        return (prev.token if prev else None), new.token

    def _before_write(self) -> None:
        if self._prepare is not None:
            self._prepare()
            self._prepare = None
        self.lease.ensure()

    def _write_locked(self, key: str, data: bytes, *, if_absent: bool = False) -> tuple[ObjectInfo, str | None, str]:
        """Write while holding the write lock. Returns (info, previous generation, new generation)."""
        self._before_write()
        self._ensure_format_marker()
        info = self.storage.put(key, data, if_absent=if_absent and bool(self.storage.supports_conditional_writes))
        prev, new = self._bump_generation()
        return info, prev, new

    def write(self, key: str, data: bytes, *, if_absent: bool = False) -> ObjectInfo:
        with self.state.write_lock():
            info, prev, new = self._write_locked(key, data, if_absent=if_absent)
        if self.on_write:
            self.on_write(key, info, data, prev, new)
        return info

    def delete(self, key: str) -> None:
        with self.state.write_lock():
            self._before_write()
            self.storage.delete(key)
            prev, new = self._bump_generation()
        if self.on_write:
            self.on_write(key, ObjectInfo(key, "", 0, datetime.now(UTC)), None, prev, new)

    # --- entries -------------------------------------------------------------

    def _entry_candidates(self, created: datetime, stem: str) -> list[str]:
        return [o.key for o in self.storage.list(keys.entry_dir(created)) if o.key.rsplit("/", 1)[-1].startswith(stem)]

    def save_entry(self, body: str, *, title: str | None = None, tags: list[str] | None = None) -> tuple[Entry, str]:
        if not body.strip():
            raise ValueError("entry is empty")
        now = self.clock().replace(microsecond=0)
        # Ids are per second. Choosing the id and writing the file happen under
        # one lock, so two processes on this machine cannot pick the same id.
        with self.state.write_lock():
            taken = set()
            for k in self._entry_candidates(now, keys.basic_timestamp(now)):
                try:
                    taken.add(Entry.parse(self.storage.get(k).decode(), where=k).id)
                except (FormatError, NotFound):
                    continue
            suffix = 0
            while (entry_id := keys.entry_id_for(now, suffix)) in taken:
                suffix = 2 if suffix == 0 else suffix + 1
            entry = Entry(id=entry_id, created=now, updated=now, title=title or None, tags=tags or [], body=body)
            key = keys.entry_key(now, entry_id, keys.slugify(title or body))
            data = entry.to_text().encode()
            info, prev, new = self._write_locked(key, data, if_absent=True)
        if self.on_write:
            self.on_write(key, info, data, prev, new)
        return entry, key

    def find_entry_key(self, entry_id: str) -> str | None:
        stem = entry_id.removeprefix("e-")
        try:
            ts = datetime.strptime(stem[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        except ValueError:
            ts = None
        candidates = self._entry_candidates(ts, stem) if ts else [o.key for o in self.storage.list(keys.ENTRIES)]
        for key in candidates:
            try:
                if Entry.parse(self.storage.get(key).decode(), where=key).id == entry_id:
                    return key
            except (FormatError, NotFound):
                continue
        return None

    def get_entry(self, entry_id: str, key: str | None = None) -> tuple[Entry, str]:
        key = key or self.find_entry_key(entry_id)
        if key is None:
            raise NotFound(f"entry {entry_id}")
        return Entry.parse(self.storage.get(key).decode(), where=key), key

    def update_entry(self, entry: Entry, key: str) -> Entry:
        entry.updated = self.clock().replace(microsecond=0)
        self.write(key, entry.to_text().encode())
        return entry

    def iter_entries(self) -> Iterator[tuple[Entry, str]]:
        for o in self.storage.list(keys.ENTRIES):
            if o.key.endswith(".md"):
                yield Entry.parse(self.storage.get(o.key).decode(), where=o.key), o.key

    # --- entities ------------------------------------------------------------

    def get_entity(self, entity_type: keys.EntityType, entity_id: str) -> Entity:
        key = keys.entity_key(entity_type, entity_id)
        return parse_entity(self.storage.get(key).decode(), where=key, default_type=entity_type)

    def allocate_entity_id(self, entity_type: keys.EntityType, name: str) -> str:
        base = keys.slugify(name, max_words=8, max_len=60, default=entity_type)
        candidate, n = base, 2
        while self.storage.exists(keys.entity_key(entity_type, candidate)):
            candidate, n = f"{base}-{n}", n + 1
        return candidate

    def create_entity(self, entity_type: keys.EntityType, name: str, build: Callable[[str], Entity]) -> Entity:
        """Pick a free id for a new entity and write it, under one lock so no other process takes the same id."""
        with self.state.write_lock():
            entity = build(self.allocate_entity_id(entity_type, name))
            key = keys.entity_key(entity_type, entity.id)
            data = entity.to_text().encode()
            info, prev, new = self._write_locked(key, data, if_absent=True)
        if self.on_write:
            self.on_write(key, info, data, prev, new)
        return entity

    def put_entity(self, entity: Entity) -> str:
        if entity.type not in ENTITY_MODELS:
            raise ValueError(f"unknown entity type {entity.type}")
        key = keys.entity_key(entity.type, entity.id)
        self.write(key, entity.to_text().encode())
        return key

    # --- decisions and sidecars ----------------------------------------------

    def get_decisions(self, entry_id: str) -> Decisions:
        key = keys.decisions_key(entry_id)
        try:
            return Decisions.parse(self.storage.get(key).decode(), where=key)
        except NotFound:
            return Decisions(entry=entry_id)

    def put_decisions(self, decisions: Decisions) -> None:
        self.write(keys.decisions_key(decisions.entry), decisions.to_text().encode())

    def put_bytes(self, key: str, data: bytes) -> None:
        self.write(key, data)
