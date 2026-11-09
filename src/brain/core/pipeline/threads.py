"""Step 4: keep each thread's running summary current.

A summary records the last entry it covers (`through_entry`). An update sends
the previous summary plus only the fragments from later entries. Dreaming
calls this with full=True to rewrite a summary from all of the thread's
fragments, since incremental updates carry errors and omissions forward.
"""

from __future__ import annotations

from brain.core import keys
from brain.core.formats.sidecars import ThreadSummary
from brain.core.index import queries
from brain.core.llm.schemas import SummaryOutput
from brain.core.pipeline.context import PipelineContext
from brain.core.pipeline.text import attr, check_citations, fragment_el, thread_el
from brain.core.temporal import as_of, local_date, today_el

MAX_FRAGMENTS = 150


def summarize(ctx: PipelineContext, thread_id: str, *, full: bool = False) -> ThreadSummary | None:
    t = queries.thread(ctx.conn, thread_id)
    if t is None or t.last_entry is None:
        return None
    prev = ctx.read_sidecar(keys.thread_summary_key(thread_id), ThreadSummary)
    previous = None if full or prev is None else prev[0]
    frags = queries.thread_fragments(ctx.conn, thread_id, limit=MAX_FRAGMENTS)
    through = None
    if previous and previous.through_entry:
        through = queries.entry_dates(ctx.conn, [previous.through_entry]).get(previous.through_entry)
        if through:
            frags = [f for f in frags if (f.created_at, f.entry_id) > (through[0], previous.through_entry)]
    if not frags:
        return None

    now = ctx.clock()
    today = local_date(now)
    lines = [
        today_el(now),
        thread_el(t),
    ]
    if previous:
        lines += [
            f"<previous_summary {as_of(through[0] if through else None, today)}>",
            previous.summary_md,
            "</previous_summary>",
        ]
    lines.append("<new_fragments>")
    for f in frags:
        extra = f' obe_reason="{attr(f.obe_reason)}"' if f.obe_reason else ""
        lines.append(fragment_el(f, today, with_id=False, extra=extra))
    lines.append("</new_fragments>")
    allowed = {f.entry_id for f in frags} | set(previous.citations if previous else [])

    def validate(out: SummaryOutput) -> None:
        check_citations(out.citations, allowed)

    result = ctx.llm.run("thread_summary", "\n".join(lines), SummaryOutput, validate=validate)
    newest = max(frags, key=lambda f: (f.created_at, f.entry_id))
    summary = ThreadSummary(
        thread_id=thread_id,
        model=result.model,
        prompt=result.prompt,
        updated_at=ctx.clock(),
        through_entry=newest.entry_id,
        summary_md=result.output.summary_md,
        citations=sorted(set(result.output.citations) | (set(previous.citations) if previous else set())),
    )
    ctx.write_sidecar(keys.thread_summary_key(thread_id), summary)
    return summary
