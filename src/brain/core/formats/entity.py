"""People, orgs, themes, and threads: markdown with YAML frontmatter.

The frontmatter holds structured fields; the body holds your free notes.
Keys you add by hand that brain does not know about are kept when brain
rewrites the file.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PrivateAttr, ValidationError, field_validator
from ruamel.yaml.comments import CommentedMap

from brain.core.formats.entry import Id
from brain.core.formats.yamlio import (
    FORMAT_VERSION,
    FormatError,
    check_format,
    join_frontmatter,
    load_yaml,
    merge_into,
    split_frontmatter,
)
from brain.core.keys import EntityType

# '2025', '2025-03', or '2025-03-14'. YAML reads a bare year as an int.
PartialDate = Annotated[
    str,
    BeforeValidator(lambda v: str(v) if v is not None else v),
    Field(pattern=r"^\d{4}(-\d{2}(-\d{2})?)?$"),
]


def _none_to_list(v: Any) -> Any:
    return [] if v is None else v


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    format: int = FORMAT_VERSION
    id: Id
    type: EntityType
    name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    notes: str = Field(default="", exclude=True)

    _fm: CommentedMap | None = PrivateAttr(default=None)
    _list_fields = field_validator("aliases", mode="before")(_none_to_list)

    @property
    def node_id(self) -> str:
        return f"{self.type}:{self.id}"

    def extra_fields(self) -> dict[str, Any]:
        return dict(self.__pydantic_extra__ or {})

    def to_text(self) -> str:
        fm = self._fm if self._fm is not None else CommentedMap()
        merge_into(fm, self.model_dump(mode="json", by_alias=True))
        self._fm = fm
        notes = self.notes.replace("\r\n", "\n").strip("\n")
        return join_frontmatter(fm, notes + "\n" if notes else "")


class Membership(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    org: Id
    from_: PartialDate | None = Field(default=None, alias="from")
    to: PartialDate | None = None
    role: str | None = None


class Person(_Base):
    type: Literal["person"] = "person"
    role: str | None = None
    works_on: list[str] = Field(default_factory=list)
    memberships: list[Membership] = Field(default_factory=list)

    _more_lists = field_validator("works_on", "memberships", mode="before")(_none_to_list)


class Org(_Base):
    type: Literal["org"] = "org"
    parent: Id | None = None


class Theme(_Base):
    type: Literal["theme"] = "theme"
    kind: Literal["domain", "technique"]
    state: Literal["proposed", "active", "merged"] = "active"
    merged_into: Id | None = None


class ObeInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: dt.date | None = None
    reason: str | None = None


class Thread(_Base):
    type: Literal["thread"] = "thread"
    description: str | None = None
    state: Literal["proposed", "active", "archived", "obe", "merged"] = "active"
    pinned: bool = False
    merged_into: Id | None = None
    obe: ObeInfo = Field(default_factory=ObeInfo)

    @field_validator("obe", mode="before")
    @classmethod
    def _obe(cls, v: Any) -> Any:
        return {} if v is None else v


Entity = Person | Org | Theme | Thread

ENTITY_MODELS: dict[str, type[_Base]] = {"person": Person, "org": Org, "theme": Theme, "thread": Thread}


def parse_entity(text: str, *, where: str, default_type: EntityType | None = None) -> Entity:
    fm_text, body = split_frontmatter(text)
    fm = load_yaml(fm_text)
    check_format(fm, where)
    etype = fm.get("type") or default_type
    if etype not in ENTITY_MODELS:
        raise FormatError(f"{where}: 'type' must be one of {', '.join(ENTITY_MODELS)}")
    if fm.get("type") is None:
        fm["type"] = etype
    try:
        entity = ENTITY_MODELS[etype].model_validate({**fm, "notes": body})
    except ValidationError as e:
        raise FormatError(f"{where}: {e}") from e
    entity._fm = fm
    return entity  # type: ignore[return-value]
