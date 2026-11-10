"""What every layer needs: the store, the index, and the models.

`Calls` is how a layer talks to a model. It records which prompts ran, and it
applies the rule that a failed call never blocks a note: when the model's
output fails validation after its retries, the call is tried on the strong
model if `llm.escalate` allows, and otherwise the layer's safe result is used
and the fallback is written to the change log.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TypeVar

from pydantic import BaseModel

from brain.core.config import Settings
from brain.core.formats.records import Event, PromptStamp, Record
from brain.core.index.db import IndexDB
from brain.core.index.queries import World
from brain.core.llm.models import LLM, LLMOutputError
from brain.core.llm.prompts import load_prompt
from brain.core.vault import Vault

R = TypeVar("R", bound=Record)
T = TypeVar("T", bound=BaseModel)


@dataclass
class PipelineContext:
    settings: Settings
    vault: Vault
    db: IndexDB
    llm: LLM
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    @property
    def conn(self):
        return self.db.conn

    def world(self) -> World:
        return World(self.db.conn)

    def read(self, key: str, model: type[R]) -> tuple[R, bytes] | None:
        return self.vault.read_record(key, model)

    def write(self, key: str, record: Record) -> bytes:
        return self.vault.put_record(key, record)


@dataclass
class Calls:
    """The model calls of one layer run (or one dream pass)."""

    llm: LLM
    budget: int | None = None  # None: unlimited
    strong: bool = False  # run every call on the strong model (dreaming, when configured)
    count: int = 0
    model: str = ""
    events: list[Event] = field(default_factory=list)

    @property
    def exhausted(self) -> bool:
        return self.budget is not None and self.count >= self.budget

    def ask(
        self,
        prompt: str,
        user: str,
        output_type: type[T],
        *,
        fallback: T | None,
        validate: Callable[[T], None] | None = None,
    ) -> T:
        """One model call. Returns `fallback` if the output never validates; raises if fallback is None."""
        self.count += 1
        try:
            got = self.llm.run(prompt, user, output_type, validate=validate, strong=self.strong)
        except LLMOutputError:
            got = None
            if self.llm.can_escalate and not self.strong:
                self.count += 1
                try:
                    got = self.llm.run(prompt, user, output_type, validate=validate, strong=True)
                except LLMOutputError:
                    got = None
            if got is None:
                if fallback is None:
                    raise
                self.events.append(Event(kind="fallback", text=f"{prompt}: no valid answer from the model; left as is"))
                return fallback
        self.model = got.model
        return got.output

    def log(self, kind: str, text: str, entity: str | None = None) -> None:
        self.events.append(Event(kind=kind, text=text, entity=entity))


def stamps(prompts: tuple[str, ...]) -> list[PromptStamp]:
    return [load_prompt(p).stamp for p in prompts]
