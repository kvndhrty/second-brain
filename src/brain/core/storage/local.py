"""Local filesystem storage."""

from __future__ import annotations

import fcntl
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from brain.core.storage.base import NotFound, ObjectInfo, PreconditionFailed, StorageError, check_key, valid_key

_LOCK_NAME = ".brain-cas.lock"


class LocalStorage:
    supports_conditional_writes: bool | None = True

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()

    @property
    def ident(self) -> str:
        return f"local:{self.root}"

    def describe(self) -> str:
        return str(self.root)

    def _path(self, key: str) -> Path:
        return self.root / check_key(key)

    def _info(self, key: str, path: Path) -> ObjectInfo:
        st = path.stat()
        return ObjectInfo(
            key=key,
            etag=f"{st.st_mtime_ns:x}-{st.st_size:x}",
            size=st.st_size,
            mtime=datetime.fromtimestamp(st.st_mtime, UTC),
        )

    def get(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError:
            raise NotFound(key) from None
        except OSError as e:
            raise StorageError(f"cannot read {key}: {e}") from e

    def put(
        self,
        key: str,
        data: bytes,
        *,
        if_absent: bool = False,
        if_match: str | None = None,
    ) -> ObjectInfo:
        path = self._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.parent / f".{path.name}.tmp-{secrets.token_hex(4)}"
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            try:
                if if_absent:
                    # link() fails if the target exists, and the target appears
                    # with its full content, so this is an atomic create-if-absent.
                    try:
                        os.link(tmp, path)
                    except FileExistsError:
                        raise PreconditionFailed(f"{key} already exists") from None
                elif if_match is not None:
                    with self._cas_lock():
                        current = self._info(key, path).etag if path.exists() else None
                        if current != if_match:
                            raise PreconditionFailed(f"{key} changed (etag {current} != {if_match})")
                        os.replace(tmp, path)
                else:
                    os.replace(tmp, path)
            finally:
                tmp.unlink(missing_ok=True)
            return self._info(key, path)
        except (PreconditionFailed, ValueError):
            raise
        except OSError as e:
            raise StorageError(f"cannot write {key} under {self.root}: {e}") from e

    @contextmanager
    def _cas_lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.root / _LOCK_NAME, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def list(self, prefix: str = "") -> Iterator[ObjectInfo]:
        # Walk only the directory that the prefix implies, then filter by prefix.
        base_dir = prefix.rsplit("/", 1)[0] if "/" in prefix else ""
        start = self.root / base_dir if base_dir else self.root
        if not start.is_dir():
            return
        found = []
        for dirpath, dirnames, filenames in os.walk(start):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if name.startswith("."):
                    continue
                full = Path(dirpath) / name
                key = full.relative_to(self.root).as_posix()
                if key.startswith(prefix) and valid_key(key):
                    found.append((key, full))
        for key, full in sorted(found):
            try:
                yield self._info(key, full)
            except FileNotFoundError:
                continue

    def delete(self, key: str) -> None:
        try:
            self._path(key).unlink(missing_ok=True)
        except OSError as e:
            raise StorageError(f"cannot delete {key}: {e}") from e

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()
