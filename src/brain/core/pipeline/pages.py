"""Layers 7 and 8: write the model-written sections of a page, and check each sentence.

A page's Open, Decided, History, and Related sections are lists that code
builds from the statements, so they need no model. The model writes two
sections: Now (threads only) and Summary. Each sentence it writes cites the
notes it rests on; layer 8 asks, per sentence, whether the cited statements
support it, and drops the ones that are not supported. When nothing
survives, the section is left as it was, unless a statement it covered was
retracted or a decision it covered was replaced: then it is removed.

The summary is updated from the previous summary plus the new statements
once a few have arrived, and rebuilt from all statements when a statement it
covered was superseded or retracted, or when dreaming asks.
"""

from __future__ import annotations

from datetime import datetime

from brain.core import keys
from brain.core.formats.records import PageFile, Section, Sentence, bytes_hash
from brain.core.index.queries import Ent, Stmt
from brain.core.llm.prompts import load_prompt
from brain.core.llm.schemas import CheckOut, Cited, NowOut, SummaryOut
from brain.core.pipeline.context import Calls, PipelineContext
from brain.core.pipeline.text import attr, check_citations, stmt_el
from brain.core.temporal import as_of, local_date, today_el

PROMPTS = ("now", "summary", "check")
NOW_STATEMENTS = 12
NOW_MAX_WORDS = 60
SUMMARY_STATEMENTS = 60
SUMMARY_MIN_NEW = 5  # new statements before a thread's summary is updated
SUMMARY_MIN_NEW_OTHER = 8  # the same for a person, org, or topic
SUMMARY_MIN_TOTAL = 2
SUMMARY_MIN_TOTAL_OTHER = 3
SUMMARY_MAX_SENTENCES = 8


def _hash(stmts: list[Stmt], *extra: str) -> str:
    return bytes_hash(*extra, *[f"{s.id}:{s.status}:{s.text}" for s in stmts])


def _covered_hash(stmts: list[Stmt]) -> str:
    """Changes when a summary's basis does: a statement was retracted, or a decision was replaced.

    An answered question or a replaced fact does not count: the summary is
    brought up to date with the next batch of statements, and Now is always
    current.
    """
    return bytes_hash(*[f"{s.id}:{s.status == 'superseded' and s.kind == 'decision'}" for s in stmts])


def _check(calls: Calls, sentence: Cited, stmts: list[Stmt], today) -> bool:
    cited = [s for s in stmts if s.note_id in sentence.citations]
    if not cited:
        return False
    user = "\n".join([f"<sentence>{sentence.text}</sentence>", *[stmt_el(s, today, with_id=False) for s in cited]])
    return calls.ask("check", user, CheckOut, fallback=CheckOut(supported=False)).supported


def _head(e: Ent, now: datetime) -> str:
    return f'{today_el(now)}\n<page type="{e.type}" name="{attr(e.name)}" status="{e.status}"/>'


def _now(calls: Calls, e: Ent, stmts: list[Stmt], prev: Section | None, now: datetime) -> Section | None:
    today = local_date(now)
    current = [s for s in stmts if s.is_current][-NOW_STATEMENTS:]
    if not current:
        return None
    h = _hash(current, e.name)
    if prev is not None and prev.input_hash == h:
        return prev
    allowed = {s.note_id for s in current}

    def validate(out: NowOut) -> None:
        if not out.text.strip():
            raise ValueError("text is empty")
        if len(out.text.split()) > NOW_MAX_WORDS:
            raise ValueError(f"at most {NOW_MAX_WORDS} words")
        if not out.citations:
            raise ValueError("cite at least one note id")
        check_citations(out.citations, allowed)

    user = "\n".join([_head(e, now), *[stmt_el(s, today, with_id=False) for s in current]])
    out = calls.ask("now", user, NowOut, fallback=NowOut(text="", citations=[]), validate=validate)
    sentence = Cited(text=" ".join(out.text.split()), citations=sorted(set(out.citations)))
    if not sentence.text or not _check(calls, sentence, current, today):
        return prev  # keep what was there; tried again the next time the page changes
    return Section(
        sentences=[Sentence(text=sentence.text, citations=sentence.citations)],
        input_hash=h,
        through=current[-1].created_at,
        model=calls.model,
        prompt=load_prompt("now").stamp,
    )


def _summary(
    calls: Calls, e: Ent, stmts: list[Stmt], prev: Section | None, now: datetime, *, rebuild: bool
) -> Section | None:
    today = local_date(now)
    basis = [s for s in stmts if s.status != "repeat"]
    through = prev.through if prev else None
    covered = [s for s in basis if through and s.created_at <= through]
    new = [s for s in basis if not through or s.created_at > through]
    # A covered statement was superseded, answered, or retracted since: start again from the statements.
    changed = prev is not None and prev.input_hash != _covered_hash(covered)
    if len(basis) < SUMMARY_MIN_TOTAL:
        return None if changed else prev  # too little to summarize; an outdated summary is removed
    full = rebuild or prev is None or changed or not prev.sentences
    if not full and len(new) < (SUMMARY_MIN_NEW if e.type == "thread" else SUMMARY_MIN_NEW_OTHER):
        return prev
    if prev is None and e.type != "thread" and len(basis) < SUMMARY_MIN_TOTAL_OTHER:
        return prev  # a person or topic gets a summary once there is something to summarize
    shown = basis[-SUMMARY_STATEMENTS:] if full else new[-SUMMARY_STATEMENTS:]
    allowed = {s.note_id for s in shown}
    lines = [_head(e, now)]
    if not full and prev is not None:
        allowed |= {c for s in prev.sentences for c in s.citations}
        lines.append(f"<previous_summary {as_of(prev.through, today)}>")
        lines += [f"- {s.text} [{', '.join(s.citations)}]" for s in prev.sentences]
        lines.append("</previous_summary>")
    lines += [stmt_el(s, today, with_id=False) for s in shown]

    def validate(out: SummaryOut) -> None:
        if not out.sentences:
            raise ValueError("write at least one sentence")
        if len(out.sentences) > SUMMARY_MAX_SENTENCES:
            raise ValueError(f"at most {SUMMARY_MAX_SENTENCES} sentences")
        for s in out.sentences:
            if not s.citations:
                raise ValueError("every sentence needs at least one citation")
            check_citations(s.citations, allowed)

    out = calls.ask("summary", "\n".join(lines), SummaryOut, fallback=SummaryOut(sentences=[]), validate=validate)
    # A sentence carried over unchanged from the previous summary was checked when it was written.
    checked = {(x.text, tuple(x.citations)) for x in prev.sentences} if prev and not changed else set()
    kept = [
        s
        for s in out.sentences
        if (" ".join(s.text.split()), tuple(sorted(set(s.citations)))) in checked or _check(calls, s, basis, today)
    ]
    if not kept:
        # Nothing new could be written. A summary whose basis changed may state something that
        # was retracted or replaced, so it is removed; it is written again the next time the
        # page changes.
        return None if changed else prev
    return Section(
        sentences=[Sentence(text=" ".join(s.text.split()), citations=sorted(set(s.citations))) for s in kept],
        input_hash=_covered_hash(basis),
        through=basis[-1].created_at,
        updates=0 if full else (prev.updates if prev else 0) + 1,
        rebuilt_at=now if full else (prev.rebuilt_at if prev else None),
        model=calls.model,
        prompt=load_prompt("summary").stamp,
    )


def update(ctx: PipelineContext, calls: Calls, entity_id: str, now: datetime, *, rebuild: bool = False) -> bool:
    """Bring one entity's page file up to date. True if the page file changed."""
    world = ctx.world()
    e = world.get(entity_id)
    if e is None or e.status == "provisional":
        return False
    stmts = [s for s in world.of_entity(e.id) if s.status != "retracted"]
    key = keys.page_key(e.id)
    found = ctx.read(key, PageFile)
    old = found[0].sections if found else {}
    sections: dict[str, Section] = {}
    if e.type == "thread" and e.status != "closed":
        section = _now(calls, e, stmts, old.get("now"), now)
        if section:
            sections["now"] = section
    elif "now" in old:
        sections["now"] = old["now"]
    section = _summary(calls, e, stmts, old.get("summary"), now, rebuild=rebuild)
    if section:
        sections["summary"] = section
    if found and sections == old:
        return False
    if not sections and not found:
        return False
    ctx.write(key, PageFile(entity_id=e.id, updated_at=now, sections=sections))
    return True
