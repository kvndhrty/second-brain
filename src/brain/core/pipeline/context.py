"""What every pipeline step needs: the store, the index, and the models."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TypeVar

from brain.core.config import Settings
from brain.core.formats.sidecars import Sidecar
from brain.core.index.db import IndexDB
from brain.core.llm.models import LLM
from brain.core.vault import Vault

S = TypeVar("S", bound=Sidecar)


@dataclass
class PipelineContext:
    settings: Settings
    vault: Vault
    db: IndexDB
    llm: LLM
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    # Embeddings of entity names, computed on demand during resolution.
    name_vectors: dict[str, list[float]] = field(default_factory=dict)

    @property
    def conn(self):
        return self.db.conn

    def read_sidecar(self, key: str, model: type[S]) -> tuple[S, bytes] | None:
        return self.vault.read_sidecar(key, model)

    def write_sidecar(self, key: str, sidecar: Sidecar) -> bytes:
        data = sidecar.to_bytes()
        self.vault.put_bytes(key, data)
        return data
