"""Step 1: entry -> fragments, mentions, themes, and thread assignments."""

from __future__ import annotations

from brain.core.formats.entry import Entry
from brain.core.formats.sidecars import (
    ExtractedFragment,
    Extraction,
    MentionRef,
    NewThread,
    ThemeRef,
    ThreadRef,
)
from brain.core.index import queries
from brain.core.llm.schemas import ExtractOutput
from brain.core.pipeline.context import PipelineContext
from brain.core.pipeline.match import assign_ids
from brain.core.pipeline.text import attr, locate_quote
from brain.core.temporal import age, dated, iso_day, local_date, today_el

THREAD_CONTEXT = 40
SUMMARY_CHARS = 300


def build_message(ctx: PipelineContext, entry: Entry) -> tuple[str, set[str]]:
    """The user message for the extract prompt, and the thread ids it offers."""
    now = ctx.clock()
    today = local_date(now)
    lines = [
        today_el(now),
        f'<entry id="{attr(entry.id)}" {dated(entry.created, today)} title="{attr(entry.title or "")}">',
        entry.body.rstrip("\n"),
        "</entry>",
        "<existing_threads>",
    ]
    offered = set()
    for t in queries.threads(ctx.conn, limit=THREAD_CONTEXT):
        offered.add(t.id)
        activity = f"{iso_day(t.last_activity)} ({age(t.last_activity, today)} ago)" if t.last_activity else "none yet"
        desc = f" | description: {t.description}" if t.description else ""
        summary = ""
        if t.summary:
            when = f"as of {iso_day(t.summary_as_of)}" if t.summary_as_of else "as of unknown date"
            summary = f" | summary ({when}): {t.summary[:SUMMARY_CHARS]}"
        lines.append(f"- id: {t.id} | name: {t.name} | last activity: {activity}{desc}{summary}")
    lines.append("</existing_threads>")
    lines.append("<known_entities>")
    for etype, label in (("person", "people"), ("org", "organizations"), ("theme", "themes")):
        names = queries.entity_names(ctx.conn, etype)
        if names:
            rendered = ", ".join(f"{n} ({', '.join(a)})" if a else n for _, n, a in names)
            lines.append(f"{label}: {rendered}")
    lines.append("</known_entities>")
    return "\n".join(lines), offered


def extract(ctx: PipelineContext, entry: Entry, previous: Extraction | None) -> Extraction:
    message, offered = build_message(ctx, entry)

    def validate(out: ExtractOutput) -> None:
        bad = sorted(
            {t.thread_id for f in out.fragments for t in f.threads if t.thread_id and t.thread_id not in offered}
        )
        if bad:
            raise ValueError(
                f"unknown thread_id {', '.join(bad)}; use an id from existing_threads or set new_name instead"
            )
        for f in out.fragments:
            for t in f.threads:
                if not t.thread_id and not t.new_name:
                    raise ValueError("each thread entry needs thread_id or new_name")

    result = ctx.llm.run("extract", message, ExtractOutput, validate=validate)
    xs = result.output.fragments
    spans = [locate_quote(entry.body, x.quote) for x in xs]
    ids = assign_ids(entry.id, [(x.kind, x.text, s) for x, s in zip(xs, spans, strict=True)], previous)

    fragments = []
    for fid, x, span in zip(ids, xs, spans, strict=True):
        threads = []
        for t in x.threads:
            if t.thread_id:
                threads.append(ThreadRef(thread=t.thread_id, weight=t.weight))
            elif t.new_name:
                threads.append(
                    ThreadRef(new=NewThread(name=t.new_name, description=t.new_description or ""), weight=t.weight)
                )
        fragments.append(
            ExtractedFragment(
                id=fid,
                kind=x.kind,
                text=x.text,
                span=span,
                quote=x.quote,
                status=x.status,
                someday=x.someday,
                mentions=_dedupe(
                    [MentionRef(surface=m.name.strip(), type=m.type) for m in x.mentions if m.name.strip()]
                ),
                themes=_dedupe([ThemeRef(surface=t.name.strip(), kind=t.kind) for t in x.themes if t.name.strip()]),
                threads=threads,
            )
        )
    return Extraction(
        entry_id=entry.id,
        entry_hash=entry.content_hash,
        model=result.model,
        prompt=result.prompt,
        created_at=ctx.clock(),
        fragments=fragments,
    )


def _dedupe(items: list) -> list:
    seen, out = set(), []
    for item in items:
        k = item.model_dump_json()
        if k not in seen:
            seen.add(k)
            out.append(item)
    return out
