"""Decisions file: your changes to one entry's extracted data.

The index takes the LLM's extraction and applies this file on top of it, so
your changes survive `brain reextract`. Unknown keys are rejected, so a typo
shows up in `brain doctor` instead of being silently ignored.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, model_validator
from ruamel.yaml.comments import CommentedMap

from brain.core.formats.entity import ObeInfo
from brain.core.formats.entry import Id
from brain.core.formats.yamlio import FORMAT_VERSION, FormatError, check_format, dump_yaml, load_yaml, merge_into

Status = Literal["confirmed", "speculative", "overcome_by_events"]
OpenState = Literal["open", "closed"]
MentionType = Literal["person", "org", "theme"]


class ThreadChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    add: list[Id] = Field(default_factory=list)
    remove: list[Id] = Field(default_factory=list)


class FragmentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Status | None = None
    obe: ObeInfo | None = None
    someday: bool | None = None
    state: OpenState | None = None
    closed_note: str | None = None
    threads: ThreadChange | None = None
    answers: list[str] | None = None  # question/action fragments this one answers (accepted)
    not_answers: list[str] | None = None  # proposed answer links you rejected
    deleted: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def _legacy_dream(cls, data: Any) -> Any:
        # Before 0.1.0 this flag was called `dream`; that word now means offline consolidation.
        if isinstance(data, dict) and "dream" in data and "someday" not in data:
            data = {("someday" if k == "dream" else k): v for k, v in data.items()}
        return data


class Decisions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: int = FORMAT_VERSION
    entry: Id
    fragments: dict[str, FragmentDecision] = Field(default_factory=dict)
    # Name as written in the entry -> entity id, per type. None means "not an
    # entity; stop asking".
    mentions: dict[MentionType, dict[str, Id | None]] = Field(default_factory=dict)

    _doc: CommentedMap | None = PrivateAttr(default=None)

    @classmethod
    def parse(cls, text: str, *, where: str = "decisions") -> Decisions:
        doc = load_yaml(text)
        check_format(doc, where)
        try:
            d = cls.model_validate(doc)
        except ValidationError as e:
            raise FormatError(f"{where}: {e}") from e
        d._doc = doc
        return d

    def to_text(self) -> str:
        doc = self._doc if self._doc is not None else CommentedMap()
        merge_into(doc, self.model_dump(mode="json", exclude_none=True))
        self._doc = doc
        return dump_yaml(doc)

    def mention_target(self, mtype: MentionType, surface: str) -> tuple[bool, str | None]:
        """(decided, entity_id). decided=False means you have not settled it."""
        by_type = self.mentions.get(mtype, {})
        if surface in by_type:
            return True, by_type[surface]
        return False, None
