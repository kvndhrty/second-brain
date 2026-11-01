"""Structured results returned by the MCP tools. Field descriptions are shown to the agent."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Fragment(BaseModel):
    id: str
    entry_id: str = Field(description="entry the fragment came from; cite this")
    date: str
    kind: str = Field(description="observation, decision, question, idea, claim, or action_item")
    text: str
    status: str = Field(description="confirmed, speculative, or overcome_by_events")
    state: str | None = Field(default=None, description="open or closed, for questions and action items")
    dream: bool = False
    obe_reason: str | None = None


class SearchHit(BaseModel):
    id: str = Field(description="entry id, fragment id, or entity node id such as person:jane-doe")
    type: str
    label: str
    snippet: str
    date: str | None = None
    entry_id: str | None = None
    kind: str | None = None
    status: str | None = None


class SearchResult(BaseModel):
    hits: list[SearchHit]


class Claim(BaseModel):
    text: str
    citations: list[str] = Field(description="entry ids that support the claim")


class Source(BaseModel):
    entry_id: str
    date: str
    label: str


class AskResult(BaseModel):
    claims: list[Claim]
    insufficient: bool = Field(description="true if the journal does not answer the question")
    sources: list[Source]


class BriefResult(BaseModel):
    target: str = Field(description="node id the brief is about, or text:<words> for a free-text brief")
    label: str
    type: str
    last_looked: str | None = Field(default=None, description="date the user last read a brief on this, if any")
    summary: str | None = Field(default=None, description="running thread summary, for threads")
    where_things_stand: list[Claim]
    recent_decisions: list[Claim]
    open_items: list[Claim]
    people: list[Claim]
    speculative: list[Claim]
    overcome_by_events: list[Claim]
    changed_since: list[Claim]
    open_fragments: list[Fragment] = Field(description="open questions and action items, exactly as recorded")
    sources: list[Source]
    notes: list[str] = Field(default_factory=list)


class EntityResult(BaseModel):
    id: str
    type: str
    name: str
    aliases: list[str]
    details: dict
    notes: str
    summary: str | None
    related: dict[str, list[str]] = Field(description="linked threads, people, orgs, themes, memberships")
    fragment_count: int
    open_items: list[Fragment]
    recent: list[Fragment] = Field(description="most recent linked fragments, newest first")


class EntryResult(BaseModel):
    id: str
    date: str
    title: str | None
    text: str = Field(description="the entry exactly as the user wrote it")
    processed: bool
    fragments: list[Fragment]


class ActiveThread(BaseModel):
    id: str
    name: str
    state: str
    pinned: bool
    last_activity: str | None
    fragments: int
    open_items: int
    people: list[str]
    summary: str | None


class ActiveResult(BaseModel):
    threads: list[ActiveThread]
    inactivity_days: int = Field(description="threads with no activity for this many days are left out unless pinned")


class AddEntryResult(BaseModel):
    entry_id: str
    key: str
    processed: bool
    error: str | None = None
    fragments: list[Fragment] = Field(default_factory=list)
    names_to_review: int = Field(default=0, description="new names the user should confirm with `brain review`")
