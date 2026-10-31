"""Journal entries: markdown with YAML frontmatter. The LLM never rewrites them."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, PrivateAttr, ValidationError, field_validator
from ruamel.yaml.comments import CommentedMap

from brain.core.formats.yamlio import (
    FORMAT_VERSION,
    FormatError,
    check_format,
    join_frontmatter,
    load_yaml,
    merge_into,
    split_frontmatter,
)

Id = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$", max_length=120)]


def normalize_body(body: str) -> str:
    return body.replace("\r\n", "\n").strip("\n") + "\n" if body.strip() else ""


def content_hash(body: str) -> str:
    return "sha256:" + hashlib.sha256(normalize_body(body).encode()).hexdigest()


def _aware(v: datetime) -> datetime:
    # Hand-written timestamps without an offset are taken as local time.
    return v if v.tzinfo else v.astimezone()


class Entry(BaseModel):
    format: int = FORMAT_VERSION
    id: Id
    created: datetime
    updated: datetime
    title: str | None = None
    tags: list[str] = Field(default_factory=list)
    body: str = Field(default="", exclude=True)

    _fm: CommentedMap | None = PrivateAttr(default=None)

    _aware_created = field_validator("created", "updated")(_aware)

    @field_validator("tags", mode="before")
    @classmethod
    def _tags(cls, v: object) -> object:
        return [] if v is None else v

    @property
    def content_hash(self) -> str:
        return content_hash(self.body)

    @property
    def label(self) -> str:
        if self.title:
            return self.title
        first = next((ln.strip() for ln in self.body.splitlines() if ln.strip()), "")
        return first[:80] or self.id

    @classmethod
    def parse(cls, text: str, *, where: str = "entry") -> Entry:
        fm_text, body = split_frontmatter(text)
        fm = load_yaml(fm_text)
        check_format(fm, where)
        try:
            entry = cls.model_validate({**fm, "body": body})
        except ValidationError as e:
            raise FormatError(f"{where}: {e}") from e
        entry._fm = fm
        return entry

    def to_text(self) -> str:
        fm = self._fm if self._fm is not None else CommentedMap()
        merge_into(fm, self.model_dump(mode="json"))
        self._fm = fm
        return join_frontmatter(fm, normalize_body(self.body))
