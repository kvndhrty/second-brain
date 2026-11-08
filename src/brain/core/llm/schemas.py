"""Typed outputs requested from the models.

These are what the model fills in. The pipeline turns them into the sidecar
formats in formats/sidecars.py, adding ids, spans, and version stamps.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class XMention(BaseModel):
    name: str = Field(description="name as written in the entry")
    type: Literal["person", "org"]


class XTheme(BaseModel):
    name: str
    kind: Literal["domain", "technique"]


class XThread(BaseModel):
    thread_id: str | None = Field(None, description="id of an existing thread from the list")
    new_name: str | None = Field(None, description="name for a new thread, only if no existing thread fits")
    new_description: str | None = None
    weight: float = Field(0.8, ge=0, le=1)


class XFragment(BaseModel):
    kind: Literal["observation", "decision", "question", "idea", "claim", "action_item"]
    text: str
    quote: str = Field(description="exact substring of the entry")
    status: Literal["confirmed", "speculative"] = "confirmed"
    someday: bool = False
    mentions: list[XMention] = Field(default_factory=list)
    themes: list[XTheme] = Field(default_factory=list)
    threads: list[XThread] = Field(default_factory=list)


class ExtractOutput(BaseModel):
    fragments: list[XFragment]


class MentionChoice(BaseModel):
    key: str
    choice: str = Field(description='a candidate entity id, "new", or "unsure"')
    confidence: float = Field(ge=0, le=1)
    reason: str = ""


class ResolveOutput(BaseModel):
    choices: list[MentionChoice]


class AnswerLink(BaseModel):
    fragment: str = Field(description="id of the new fragment")
    answers: str = Field(description="id of the earlier open item it answers or closes")
    confidence: float = Field(ge=0, le=1)


class AnswersOutput(BaseModel):
    links: list[AnswerLink] = Field(default_factory=list)


class SummaryOutput(BaseModel):
    summary_md: str
    citations: list[str] = Field(default_factory=list)


class SupersedePair(BaseModel):
    older: str = Field(description="id of the earlier fragment that may no longer hold")
    newer: str = Field(description="id of the later fragment that replaces or contradicts it")
    reason: str = Field(description="one sentence: what changed")
    confidence: float = Field(ge=0, le=1)


class SupersedeOutput(BaseModel):
    pairs: list[SupersedePair] = Field(default_factory=list)


class SweepCheck(BaseModel):
    item: str = Field(description="id of the open or speculative item")
    verdict: Literal["still_open", "probably_resolved", "unclear"]
    evidence: str | None = Field(None, description="id of the later fragment the verdict rests on, if any")
    reason: str = Field(description="one sentence")
    confidence: float = Field(0.7, ge=0, le=1)
    citations: list[str] = Field(default_factory=list, description="entry ids the verdict rests on")


class SweepOutput(BaseModel):
    checks: list[SweepCheck] = Field(default_factory=list)


class NowOutput(BaseModel):
    line: str = Field(description="one sentence, at most about 30 words")
    citations: list[str] = Field(default_factory=list, description="entry ids the line rests on")


class Claim(BaseModel):
    text: str
    citations: list[str] = Field(description="entry ids that support the claim")


class AskOutput(BaseModel):
    claims: list[Claim] = Field(default_factory=list)
    insufficient: bool = False


class BriefOutput(BaseModel):
    where_things_stand: list[Claim] = Field(default_factory=list)
    recent_decisions: list[Claim] = Field(default_factory=list)
    open_items: list[Claim] = Field(default_factory=list)
    people: list[Claim] = Field(default_factory=list)
    speculative: list[Claim] = Field(default_factory=list)
    overcome_by_events: list[Claim] = Field(default_factory=list)
    changed_since: list[Claim] = Field(default_factory=list)

    def all_claims(self) -> list[Claim]:
        return [c for name in BriefOutput.model_fields for c in getattr(self, name)]


class Probe(BaseModel):
    ok: bool
    word: str
