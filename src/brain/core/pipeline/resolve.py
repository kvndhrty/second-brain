"""Step 3: match mentions to existing people, orgs, themes, and threads.

Rules, biased toward asking because duplicate people and orgs are the main way
a knowledge base like this decays:

- A name that is exactly one known alias links directly.
- Otherwise candidates come from close spellings, first/last-name and acronym
  matches, and (for themes) embedding similarity of names; the model then
  chooses among them.
- People and orgs link only when the model picks the top candidate with high
  confidence. Anything else goes to `brain review`.
- Themes and threads with no match are created with `state: proposed` and
  used right away; review lists them for accept, rename, or merge.

The step also proposes "answers" links from this entry's fragments to earlier
open questions and action items. Those are suggestions; nothing closes on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from brain.core.formats.decisions import Decisions
from brain.core.formats.entity import Theme, Thread
from brain.core.formats.entry import Entry
from brain.core.formats.sidecars import (
    AnswerProposal,
    Candidate,
    EmbeddingFile,
    Extraction,
    Resolution,
    ResolutionFile,
    bytes_hash,
)
from brain.core.index import queries, search
from brain.core.index.build import norm_alias
from brain.core.llm.prompts import load_prompt
from brain.core.llm.schemas import AnswersOutput, ResolveOutput
from brain.core.pipeline.context import PipelineContext
from brain.core.pipeline.text import attr, cosine, similarity
from brain.core.temporal import dated, last_mentioned, local_date, today_el

AUTO_LINK_CONFIDENCE = 0.8
THEME_LINK_CONFIDENCE = 0.7
THEME_AUTO_SIMILARITY = 0.92
THEME_CANDIDATE_SIMILARITY = 0.6
THREAD_NAME_SIMILARITY = 0.85
ANSWER_CANDIDATE_SIMILARITY = 0.5
ANSWER_CONFIDENCE = 0.6
MAX_ANSWER_CANDIDATES = 10


MentionType = Literal["person", "org", "theme", "thread"]
Outcome = Literal["link", "new", "pending", "ignore"]


@dataclass
class Mention:
    type: MentionType
    surface: str
    contexts: list[str] = field(default_factory=list)
    kind: str | None = None  # theme kind
    description: str = ""  # new thread description


def collect_mentions(
    extraction: Extraction, decisions: Decisions, thread_names: dict[str, str] | None = None
) -> list[Mention]:
    """thread_names maps existing thread ids to names. A theme that only repeats the
    name of its own fragment's thread is dropped: it would duplicate the thread."""
    found: dict[tuple[MentionType, str], Mention] = {}
    thread_names = thread_names or {}

    def add(mtype: MentionType, surface: str, text: str, **extra) -> None:
        m = found.setdefault((mtype, surface), Mention(mtype, surface, **extra))
        if text not in m.contexts:
            m.contexts.append(text)

    for f in extraction.fragments:
        d = decisions.fragments.get(f.id)
        if d and d.deleted:
            continue
        for m in f.mentions:
            add(m.type, m.surface, f.text)
        own = {norm_alias(r.new.name if r.new else thread_names.get(r.thread or "", "")) for r in f.threads} - {""}
        for t in f.themes:
            if norm_alias(t.surface) not in own:
                add("theme", t.surface, f.text, kind=t.kind)
        for r in f.threads:
            if r.new:
                add("thread", r.new.name, f.text, description=r.new.description)
    return list(found.values())


class Resolver:
    def __init__(self, ctx: PipelineContext, entry: Entry, decisions: Decisions) -> None:
        self.ctx = ctx
        self.conn = ctx.conn
        self.entry = entry
        self.decisions = decisions
        self.records: list[Resolution] = []
        self.model_used: str | None = None

    def _record(
        self,
        m: Mention,
        decision: Outcome,
        entity_id: str | None = None,
        *,
        confidence: float = 1.0,
        candidates: list[queries.Candidate] | None = None,
        proposed: dict | None = None,
    ) -> None:
        self.records.append(
            Resolution(
                surface=m.surface,
                type=m.type,
                decision=decision,
                entity_id=entity_id,
                confidence=round(confidence, 3),
                proposed=proposed,
                candidates=[Candidate(entity_id=c.entity_id, score=c.score) for c in candidates or []],
            )
        )

    # --- themes and threads: created as proposed when nothing matches --------

    def _create_theme(self, m: Mention) -> str:
        kind = m.kind if m.kind in ("domain", "technique") else "domain"
        theme = self.ctx.vault.create_entity(
            "theme",
            m.surface,
            lambda tid: Theme(id=tid, name=m.surface, kind=kind, state="proposed"),  # type: ignore[arg-type]
        )
        return theme.id

    def _thread(self, m: Mention) -> None:
        exact = queries.alias_matches(self.conn, "thread", m.surface)
        if exact:
            self._record(m, "link", exact[0])
            return
        best = max(
            (
                (similarity(m.surface, t.name), t.id)
                for t in queries.threads(self.conn, states=("active", "proposed", "archived"))
            ),
            default=(0.0, ""),
        )
        if best[0] >= THREAD_NAME_SIMILARITY:
            self._record(m, "link", best[1], confidence=best[0])
            return
        thread = self.ctx.vault.create_entity(
            "thread",
            m.surface,
            lambda tid: Thread(id=tid, name=m.surface, description=m.description or None, state="proposed"),
        )
        self._record(m, "link", thread.id, proposed={"created": "proposed thread"})

    def _theme_vector_candidates(self, m: Mention) -> list[queries.Candidate]:
        if not self.ctx.llm.embed_model:
            return []
        themes = [(eid, name) for eid, name, _ in queries.entity_names(self.conn, "theme", limit=500)]
        if not themes:
            return []
        missing = [t for t in {m.surface, *(name for _, name in themes)} if t not in self.ctx.name_vectors]
        if missing:
            for text, vec in zip(missing, self.ctx.llm.embed(missing), strict=True):
                self.ctx.name_vectors[text] = vec
        q = self.ctx.name_vectors[m.surface]
        scored = [
            queries.Candidate(eid, name, round(cosine(q, self.ctx.name_vectors[name]), 3)) for eid, name in themes
        ]
        return sorted((c for c in scored if c.score >= THEME_CANDIDATE_SIMILARITY), key=lambda c: -c.score)[:5]

    # --- main ------------------------------------------------------------------

    def run(self, mentions: list[Mention]) -> None:
        ambiguous: list[tuple[Mention, list[queries.Candidate]]] = []
        for m in mentions:
            if m.type == "thread":
                self._thread(m)
                continue
            if self.decisions.mention_target(m.type, m.surface)[0]:  # type: ignore[arg-type]
                continue  # you already settled this name for this entry
            exact = queries.alias_matches(self.conn, m.type, m.surface)
            if len(exact) == 1:
                self._record(m, "link", exact[0])
                continue
            cands = queries.fuzzy_candidates(self.conn, m.type, m.surface)
            if m.type == "theme":
                by_id = {c.entity_id: c for c in cands}
                for c in self._theme_vector_candidates(m):
                    if c.entity_id not in by_id or by_id[c.entity_id].score < c.score:
                        by_id[c.entity_id] = c
                cands = sorted(by_id.values(), key=lambda c: -c.score)[:5]
                if cands and cands[0].score >= THEME_AUTO_SIMILARITY:
                    self._record(m, "link", cands[0].entity_id, confidence=cands[0].score, candidates=cands)
                    continue
            if not cands:
                if m.type == "theme":
                    self._record(m, "link", self._create_theme(m), proposed={"created": "proposed theme"})
                else:
                    self._record(m, "new", proposed={"name": m.surface})
                continue
            ambiguous.append((m, cands))
        if ambiguous:
            self._adjudicate(ambiguous)

    def _adjudicate(self, items: list[tuple[Mention, list[queries.Candidate]]]) -> None:
        now = self.ctx.clock()
        today = local_date(now)
        written = dated(self.entry.created, today)
        lines = [today_el(now), "<mentions>"]
        keyed = {}
        for i, (m, cands) in enumerate(items, 1):
            key = f"m{i}"
            keyed[key] = (m, cands)
            lines.append(f'<mention key="{key}" type="{m.type}" name="{attr(m.surface)}">')
            for text in m.contexts[:3]:
                lines.append(f"<context {written}>{text}</context>")
            for c in cands:
                nid = f"{m.type}:{c.entity_id}"
                profile = queries.entity_profile(self.conn, nid, today=today)
                seen = last_mentioned(queries.last_mention(self.conn, nid), today)
                lines.append(f'<candidate id="{attr(c.entity_id)}" score="{c.score}" {seen}>{profile}</candidate>')
            lines.append("</mention>")
        lines.append("</mentions>")

        def validate(out: ResolveOutput) -> None:
            missing = sorted(set(keyed) - {c.key for c in out.choices})
            if missing:
                raise ValueError(f"give a choice for every mention key; missing: {', '.join(missing)}")

        result = self.ctx.llm.run("resolve", "\n".join(lines), ResolveOutput, validate=validate)
        self.model_used = result.model
        choices = {c.key: c for c in result.output.choices}
        for key, (m, cands) in keyed.items():
            c = choices.get(key)
            ids = {x.entity_id for x in cands}
            top = cands[0]
            if c is None:
                self._record(m, "pending", candidates=cands)
            elif m.type == "theme":
                if c.choice in ids and c.confidence >= THEME_LINK_CONFIDENCE:
                    self._record(m, "link", c.choice, confidence=c.confidence, candidates=cands)
                elif c.choice == "new":
                    self._record(
                        m,
                        "link",
                        self._create_theme(m),
                        confidence=c.confidence,
                        candidates=cands,
                        proposed={"created": "proposed theme"},
                    )
                else:
                    self._record(m, "pending", confidence=c.confidence, candidates=cands)
            elif c.choice == top.entity_id and c.confidence >= AUTO_LINK_CONFIDENCE:
                self._record(m, "link", c.choice, confidence=c.confidence, candidates=cands)
            elif c.choice == "new" and c.confidence >= AUTO_LINK_CONFIDENCE and top.score < 0.95:
                self._record(m, "new", confidence=c.confidence, candidates=cands, proposed={"name": m.surface})
            else:
                self._record(m, "pending", confidence=c.confidence, candidates=cands)


def propose_answers(
    ctx: PipelineContext, entry: Entry, extraction: Extraction, decisions: Decisions, embedding: EmbeddingFile | None
) -> tuple[list[AnswerProposal], str | None]:
    if embedding is None:
        return [], None
    open_items = queries.open_items(ctx.conn, exclude_entry=entry.id)
    if not open_items or not ctx.db.has_vec():
        return [], None
    vectors = {i.id: i.vector() for i in embedding.items}
    live = [
        f for f in extraction.fragments if not (decisions.fragments.get(f.id) and decisions.fragments[f.id].deleted)
    ]
    scores: dict[str, float] = {}
    for f in live:
        if f.id not in vectors:
            continue
        for hit in search.nearest(ctx.conn, vectors[f.id], k=20):
            if hit.item_id in open_items and hit.score >= ANSWER_CANDIDATE_SIMILARITY:
                scores[hit.item_id] = max(scores.get(hit.item_id, 0.0), hit.score)
    cands = sorted(scores, key=lambda i: -scores[i])[:MAX_ANSWER_CANDIDATES]
    if not cands:
        return [], None
    now = ctx.clock()
    today = local_date(now)
    written = dated(entry.created, today)
    lines = [today_el(now), "<new_fragments>"]
    lines += [f'<fragment id="{attr(f.id)}" kind="{f.kind}" {written}>{f.text}</fragment>' for f in live]
    lines += ["</new_fragments>", "<open_items>"]
    for i in sorted(cands, key=lambda i: open_items[i].created_at):
        o = open_items[i]
        lines.append(f'<item id="{attr(o.id)}" {dated(o.created_at, today)} kind="{o.kind}">{o.text}</item>')
    lines.append("</open_items>")
    new_ids = {f.id for f in live}
    result = ctx.llm.run("answers", "\n".join(lines), AnswersOutput)
    proposals = [
        AnswerProposal(fragment=link.fragment, answers=link.answers, confidence=round(link.confidence, 3))
        for link in result.output.links
        if link.fragment in new_ids and link.answers in cands and link.confidence >= ANSWER_CONFIDENCE
    ]
    return proposals, result.model


def resolve(
    ctx: PipelineContext,
    entry: Entry,
    extraction: Extraction,
    extraction_bytes: bytes,
    decisions: Decisions,
    embedding: EmbeddingFile | None,
) -> ResolutionFile:
    resolver = Resolver(ctx, entry, decisions)
    names = {t.id: t.name for t in queries.threads(ctx.conn, states=("active", "proposed", "archived"))}
    resolver.run(collect_mentions(extraction, decisions, names))
    answers, answers_model = propose_answers(ctx, entry, extraction, decisions, embedding)
    return ResolutionFile(
        entry_id=entry.id,
        extraction_hash=bytes_hash(extraction_bytes),
        model=resolver.model_used or answers_model or "none",
        prompt=load_prompt("resolve").stamp,
        created_at=ctx.clock(),
        mentions=resolver.records,
        answers=answers,
    )
