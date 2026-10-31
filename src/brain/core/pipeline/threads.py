"""Step 4: keep each thread's running summary current.

A summary records the last entry it covers (`through_entry`). An update sends
the previous summary plus only the fragments from later entries.
"""

from __future__ import annotations

from brain.core import keys
from brain.core.formats.sidecars import ThreadSummary
from brain.core.index import queries
from brain.core.llm.schemas import SummaryOutput
from brain.core.pipeline.context import PipelineContext
from brain.core.pipeline.text import attr, short_date

MAX_FRAGMENTS = 150


def summarize(ctx: PipelineContext, thread_id: str) -> ThreadSummary | None:
    t = queries.thread(ctx.conn, thread_id)
    if t is None or t.last_entry is None:
        return None
    prev = ctx.read_sidecar(keys.thread_summary_key(thread_id), ThreadSummary)
    previous = prev[0] if prev else None
    family = [f"thread:{x}" for x in queries.thread_family(ctx.conn, thread_id)]
    frags = queries.fragments_linked_to(ctx.conn, family, ("in_thread",), limit=MAX_FRAGMENTS)
    if previous and previous.through_entry:
        through = queries.entry_dates(ctx.conn, [previous.through_entry]).get(previous.through_entry)
        if through:
            frags = [f for f in frags if (f.created_at, f.entry_id) > (through[0], previous.through_entry)]
    if not frags:
        return None

    lines = [f'<thread id="{attr(t.id)}" name="{attr(t.name)}" description="{attr(t.description or "")}"/>']
    if previous:
        lines += ["<previous_summary>", previous.summary_md, "</previous_summary>"]
    lines.append("<new_fragments>")
    for f in frags:
        extra = f' obe_reason="{attr(f.obe_reason)}"' if f.obe_reason else ""
        state = f' state="{f.state}"' if f.state else ""
        lines.append(
            f'<fragment entry="{f.entry_id}" date="{short_date(f.created_at)}" kind="{f.kind}" '
            f'status="{f.status}"{state}{extra}>{f.text}</fragment>'
        )
    lines.append("</new_fragments>")
    allowed = {f.entry_id for f in frags} | set(previous.citations if previous else [])

    def validate(out: SummaryOutput) -> None:
        bad = sorted(set(out.citations) - allowed)
        if bad:
            raise ValueError(f"citations must be entry ids from the input; not allowed: {', '.join(bad)}")

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
