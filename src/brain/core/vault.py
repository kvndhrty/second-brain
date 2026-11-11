"""Typed access to the files in the store.

Every write takes the local write lock, makes sure this machine holds the
lease, writes the object, bumps `_system/generation.json`, and then tells the
index about the change so it does not have to re-list the store.
"""

from __future__ import annotations

import socket
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TypeVar

from brain.core import keys
from brain.core.formats.note import Note
from brain.core.formats.records import EntityRecord, Generation, Record, StoreFormat
from brain.core.formats.yamlio import FORMAT_VERSION, FormatError, check_format
from brain.core.index.sync import read_generation
from brain.core.localstate import LocalState
from brain.core.storage.base import NotFound, ObjectInfo, PreconditionFailed, Storage, StorageError
from brain.core.storage.lease import Lease

# (key, info, data or None for a delete, previous generation token, new token)
WriteHook = Callable[[str, ObjectInfo, bytes | None, str | None, str], None]


R = TypeVar("R", bound=Record)

# A file put into notes/ by hand is taken once it has not changed for this long, so a file an
# editor is still saving is not taken half-written.
SETTLE = timedelta(seconds=30)


@dataclass
class Adopted:
    notes: list[Note] = field(default_factory=list)  # bare files that were turned into notes
    in_place: list[str] = field(default_factory=list)  # keys of complete notes at paths of the user's choosing


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

    # --- notes ---------------------------------------------------------------

    def _note_candidates(self, created: datetime, stem: str) -> list[str]:
        return [o.key for o in self.storage.list(keys.note_dir(created)) if o.key.rsplit("/", 1)[-1].startswith(stem)]

    def save_note(
        self, body: str, *, title: str | None = None, about: str | None = None, created: datetime | None = None
    ) -> tuple[Note, str]:
        if not body.strip():
            raise ValueError("note is empty")
        now = (created or self.clock()).replace(microsecond=0)
        # Ids are per second. Choosing the id and writing the file happen under
        # one lock, so two processes on this machine cannot pick the same id.
        with self.state.write_lock():
            taken = set()
            for k in self._note_candidates(now, keys.basic_timestamp(now)):
                try:
                    taken.add(Note.parse(self.storage.get(k).decode(), where=k).id)
                except (FormatError, NotFound, UnicodeDecodeError):
                    continue
            suffix = 0
            while (note_id := keys.note_id_for(now, suffix)) in taken:
                suffix = 2 if suffix == 0 else suffix + 1
            note = Note(id=note_id, created=now, title=title or None, about=about or None, body=body)
            key = keys.note_key(now, note_id, keys.slugify(title or body))
            data = note.to_text().encode()
            self._before_write()
            self._ensure_format_marker()
            info = self.storage.put(key, data, if_absent=bool(self.storage.supports_conditional_writes))
            # The note is in the store. Nothing after this may report the save as failed: the
            # caller would save the text again. A missed generation bump or index update is
            # repaired by the next full listing.
            try:
                prev, new = self._bump_generation()
            except StorageError:
                return note, key
        if self.on_write:
            try:
                self.on_write(key, info, data, prev, new)
            except Exception:  # noqa: BLE001 - the index is rebuilt from the store
                pass
        return note, key

    def adopt_bare_notes(self, *, now: datetime | None = None) -> Adopted:
        """Turn markdown files saved into notes/ by hand into notes.

        A file without frontmatter gets an id and a creation time (its
        modification time). The bare file is removed once the note is written.
        A file that changed in the last `SETTLE` seconds is left for the next
        pass, and so is one that cannot be read as text.
        """
        out = Adopted()
        newest = (now or datetime.now(UTC)) - SETTLE
        for o in list(self.storage.list(keys.NOTES)):
            if not keys.is_bare_note(o.key):
                continue  # judged by its path, so brain's own notes are not read again on every pass
            if o.mtime > newest:
                continue
            try:
                text = self.storage.get(o.key).decode()
            except (NotFound, UnicodeDecodeError):
                continue
            if not text.strip():
                continue
            try:
                Note.parse(text, where=o.key)
                out.in_place.append(o.key)  # a complete note at a path of the user's choosing
                continue
            except FormatError:
                pass  # no frontmatter, or frontmatter from another tool: adopt it as part of the text
            stem = o.key.rsplit("/", 1)[-1][:-3]
            title = stem.replace("-", " ").replace("_", " ").strip() or None
            note, _ = self.save_note(text, title=title, created=o.mtime)
            self.delete(o.key)
            out.notes.append(note)
        return out

    def find_note_key(self, note_id: str) -> str | None:
        stem = note_id.removeprefix("n-")
        try:
            ts = datetime.strptime(stem[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        except ValueError:
            ts = None
        candidates = self._note_candidates(ts, stem) if ts else [o.key for o in self.storage.list(keys.NOTES)]
        for key in candidates:
            try:
                if Note.parse(self.storage.get(key).decode(), where=key).id == note_id:
                    return key
            except (FormatError, NotFound):
                continue
        return None

    def get_note(self, note_id: str, key: str | None = None) -> tuple[Note, str]:
        key = key or self.find_note_key(note_id)
        if key is None:
            raise NotFound(f"note {note_id}")
        return Note.parse(self.storage.get(key).decode(), where=key), key

    # --- entities ------------------------------------------------------------

    def get_entity(self, entity_id: str) -> EntityRecord:
        key = keys.entity_key(entity_id)
        return EntityRecord.parse(self.storage.get(key), where=key)

    def create_entity(self, name: str, build: Callable[[str], EntityRecord]) -> EntityRecord:
        """Pick a free id for a new entity and write it, under one lock so no other process takes the same id."""
        with self.state.write_lock():
            base = keys.slugify(name, max_words=8, max_len=60, default="entity")
            entity_id, n = base, 2
            while self.storage.exists(keys.entity_key(entity_id)):
                entity_id, n = f"{base}-{n}", n + 1
            entity = build(entity_id)
            key = keys.entity_key(entity.id)
            data = entity.to_bytes()
            info, prev, new = self._write_locked(key, data, if_absent=True)
        if self.on_write:
            self.on_write(key, info, data, prev, new)
        return entity

    def put_entity(self, entity: EntityRecord) -> None:
        self.write(keys.entity_key(entity.id), entity.to_bytes())

    # --- generated files -----------------------------------------------------

    def put_record(self, key: str, record: Record) -> bytes:
        data = record.to_bytes()
        self.write(key, data)
        return data

    def put_text(self, key: str, text: str) -> bool:
        """Write a text file unless it already has exactly this content. True if written."""
        data = text.encode()
        try:
            if self.storage.get(key) == data:
                return False
        except NotFound:
            pass
        self.write(key, data)
        return True

    def read_record(self, key: str, model: type[R]) -> tuple[R, bytes] | None:
        """The parsed record and its exact bytes, or None if missing or unreadable."""
        try:
            data = self.storage.get(key)
        except NotFound:
            return None
        try:
            return model.parse(data, where=key), data
        except FormatError:
            return None  # regenerate rather than fail; `brain doctor` reports the bad file
