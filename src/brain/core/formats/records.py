"""Generated files (JSON): one per layer per note, entity records, pages, dream runs.

Everything here is written by the pipeline or by dreaming and can be rebuilt
from the notes with `brain rebuild`. Each layer file records the model, the
prompt version, and a hash of its input, so a layer is skipped when nothing it
depends on has changed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from array import array
from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from brain.core.formats.note import Id
from brain.core.formats.yamlio import FORMAT_VERSION, FormatError, check_format

Kind = Literal["fact", "decision", "question", "action", "idea", "correction"]
OPEN_KINDS: frozenset[str] = frozenset({"question", "action"})
EntityType = Literal["person", "org", "topic", "thread"]
ENTITY_TYPES: tuple[EntityType, ...] = ("thread", "person", "org", "topic")
NameType = Literal["person", "org", "topic"]
Status = Literal["provisional", "active", "closed"]
Rel = Literal["answers", "supersedes", "repeats"]
Op = Literal["same_as", "not_same_as", "rename", "close", "reopen", "retract", "not_part_of"]
LayerName = Literal["split", "classify", "names", "links", "filing", "state"]


def bytes_hash(*parts: bytes | str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode() if isinstance(p, str) else p)
        h.update(b"\0")
    return "sha256:" + h.hexdigest()


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Record(_Strict):
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


class PromptStamp(_Strict):
    name: str
    version: int
    sha256: str


class Event(_Strict):
    """One line of the change log: something the pipeline or dreaming did."""

    kind: str  # created|linked|merged|split|renamed|closed|reopened|retracted|superseded|answered|filed|moved|fallback|profile
    text: str
    entity: str | None = None


# --- per-note layer files --------------------------------------------------------


class LayerFile(Record):
    note_id: Id
    input_hash: str
    model: str = ""
    prompts: list[PromptStamp] = Field(default_factory=list)
    created_at: datetime
    events: list[Event] = Field(default_factory=list)


class Statement(_Strict):
    id: str
    text: str
    quote: str = ""


class SplitFile(LayerFile):
    statements: list[Statement] = Field(default_factory=list)


class ClassifyFile(LayerFile):
    kinds: dict[str, Kind] = Field(default_factory=dict)


class NameRef(_Strict):
    surface: str
    type: NameType


class NamesFile(LayerFile):
    names: list[NameRef] = Field(default_factory=list)


class Link(_Strict):
    surface: str
    type: NameType
    entity: Id
    how: Literal["exact", "model", "new"]


class LinksFile(LayerFile):
    links: list[Link] = Field(default_factory=list)


class FilingFile(LayerFile):
    threads: dict[str, Id | None] = Field(default_factory=dict)


class Relation(_Strict):
    src: str  # the later statement
    dst: str  # the earlier statement it answers, supersedes, or repeats
    rel: Rel


class Operation(_Strict):
    op: Op
    statement: str  # the correction statement it came from
    targets: list[str] = Field(default_factory=list)  # entity ids, or statement ids for retract
    name: str | None = None  # rename: the new name
    moved: list[str] = Field(default_factory=list)  # not_part_of: statements moved to targets[0]


class StateFile(LayerFile):
    relations: list[Relation] = Field(default_factory=list)
    operations: list[Operation] = Field(default_factory=list)


LAYER_MODELS: dict[str, type[LayerFile]] = {
    "split": SplitFile,
    "classify": ClassifyFile,
    "names": NamesFile,
    "links": LinksFile,
    "filing": FilingFile,
    "state": StateFile,
}


# --- entities and pages ----------------------------------------------------------


class EntityRecord(Record):
    """Identity of a person, org, topic, or thread. The id is assigned once and kept across rebuilds."""

    id: Id
    type: EntityType
    name: str
    names: list[str] = Field(default_factory=list)  # other names it has been called
    status: Status = "provisional"
    merged_into: Id | None = None
    not_same_as: list[Id] = Field(default_factory=list)
    created_at: datetime
    created_from: str | None = None  # note id
    first_name: str | None = None  # the name it was created with, once a note has renamed it
    closed_at: datetime | None = None
    # Which fields a note set ("note:<id>") rather than a model. A note always wins.
    set_by: dict[str, str] = Field(default_factory=dict)
    profile: dict[str, str] = Field(default_factory=dict)  # role, org (people); filled by dreaming

    def all_names(self) -> list[str]:
        return [self.name, *[n for n in self.names if n != self.name]]

    def add_name(self, name: str) -> bool:
        name = " ".join(name.split())
        if not name or norm_name(name) in {norm_name(n) for n in self.all_names()}:
            return False
        self.names.append(name)
        return True

    def note_set(self, field: str) -> bool:
        return self.set_by.get(field, "").startswith("note:")

    def reset(self) -> EntityRecord:
        """The record as it was when created: same id, nothing learned since. Used by `brain rebuild`."""
        name = self.first_name or self.name
        full_name = self.type == "person" and len(name.split()) >= 2
        return EntityRecord(
            id=self.id,
            type=self.type,
            name=name,
            status="active" if full_name else "provisional",
            created_at=self.created_at,
            created_from=self.created_from,
        )


def norm_name(name: str) -> str:
    return " ".join(name.casefold().split())


class Sentence(_Strict):
    text: str
    citations: list[str] = Field(default_factory=list)  # note ids


class Section(_Strict):
    sentences: list[Sentence] = Field(default_factory=list)
    input_hash: str = ""
    through: str | None = None  # newest note the section covers
    updates: int = 0  # incremental updates since the last full rebuild
    rebuilt_at: datetime | None = None
    model: str = ""
    prompt: PromptStamp | None = None

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.sentences)


class PageFile(Record):
    entity_id: Id
    updated_at: datetime
    sections: dict[str, Section] = Field(default_factory=dict)  # "now", "summary"


# --- embeddings ------------------------------------------------------------------


class EmbeddingItem(_Strict):
    id: str  # note or statement id
    hash: str  # content hash of the embedded text
    vec: str  # base64 of little-endian float32


def encode_vector(values: list[float]) -> str:
    a = array("f", values)
    if sys.byteorder != "little":
        a.byteswap()
    return base64.b64encode(a.tobytes()).decode()


def decode_vector(text: str) -> list[float]:
    a = array("f")
    a.frombytes(base64.b64decode(text))
    if sys.byteorder != "little":
        a.byteswap()
    return list(a)


class EmbeddingFile(Record):
    note_id: Id
    model: str
    dim: int = Field(gt=0)
    dtype: Literal["float32-le"] = "float32-le"
    items: list[EmbeddingItem] = Field(default_factory=list)


# --- dreaming --------------------------------------------------------------------


class DreamRun(Record):
    run_id: str
    started_at: datetime
    finished_at: datetime | None = None
    model: str = ""
    calls: int = 0
    budget_exhausted: bool = False
    relations: list[Relation] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class DreamState(Record):
    last_run: str | None = None
    last_run_at: datetime | None = None
    notes_at_last_run: int = 0
    checked_pairs: dict[str, str] = Field(default_factory=dict)  # "a|b" -> hash of what was compared
    checked_items: dict[str, str] = Field(default_factory=dict)  # statement id -> newest note looked at
    profiles: dict[str, str] = Field(default_factory=dict)  # entity id -> hash of the statements used


# --- system ----------------------------------------------------------------------


class StoreFormat(Record):
    created_at: datetime
    created_by: str


class Generation(Record):
    token: str
    counter: int = 0
    host: str = ""
    at: datetime | None = None


class BriefState(Record):
    last: dict[str, datetime] = Field(default_factory=dict)  # entity id -> when it was last briefed
