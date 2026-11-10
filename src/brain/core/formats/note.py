"""Notes: markdown with YAML frontmatter. The only thing a person writes; the tool never changes the body."""

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


class Note(BaseModel):
    format: int = FORMAT_VERSION
    id: Id
    created: datetime
    title: str | None = None
    about: str | None = None  # the wiki page a "This is wrong" note was written from
    body: str = Field(default="", exclude=True)

    _fm: CommentedMap | None = PrivateAttr(default=None)

    _aware_created = field_validator("created")(_aware)

    @property
    def content_hash(self) -> str:
        return content_hash(f"{self.about or ''}\n{self.body}")

    @property
    def label(self) -> str:
        if self.title:
            return self.title
        first = next((ln.strip() for ln in self.body.splitlines() if ln.strip()), "")
        return first[:80] or self.id

    @classmethod
    def parse(cls, text: str, *, where: str = "note") -> Note:
        fm_text, body = split_frontmatter(text)
        fm = load_yaml(fm_text)
        check_format(fm, where)
        try:
            note = cls.model_validate({**fm, "body": body})
        except ValidationError as e:
            raise FormatError(f"{where}: {e}") from e
        note._fm = fm
        return note

    def to_text(self) -> str:
        fm = self._fm if self._fm is not None else CommentedMap()
        merge_into(fm, self.model_dump(mode="json", exclude_none=True))
        self._fm = fm
        return join_frontmatter(fm, normalize_body(self.body))
