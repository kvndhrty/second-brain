"""`ask` and `brief`: retrieve from the index, synthesize with citations.

Every claim the model returns must cite entry ids that were in its input; an
output validator rejects anything else and makes the model retry.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from brain.core import keys
from brain.core.formats.sidecars import BriefState
from brain.core.index import queries, search
from brain.core.index.build import norm_alias
from brain.core.index.queries import FragmentRow
from brain.core.llm.models import LLM
from brain.core.llm.schemas import AskOutput, BriefOutput, Claim
from brain.core.pipeline.text import attr
from brain.core.storage.base import NotFound
from brain.core.storage.lease import LeaseError
from brain.core.temporal import as_of, dated, iso_day, last_mentioned, local_date, today_el
from brain.core.vault import Vault

RRF_K = 60
ASK_EXCERPTS = 14
BRIEF_FRAGMENTS = 120
ENTRY_EXCERPT_CHARS = 1200
NODE_TYPES = ("thread", "person", "org", "theme")


@dataclass(frozen=True)
class Source:
    entry_id: str
    date: str
    label: str


@dataclass
class Answer:
    question: str
    claims: list[Claim]
    insufficient: bool
    sources: dict[str, Source]


@dataclass(frozen=True)
class NodeMatch:
    node_id: str
    type: str
    label: str


class AmbiguousTarget(Exception):
    def __init__(self, text: str, matches: list[NodeMatch]) -> None:
        self.matches = matches
        names = ", ".join(f"{m.node_id} ({m.label})" for m in matches)
        super().__init__(f"'{text}' matches several things: {names}. Use one of the ids.")


@dataclass
class Brief:
    target: str  # node id, or "text:<words>" for a free-text brief
    label: str
    type: str
    since: datetime | None
    output: BriefOutput
    open_items: list[FragmentRow]
    sources: dict[str, Source]
    summary: str | None = None
    summary_as_of: str | None = None  # created_at of the newest entry the summary covers
    recorded: bool = True  # False if the brief time could not be saved (e.g. lease held elsewhere)
    fragment_count: int = 0
    notes: list[str] = field(default_factory=list)


def _sources(conn, entry_ids: set[str]) -> dict[str, Source]:
    return {
        eid: Source(eid, iso_day(created), label)
        for eid, (created, label) in queries.entry_dates(conn, sorted(entry_ids)).items()
    }


def _check_citations(claims: list[Claim], allowed: set[str]) -> None:
    bad = sorted({c for claim in claims for c in claim.citations} - allowed)
    if bad:
        raise ValueError(f"citations must be entry ids from the input; not allowed: {', '.join(bad)}")
    uncited = [claim.text[:60] for claim in claims if not claim.citations]
    if uncited:
        raise ValueError(f"every claim needs at least one citation; uncited: {uncited}")


def _fragment_line(f: FragmentRow, *, today: date, since: datetime | None = None) -> str:
    attrs = [f'entry="{f.entry_id}"', dated(f.created_at, today), f'kind="{f.kind}"', f'status="{f.status}"']
    if f.state:
        attrs.append(f'state="{f.state}"')
    if f.someday:
        attrs.append('someday="yes"')
    if f.obe_reason:
        attrs.append(f'obe_reason="{attr(f.obe_reason)}"')
    if since is not None:
        attrs.append(f'new="{"yes" if datetime.fromisoformat(f.created_at) > since else "no"}"')
    return f"<fragment {' '.join(attrs)}>{f.text}</fragment>"


def _rrf(*rankings: list[search.Hit]) -> list[tuple[str, str]]:
    scores: dict[tuple[str, str], float] = {}
    for ranking in rankings:
        for rank, hit in enumerate(ranking):
            k = (hit.item_id, hit.item_type)
            scores[k] = scores.get(k, 0.0) + 1.0 / (RRF_K + rank + 1)
    return sorted(scores, key=lambda k: -scores[k])


def retrieve(conn, llm: LLM, text: str, *, limit: int) -> list[tuple[str, str]]:
    """(item_id, item_type) for entries and fragments, best first: keyword and vector results fused."""
    kw = search.keyword(conn, text, limit=40, types=("entry", "fragment"))
    vec: list[search.Hit] = []
    has_vec = conn.execute("SELECT value FROM meta WHERE key = 'embed_dim'").fetchone() is not None
    if has_vec and llm.embed_model:
        vec = search.nearest(conn, llm.embed([text])[0], k=40)
    return _rrf(kw, vec)[:limit]


def ask(conn, llm: LLM, question: str, *, now: datetime | None = None) -> Answer:
    now = now or datetime.now(UTC)
    today = local_date(now)
    hits = retrieve(conn, llm, question, limit=ASK_EXCERPTS)
    frag_ids = [i for i, t in hits if t == "fragment"]
    frags = queries.fragments_by_id(conn, frag_ids)
    excerpts: list[tuple[str, str, str]] = []  # (created_at, entry id, element): retrieval picks, date orders
    allowed: set[str] = set()
    for item_id, item_type in hits:
        if item_type == "fragment" and item_id in frags:
            f = frags[item_id]
            allowed.add(f.entry_id)
            excerpts.append(
                (
                    f.created_at,
                    f.entry_id,
                    f'<excerpt entry="{f.entry_id}" {dated(f.created_at, today)} kind="{f.kind}" '
                    f'status="{f.status}">{f.text}</excerpt>',
                )
            )
        elif item_type == "entry":
            row = conn.execute("SELECT created_at, body FROM entries WHERE id = ?", (item_id,)).fetchone()
            if row:
                allowed.add(item_id)
                excerpts.append(
                    (
                        row[0],
                        item_id,
                        f'<excerpt entry="{item_id}" {dated(row[0], today)} kind="entry">'
                        f"{row[1][:ENTRY_EXCERPT_CHARS]}</excerpt>",
                    )
                )
    lines = [today_el(now), f"<question>{question}</question>", "<excerpts>"]
    lines += [el for *_, el in sorted(excerpts, key=lambda x: (x[0], x[1]))]
    lines.append("</excerpts>")
    if not allowed:
        return Answer(question, [], True, {})

    def validate(out: AskOutput) -> None:
        _check_citations(out.claims, allowed)

    out = llm.run("ask", "\n".join(lines), AskOutput, validate=validate).output
    cited = {c for claim in out.claims for c in claim.citations}
    return Answer(question, out.claims, out.insufficient, _sources(conn, cited))


# --- brief -------------------------------------------------------------------


def find_targets(conn, text: str) -> list[NodeMatch]:
    """Nodes that `text` names: an exact node id, an id, an alias, or a close name."""

    def match(nid: str) -> NodeMatch | None:
        r = conn.execute("SELECT id, type, label FROM nodes WHERE id = ?", (nid,)).fetchone()
        return NodeMatch(r[0], r[1], r[2]) if r and r[1] in NODE_TYPES else None

    if ":" in text and (m := match(text)):
        return [m]
    found = [m for t in NODE_TYPES if (m := match(f"{t}:{text}"))]
    if found:
        return found
    alias = norm_alias(text)
    rows = conn.execute(
        "SELECT DISTINCT n.id, n.type, n.label FROM aliases a JOIN nodes n ON n.id = a.node_id WHERE a.alias = ?",
        (alias,),
    ).fetchall()
    found = [NodeMatch(*r) for r in rows if r[1] in NODE_TYPES]
    if found:
        return found
    rows = conn.execute(
        f"SELECT a.alias, n.id, n.type, n.label FROM aliases a JOIN nodes n ON n.id = a.node_id "
        f"WHERE n.type IN ({','.join('?' * len(NODE_TYPES))})",
        NODE_TYPES,
    ).fetchall()
    close = {(r[1], r[2], r[3]) for r in rows if difflib.SequenceMatcher(None, r[0], alias).ratio() >= 0.85}
    return [NodeMatch(*c) for c in sorted(close)]


def _node_fragments(conn, node: NodeMatch) -> list[FragmentRow]:
    family = queries.node_family(conn, node.node_id)
    if node.type == "thread":
        return queries.fragments_linked_to(conn, family, ("in_thread",), limit=BRIEF_FRAGMENTS)
    if node.type == "theme":
        return queries.fragments_linked_to(conn, family, ("about",), limit=BRIEF_FRAGMENTS)
    targets = list(family)
    if node.type == "org":
        targets += [
            r[0]
            for r in conn.execute(
                "SELECT src FROM edges WHERE dst = ? AND type = 'member_of' AND valid_to IS NULL", (node.node_id,)
            )
        ]
    return queries.fragments_linked_to(conn, targets, ("mentions",), limit=BRIEF_FRAGMENTS)


def _people_block(conn, frags: list[FragmentRow], exclude: str, *, today: date) -> list[str]:
    ids = [f"frag:{f.id}" for f in frags]
    if not ids:
        return []
    rows = conn.execute(
        f"SELECT dst, COUNT(*) c FROM edges WHERE type = 'mentions' AND src IN ({','.join('?' * len(ids))}) "
        "AND dst LIKE 'person:%' GROUP BY dst ORDER BY c DESC LIMIT 12",
        ids,
    ).fetchall()
    return [
        f"<person {last_mentioned(queries.last_mention(conn, r[0]), today)}>"
        f"{queries.entity_profile(conn, r[0], today=today)}</person>"
        for r in rows
        if r[0] != exclude
    ]


def _adjacent_activity(conn, node: NodeMatch, frags: list[FragmentRow], *, limit: int = 20) -> list[FragmentRow]:
    """Fragments in neighbouring threads, people, and themes newer than the last fragment about this node."""
    if not frags:
        return []
    cutoff = max(f.created_at for f in frags)
    own = set(queries.node_family(conn, node.node_id))
    src = [f"frag:{f.id}" for f in frags[-40:]]
    rows = conn.execute(
        f"SELECT DISTINCT dst FROM edges WHERE src IN ({','.join('?' * len(src))}) "
        "AND type IN ('in_thread', 'mentions', 'about')",
        src,
    ).fetchall()
    near = [r[0] for r in rows if r[0] not in own]
    seen = {f.id for f in frags}
    later = [
        f
        for f in queries.fragments_linked_to(conn, near, ("in_thread", "mentions", "about"), limit=200)
        if f.created_at > cutoff and f.id not in seen
    ]
    return later[-limit:]


def _last_brief(vault: Vault, target: str) -> datetime | None:
    try:
        state = BriefState.parse(vault.storage.get(keys.BRIEF_STATE), where=keys.BRIEF_STATE)
    except NotFound:
        return None
    return state.last.get(target)


def _record_brief(vault: Vault, target: str, when: datetime) -> bool:
    try:
        try:
            state = BriefState.parse(vault.storage.get(keys.BRIEF_STATE), where=keys.BRIEF_STATE)
        except NotFound:
            state = BriefState()
        state.last[target] = when
        vault.put_bytes(keys.BRIEF_STATE, state.to_bytes())
        return True
    except LeaseError:
        return False


def brief(
    conn,
    llm: LLM,
    vault: Vault,
    target: str,
    *,
    since: datetime | None = None,
    now: datetime | None = None,
    record: bool = True,
) -> Brief:
    """record=False leaves "last looked" unchanged (e.g. when an agent reads a brief for its own context)."""
    now = now or datetime.now(UTC)
    today = local_date(now)
    matches = find_targets(conn, target)
    if len(matches) > 1:
        raise AmbiguousTarget(target, matches)
    summary = None
    summary_as_of = None
    topic_state = ""
    if matches:
        node = matches[0]
        frags = _node_fragments(conn, node)
        key, label, ntype = node.node_id, node.label, node.type
        if ntype == "thread":
            t = queries.thread(conn, node.node_id.split(":", 1)[1])
            summary = t.summary if t else None
            summary_as_of = t.summary_as_of if t else None
            if t and t.state in ("archived", "obe"):
                topic_state = f' state="{"overcome_by_events" if t.state == "obe" else t.state}"'
    else:
        hits = retrieve(conn, llm, target, limit=BRIEF_FRAGMENTS)
        by_id = queries.fragments_by_id(conn, [i for i, t in hits if t == "fragment"])
        frags = sorted(by_id.values(), key=lambda f: (f.created_at, f.id))
        key, label, ntype = f"text:{norm_alias(target)}", target, "text"
    since = since if since is not None else _last_brief(vault, key)
    lines = [
        today_el(now),
        f'<topic type="{ntype}" name="{attr(label)}"{topic_state}/>',
        f"<last_looked>{since.date().isoformat() if since else 'never'}</last_looked>",
    ]
    if summary:
        lines += [f"<thread_summary {as_of(summary_as_of, today)}>", summary, "</thread_summary>"]
    lines += _people_block(conn, frags, key, today=today)
    lines.append("<fragments>")
    lines += [_fragment_line(f, today=today, since=since) for f in frags]
    lines.append("</fragments>")
    related = _adjacent_activity(conn, matches[0], frags) if matches else []
    if related:
        lines.append("<related_activity>")
        lines += [_fragment_line(f, today=today, since=since) for f in related]
        lines.append("</related_activity>")
    allowed = {f.entry_id for f in frags} | {f.entry_id for f in related}
    open_now = [f for f in frags if f.state == "open" and f.status != "overcome_by_events"]
    result = Brief(
        key,
        label,
        ntype,
        since,
        BriefOutput(),
        open_now,
        {},
        summary=summary,
        summary_as_of=summary_as_of,
        fragment_count=len(frags),
    )
    if not frags:
        result.notes.append("No journal fragments are linked to this yet.")
        return result

    def validate(out: BriefOutput) -> None:
        _check_citations(out.all_claims(), allowed)

    result.output = llm.run("brief", "\n".join(lines), BriefOutput, validate=validate).output
    cited = {c for claim in result.output.all_claims() for c in claim.citations} | {f.entry_id for f in open_now}
    result.sources = _sources(conn, cited)
    result.recorded = _record_brief(vault, key, now) if record else False
    return result
