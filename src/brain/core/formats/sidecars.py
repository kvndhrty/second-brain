"""Generated sidecars (JSON) and small system objects.

Sidecars are written by the pipeline and can be regenerated with
`brain reextract`. Each records the model and prompt version that made it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from array import array
from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from brain.core.formats.decisions import OpenState, Status
from brain.core.formats.entry import Id
from brain.core.formats.yamlio import FORMAT_VERSION, FormatError, check_format


def bytes_hash(data: bytes) -> str:
    """Hash of a file as stored; used to tell whether a later step saw this exact version."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


FragmentKind = Literal["observation", "decision", "question", "idea", "claim", "action_item"]
OPEN_KINDS: frozenset[str] = frozenset({"question", "action_item"})


class Sidecar(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: int = FORMAT_VERSION

    @classmethod
    def parse(cls, data: bytes | str, *, where: str) -> Self:
        try:
            raw = json.loads(data)
        except json.JSONDecodeError as e:
            raise FormatError(f"{where}: invalid JSON: {e}") from e
        if not isinstance(raw, dict):
            raise FormatError(f"{where}: expected a JSON object")
        check_format(raw, where)
        try:
            return cls.model_validate(raw)
        except ValidationError as e:
            raise FormatError(f"{where}: {e}") from e

    def to_bytes(self) -> bytes:
        return (self.model_dump_json(indent=2) + "\n").encode()


class PromptStamp(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: int
    sha256: str


class MentionRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    surface: str
    type: Literal["person", "org"]


class ThemeRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    surface: str
    kind: Literal["domain", "technique"]


class NewThread(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""


class ThreadRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread: Id | None = None
    new: NewThread | None = None
    weight: float = Field(1.0, ge=0, le=1)

    @model_validator(mode="after")
    def _one(self) -> ThreadRef:
        if (self.thread is None) == (self.new is None):
            raise ValueError("thread reference needs exactly one of 'thread' or 'new'")
        return self

    @property
    def surface(self) -> str:
        return self.new.name if self.new else (self.thread or "")


class ExtractedFragment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: FragmentKind
    text: str
    span: tuple[int, int] | None = None
    quote: str = ""
    status: Status = "confirmed"
    someday: bool = False
    state: OpenState | None = None
    mentions: list[MentionRef] = Field(default_factory=list)
    themes: list[ThemeRef] = Field(default_factory=list)
    threads: list[ThreadRef] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _legacy_dream(cls, data: Any) -> Any:
        # Before 0.1.0 this flag was called `dream`; that word now means offline consolidation.
        if isinstance(data, dict) and "dream" in data and "someday" not in data:
            data = {("someday" if k == "dream" else k): v for k, v in data.items()}
        return data


class Extraction(Sidecar):
    entry_id: Id
    entry_hash: str
    model: str
    prompt: PromptStamp
    created_at: datetime
    fragments: list[ExtractedFragment] = Field(default_factory=list)


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: Id
    score: float


class Resolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    surface: str
    type: Literal["person", "org", "theme", "thread"]
    # link: matched an existing entity (or a theme/thread created as proposed)
    # new: looks like a new person/org; waits for review
    # pending: ambiguous between candidates; waits for review
    # ignore: not an entity
    decision: Literal["link", "new", "pending", "ignore"]
    entity_id: Id | None = None
    proposed: dict[str, str] | None = None
    confidence: float = Field(0.0, ge=0, le=1)
    candidates: list[Candidate] = Field(default_factory=list)


class AnswerProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fragment: str  # fragment in this entry
    answers: str  # earlier question or action-item fragment
    confidence: float = Field(0.0, ge=0, le=1)


class ResolutionFile(Sidecar):
    entry_id: Id
    extraction_hash: str  # content hash of the extraction it resolved
    model: str
    prompt: PromptStamp
    created_at: datetime
    mentions: list[Resolution] = Field(default_factory=list)
    answers: list[AnswerProposal] = Field(default_factory=list)


class EmbeddingItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str  # entry or fragment id
    hash: str  # content hash of the embedded text
    vec: str  # base64 of little-endian float32

    def vector(self) -> list[float]:
        a = array("f")
        a.frombytes(base64.b64decode(self.vec))
        if sys.byteorder != "little":
            a.byteswap()
        return a.tolist()


def encode_vector(values: list[float]) -> str:
    a = array("f", values)
    if sys.byteorder != "little":
        a.byteswap()
    return base64.b64encode(a.tobytes()).decode()


class EmbeddingFile(Sidecar):
    entry_id: Id
    model: str
    dim: int = Field(gt=0)
    dtype: Literal["float32-le"] = "float32-le"
    items: list[EmbeddingItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def _dims(self) -> EmbeddingFile:
        for item in self.items:
            if len(base64.b64decode(item.vec)) != self.dim * 4:
                raise ValueError(f"vector for {item.id} does not have {self.dim} dimensions")
        return self


class ThreadSummary(Sidecar):
    thread_id: Id
    model: str
    prompt: PromptStamp
    updated_at: datetime
    through_entry: str | None = None
    summary_md: str
    citations: list[str] = Field(default_factory=list)


class BriefState(Sidecar):
    last: dict[str, datetime] = Field(default_factory=dict)  # node id -> last brief time


class StoreFormat(Sidecar):
    created_at: datetime
    created_by: str


class Generation(Sidecar):
    token: str
    counter: int = 0
    host: str = ""
    at: datetime | None = None
