"""What each prompt returns.

Every layer's output is a choice from a list or a short structure, so a small
model can produce it and code can check it. Field descriptions are sent to the
model as part of the schema.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from brain.core.formats.records import Kind, NameType


class SplitItem(BaseModel):
    text: str = Field(description="one statement that can be read on its own, with names written out")
    quote: str = Field(description="the exact words of the note this statement comes from")


class SplitOut(BaseModel):
    statements: list[SplitItem] = Field(default_factory=list)


class KindOut(BaseModel):
    kind: Kind


class SpottedName(BaseModel):
    name: str = Field(description="the name exactly as written in the note")
    type: NameType


class NamesOut(BaseModel):
    names: list[SpottedName] = Field(default_factory=list)


class ChoiceOut(BaseModel):
    choice: str = Field(description='the id of one candidate, or "new"')


class FileOut(BaseModel):
    choice: str = Field(description='the id of one candidate thread, "new", or "none"')
    new_name: str | None = Field(default=None, description='only when choice is "new": a short name for the new thread')


class RelationOut(BaseModel):
    relation: Literal["answers", "supersedes", "repeats", "unrelated"]


class OpOut(BaseModel):
    op: Literal["same_as", "not_same_as", "rename", "close", "reopen", "retract", "not_part_of", "none"]


class TargetsOut(BaseModel):
    targets: list[str] = Field(default_factory=list, description="candidate ids, in the order the rule asks for")
    new_name: str | None = Field(default=None, description="only for rename: the new name")


class Cited(BaseModel):
    text: str = Field(description="one sentence")
    citations: list[str] = Field(description="note ids that support the sentence")


class NowOut(BaseModel):
    text: str = Field(description="one or two sentences, at most 40 words")
    citations: list[str] = Field(default_factory=list, description="note ids the text rests on")


class SummaryOut(BaseModel):
    sentences: list[Cited] = Field(default_factory=list)


class CheckOut(BaseModel):
    supported: bool


class SameOut(BaseModel):
    same: bool


class StaleOut(BaseModel):
    verdict: Literal["answered", "overtaken", "open"]
    by: str | None = Field(default=None, description="id of the later statement that answers or overtakes it")


class ProfileOut(BaseModel):
    role: str | None = Field(
        default=None, description="the person's role or job, in a few words, if the statements say"
    )
    org: str | None = Field(
        default=None, description="id of the candidate organization the person belongs to, if stated"
    )


class Claim(BaseModel):
    text: str
    citations: list[str] = Field(description="note ids that support the claim")


class AskOutput(BaseModel):
    claims: list[Claim] = Field(default_factory=list)
    insufficient: bool = False


class BriefOutput(BaseModel):
    where_things_stand: list[Claim] = Field(default_factory=list)
    recent_decisions: list[Claim] = Field(default_factory=list)
    open_items: list[Claim] = Field(default_factory=list)
    people: list[Claim] = Field(default_factory=list)
    no_longer_applies: list[Claim] = Field(default_factory=list)
    changed_since: list[Claim] = Field(default_factory=list)

    def all_claims(self) -> list[Claim]:
        return [c for name in BriefOutput.model_fields for c in getattr(self, name)]


class BriefRoute(BaseModel):
    targets: list[str] = Field(
        default_factory=list,
        description="candidate ids the brief should cover, most relevant first; empty for a brief on the words alone",
    )
    clarify: str | None = Field(
        default=None, description="a short question to ask instead, only when the candidates do not settle it"
    )


class Probe(BaseModel):
    ok: bool
    word: str
