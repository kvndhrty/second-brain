"""Structured results returned by the MCP tools. Field descriptions are shown to the agent."""

from __future__ import annotations

from pydantic import BaseModel, Field

TODAY = "today's date (YYYY-MM-DD, local); every age counts back from it"


class Statement(BaseModel):
    id: str
    note_id: str = Field(description="the note the statement came from; cite this")
    date: str = Field(description="local date the note was written (YYYY-MM-DD)")
    age: str = Field(description="how long before today: days d, weeks w, months mo")
    kind: str = Field(description="fact, decision, question, action, idea, or correction")
    text: str
    status: str = Field(description="current, superseded (a later note replaced it), answered, or repeat")
    replaced_by: str | None = Field(default=None, description="the later statement that superseded or answered it")
    thread: str | None = Field(default=None, description="id of the thread it is filed under")
    thread_name: str | None = None


class SearchHit(BaseModel):
    id: str = Field(description="note id, statement id, or page id")
    type: str = Field(description="note, statement, thread, person, org, or topic")
    label: str
    snippet: str
    date: str | None = None
    note_id: str | None = None
    kind: str | None = None
    status: str | None = None


class SearchResult(BaseModel):
    today: str = Field(description=TODAY)
    hits: list[SearchHit]


class Claim(BaseModel):
    text: str
    citations: list[str] = Field(description="note ids that support the claim")


class Source(BaseModel):
    note_id: str
    date: str
    label: str


class AskResult(BaseModel):
    today: str = Field(description=TODAY)
    claims: list[Claim]
    insufficient: bool = Field(description="true when the journal does not contain the answer")
    sources: list[Source] = Field(description="the cited notes, oldest first")


class Ref(BaseModel):
    id: str
    type: str
    name: str


class BriefResult(BaseModel):
    today: str = Field(description=TODAY)
    topics: list[Ref] = Field(description="the pages the brief covers; empty when it was written from a search")
    last_looked: str | None = Field(default=None, description="when the user last read a brief on these topics")
    where_things_stand: list[Claim]
    recent_decisions: list[Claim]
    open_items: list[Claim]
    people: list[Claim]
    no_longer_applies: list[Claim]
    changed_since: list[Claim]
    sources: list[Source] = Field(description="the cited notes, oldest first")


class Cited(BaseModel):
    text: str
    citations: list[str] = Field(description="note ids")


class PageResult(BaseModel):
    today: str = Field(description=TODAY)
    id: str
    type: str = Field(description="thread, person, org, or topic")
    name: str
    names: list[str] = Field(description="other names it has been called")
    status: str = Field(description="active or closed")
    profile: dict[str, str] = Field(description="for a person: role and org, when known")
    as_of: str | None = Field(default=None, description="date of the newest note the page covers")
    now: Cited | None = Field(default=None, description="threads only: where it stands")
    summary: list[Cited]
    open: list[Statement] = Field(description="open questions and things to do")
    decided: list[Statement] = Field(description="decisions that still stand, newest first")
    history: list[Statement] = Field(description="every statement, newest first, with what replaced it")
    related: list[Ref]


class NoteResult(BaseModel):
    today: str = Field(description=TODAY)
    id: str
    date: str
    title: str | None = None
    text: str = Field(description="the note exactly as the user wrote it")
    processed: bool
    statements: list[Statement]
    mentions: list[Ref]
    changes: list[str] = Field(description="what brain did with the note")


class ActiveThread(BaseModel):
    id: str
    name: str
    last_activity: str | None = None
    open_items: int
    now: str | None = Field(default=None, description="where it stands")


class ActiveResult(BaseModel):
    today: str = Field(description=TODAY)
    threads: list[ActiveThread]


class JotResult(BaseModel):
    today: str = Field(description=TODAY)
    note_id: str
    key: str
    processed: bool
    statements: list[Statement] = Field(default_factory=list)
    changes: list[str] = Field(default_factory=list, description="what brain did with the note")
    error: str | None = None
