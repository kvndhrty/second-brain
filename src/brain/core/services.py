"""The Brain façade: the only API the CLI, web, and MCP front-ends use."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from brain.core import paths
from brain.core.config import LoadedSettings, Settings
from brain.core.formats.entry import Entry
from brain.core.index.db import IndexDB
from brain.core.index.sync import Syncer, SyncReport
from brain.core.localstate import LocalState
from brain.core.storage import Storage, open_storage
from brain.core.storage.base import ObjectInfo
from brain.core.storage.lease import Lease
from brain.core.storage.probe import ensure_probed
from brain.core.vault import Vault


class JotFailed(Exception):
    def __init__(self, draft: Path, cause: Exception) -> None:
        self.draft = draft
        self.cause = cause
        super().__init__(f"{cause}\nYour text is saved in {draft}; run `brain jot --file {draft}` to retry.")


@dataclass(frozen=True)
class JotResult:
    entry: Entry
    key: str


def index_path(settings: Settings, storage: Storage) -> Path:
    if settings.index.path:
        p = Path(settings.index.path).expanduser()
        return p / "index.db" if p.is_dir() or settings.index.path.endswith("/") else p
    store_hash = hashlib.sha256(storage.ident.encode()).hexdigest()[:12]
    return paths.cache_dir() / store_hash / "index.db"


class Brain:
    def __init__(
        self,
        loaded: LoadedSettings,
        *,
        command: str = "brain",
        storage: Storage | None = None,
        state: LocalState | None = None,
        force_lease: bool = False,
    ) -> None:
        self.loaded = loaded
        self.settings = loaded.settings
        self.storage = storage or open_storage(self.settings)
        self.state = state or LocalState()
        self.lease = Lease(
            self.storage,
            machine_id=self.state.machine_id,
            ttl_seconds=self.settings.lease.ttl_seconds,
            command=command,
            force=force_lease,
        )
        self.db = IndexDB(index_path(self.settings, self.storage), embed_model=self.settings.llm.embed_model)
        self.syncer = Syncer(self.db, self.storage, embed_model=self.settings.llm.embed_model)
        self.vault = Vault(
            self.storage,
            self.lease,
            self.state,
            on_write=self._on_write,
            prepare=lambda: ensure_probed(self.storage, self.state),
        )

    def __enter__(self) -> Brain:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.lease.release()
        finally:
            self.db.close()

    def _on_write(self, key: str, info: ObjectInfo, data: bytes | None, prev: str | None, new: str) -> None:
        self.syncer.apply_write(key, info, data, prev_token=prev, new_token=new)

    # --- index ---------------------------------------------------------------

    def sync(self, *, full: bool = False) -> SyncReport:
        return self.syncer.sync(full=full)

    def reindex(self) -> SyncReport:
        """Delete the local index and rebuild it from the store. Calls no models."""
        self.db.delete()
        self.db = IndexDB(self.db.path, embed_model=self.settings.llm.embed_model)
        self.syncer = Syncer(self.db, self.storage, embed_model=self.settings.llm.embed_model)
        return self.syncer.sync(full=True)

    # --- entries -------------------------------------------------------------

    def jot(self, text: str, *, title: str | None = None, tags: list[str] | None = None) -> JotResult:
        """Save an entry. The text is kept in a local draft until the store confirms the write."""
        if not text.strip():
            raise ValueError("entry is empty")
        draft = self.state.drafts_dir / f"draft-{secrets.token_hex(4)}.md"
        draft.write_text(text, encoding="utf-8")
        try:
            entry, key = self.vault.save_entry(text, title=title, tags=tags)
        except Exception as e:
            raise JotFailed(draft, e) from e
        draft.unlink(missing_ok=True)
        return JotResult(entry, key)

    def pending_drafts(self) -> list[Path]:
        return sorted(self.state.drafts_dir.glob("draft-*.md"))
