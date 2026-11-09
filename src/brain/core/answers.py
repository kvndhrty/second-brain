"""`ask` and `brief`: retrieve from the index, synthesize with citations.

Every claim the model returns must cite entry ids that were in its input; an
output validator rejects anything else and makes the model retry.
"""

from __future__ import annotations

import difflib
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from brain.core import keys
from brain.core.formats.sidecars import BriefState
from brain.core.index import queries, search
from brain.core.index.build import norm_alias
from brain.core.index.db import get_meta
from brain.core.index.queries import FragmentRow
from brain.core.llm.models import LLM
from brain.core.llm.schemas import AskOutput, BriefOutput, BriefRoute, Claim
from brain.core.pipeline.text import attr, check_citations, fragment_el
from brain.core.storage.lease import LeaseError
from brain.core.temporal import as_of, dated, iso_day, last_mentioned, local_date, today_el
from brain.core.vault import Vault

RRF_K = 60
ASK_EXCERPTS = 14
BRIEF_FRAGMENTS = 120
ROUTE_HITS = 40
ROUTE_CANDIDATES = 12
ROUTE_MAX_TOPICS = 4
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
    def __init__(self, text: str, matches: list[NodeMatch], *, question: str | None = None) -> None:
        self.matches = matches
        self.question = question
        names = ", ".join(f"{m.node_id} ({m.label})" for m in matches)
        lead = question or f"'{text}' matches several things."
        super().__init__(f"{lead} Candidates: {names}. Use one of the ids.")


@dataclass
class Brief:
    target: str  # node id, node ids joined by ",", or "text:<words>" for a free-text brief
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
    topics: list[NodeMatch] = field(default_factory=list)  # empty for a free-text brief


def _sources(conn, entry_ids: set[str]) -> dict[str, Source]:
    return {
        eid: Source(eid, iso_day(created), label)
        for eid, (created, label) in queries.entry_dates(conn, sorted(entry_ids)).items()
    }


def _check_citations(claims: list[Claim], allowed: set[str]) -> None:
    check_citations((c for claim in claims for c in claim.citations), allowed)
    uncited = [claim.text[:60] for claim in claims if not claim.citations]
    if uncited:
        raise ValueError(f"every claim needs at least one citation; uncited: {uncited}")


def _fragment_line(f: FragmentRow, *, today: date, since: datetime | None = None) -> str:
    extra = ' someday="yes"' if f.someday else ""
    if f.obe_reason:
        extra += f' obe_reason="{attr(f.obe_reason)}"'
    if since is not None:
        extra += f' new="{"yes" if datetime.fromisoformat(f.created_at) > since else "no"}"'
    return fragment_el(f, today, with_id=False, extra=extra)


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
    if get_meta(conn, "embed_dim") is not None and llm.embed_model:
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
                    fragment_el(f, today, tag="excerpt", with_id=False),
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


def _node(conn, node_id: str) -> NodeMatch | None:
    """The node with this id, if it is a type a brief can be about."""
    r = conn.execute("SELECT id, type, label FROM nodes WHERE id = ?", (node_id,)).fetchone()
    return NodeMatch(r[0], r[1], r[2]) if r and r[1] in NODE_TYPES else None


def find_targets(conn, text: str) -> list[NodeMatch]:
    """Nodes that `text` names: an exact node id, an id, an alias, or a close name.

    When a name matches one thread and otherwise only themes (extraction often
    names a theme after its thread), the thread wins; the themes stay reachable
    by node id ("theme:<id>"). A thread and a person or org with the same name
    stay ambiguous.
    """

    def prefer_thread(found: list[NodeMatch]) -> list[NodeMatch]:
        threads = [m for m in found if m.type == "thread"]
        if len(threads) == 1 and all(m.type in ("thread", "theme") for m in found):
            return threads
        return found

    if ":" in text and (m := _node(conn, text)):
        return [m]
    found = [m for t in NODE_TYPES if (m := _node(conn, f"{t}:{text}"))]
    if found:
        return prefer_thread(found)
    alias = norm_alias(text)
    rows = conn.execute(
        "SELECT DISTINCT n.id, n.type, n.label FROM aliases a JOIN nodes n ON n.id = a.node_id WHERE a.alias = ?",
        (alias,),
    ).fetchall()
    found = [NodeMatch(*r) for r in rows if r[1] in NODE_TYPES]
    if found:
        return prefer_thread(found)
    rows = conn.execute(
        f"SELECT a.alias, n.id, n.type, n.label FROM aliases a JOIN nodes n ON n.id = a.node_id "
        f"WHERE n.type IN ({','.join('?' * len(NODE_TYPES))})",
        NODE_TYPES,
    ).fetchall()
    close = {(r[1], r[2], r[3]) for r in rows if difflib.SequenceMatcher(None, r[0], alias).ratio() >= 0.85}
    return prefer_thread([NodeMatch(*c) for c in sorted(close)])


def _node_fragments(conn, node: NodeMatch) -> list[FragmentRow]:
    """Fragments about a node; for an org, also those that mention its current members."""
    if node.type != "org":
        return queries.node_fragments(conn, node.node_id, limit=BRIEF_FRAGMENTS)
    members = [
        r[0]
        for r in conn.execute(
            "SELECT src FROM edges WHERE dst = ? AND type = 'member_of' AND valid_to IS NULL", (node.node_id,)
        )
    ]
    targets = [*queries.node_family(conn, node.node_id), *members]
    return queries.fragments_linked_to(conn, targets, ("mentions",), limit=BRIEF_FRAGMENTS)


def _people_block(conn, frags: list[FragmentRow], exclude: set[str], *, today: date) -> list[str]:
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
        if r[0] not in exclude
    ]


def _adjacent_activity(conn, own: set[str], frags: list[FragmentRow], *, limit: int = 20) -> list[FragmentRow]:
    """Fragments in neighbouring threads, people, and themes newer than the last fragment about these nodes."""
    if not frags:
        return []
    cutoff = max(f.created_at for f in frags)
    near = sorted(set(queries.linked_nodes(conn, [f.id for f in frags[-40:]])) - own)
    seen = {f.id for f in frags}
    later = [
        f
        for f in queries.fragments_linked_to(conn, near, queries.LINK_EDGES, limit=200)
        if f.created_at > cutoff and f.id not in seen
    ]
    return later[-limit:]


def _read_brief_state(vault: Vault) -> BriefState:
    found = vault.read_sidecar(keys.BRIEF_STATE, BriefState)
    return found[0] if found else BriefState()


def _last_brief(vault: Vault, targets: list[str]) -> datetime | None:
    """The earliest last brief among the targets; None if any of them was never briefed."""
    last = _read_brief_state(vault).last
    if not targets or any(t not in last for t in targets):
        return None
    return min(last[t] for t in targets)


def _record_brief(vault: Vault, targets: list[str], when: datetime) -> bool:
    try:
        state = _read_brief_state(vault)
        for t in targets:
            state.last[t] = when
        vault.put_bytes(keys.BRIEF_STATE, state.to_bytes())
        return True
    except LeaseError:
        return False


def _node_last_activity(conn, node: NodeMatch) -> str | None:
    if node.type == "thread":
        t = queries.thread(conn, node.node_id.split(":", 1)[1])
        return t.last_activity if t else None
    return queries.last_mention(conn, node.node_id)


def _route_candidates(conn, llm: LLM, text: str, named: list[NodeMatch]) -> list[NodeMatch]:
    """Nodes the request might be about: the ones it names, then the ones linked to the best search hits."""
    hits = retrieve(conn, llm, text, limit=ROUTE_HITS)
    counts = Counter(queries.linked_nodes(conn, [i for i, t in hits if t == "fragment"]))
    found = {m.node_id: m for m in named}
    for nid, _ in counts.most_common():
        if nid not in found and (m := _node(conn, nid)):
            found[nid] = m
    return list(found.values())[:ROUTE_CANDIDATES]


def choose_topics(conn, llm: LLM, text: str, *, now: datetime) -> tuple[list[NodeMatch], bool]:
    """(topics, routed). A request that names exactly one thing needs no model call. Anything
    else goes to the model, which picks one or more of the candidate nodes, none (a brief on
    the words themselves), or asks which one is meant (AmbiguousTarget)."""
    named = find_targets(conn, text)
    if len(named) == 1:
        return named, False
    cands = _route_candidates(conn, llm, text, named)
    if not cands:
        return [], False
    today = local_date(now)
    by_id = {c.node_id: c for c in cands}
    lines = [today_el(now), f"<request>{text}</request>", "<candidates>"]
    for c in cands:
        lines.append(
            f'<candidate id="{attr(c.node_id)}" type="{c.type}" '
            f"{last_mentioned(_node_last_activity(conn, c), today)}>"
            f"{queries.entity_profile(conn, c.node_id, today=today)}</candidate>"
        )
    lines.append("</candidates>")

    def validate(out: BriefRoute) -> None:
        bad = [t for t in out.targets if t not in by_id]
        if bad:
            raise ValueError(f"use only candidate ids; not found: {', '.join(bad)}")

    out = llm.run("brief_route", "\n".join(lines), BriefRoute, validate=validate).output
    if out.clarify and not out.targets:
        raise AmbiguousTarget(text, named or cands, question=out.clarify)
    return [by_id[t] for t in dict.fromkeys(out.targets)][:ROUTE_MAX_TOPICS], True


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
    """A brief on what `target` names, or on what a free-form request is about.

    record=False leaves "last looked" unchanged (e.g. when an agent reads a brief for its own context).
    """
    now = now or datetime.now(UTC)
    today = local_date(now)
    topics, routed = choose_topics(conn, llm, target, now=now)
    lines = [today_el(now)]
    if routed:
        lines.append(f"<request>{target}</request>")
    summaries: list[tuple[str, str | None]] = []
    if topics:
        by_id: dict[str, FragmentRow] = {}
        for node in topics:
            by_id.update((f.id, f) for f in _node_fragments(conn, node))
            topic_state = ""
            if node.type == "thread":
                t = queries.thread(conn, node.node_id.split(":", 1)[1])
                if t and t.summary:
                    summaries.append((t.summary, t.summary_as_of))
                if t and t.state in ("archived", "obe"):
                    topic_state = f' state="{"overcome_by_events" if t.state == "obe" else t.state}"'
            lines.append(f'<topic type="{node.type}" name="{attr(node.label)}"{topic_state}/>')
        frags = sorted(by_id.values(), key=lambda f: (f.created_at, f.id))[-BRIEF_FRAGMENTS:]
        brief_keys = [n.node_id for n in topics]
        label = " + ".join(n.label for n in topics)
        ntype = topics[0].type if len(topics) == 1 else "several"
        own = {x for n in topics for x in queries.node_family(conn, n.node_id)}
    else:
        hits = retrieve(conn, llm, target, limit=BRIEF_FRAGMENTS)
        found = queries.fragments_by_id(conn, [i for i, t in hits if t == "fragment"])
        frags = sorted(found.values(), key=lambda f: (f.created_at, f.id))
        brief_keys, label, ntype, own = [f"text:{norm_alias(target)}"], target, "text", set()
        lines.append(f'<topic type="text" name="{attr(target)}"/>')
    since = since if since is not None else _last_brief(vault, brief_keys)
    lines.append(f"<last_looked>{since.date().isoformat() if since else 'never'}</last_looked>")
    for text, when in summaries:
        lines += [f"<thread_summary {as_of(when, today)}>", text, "</thread_summary>"]
    lines += _people_block(conn, frags, own, today=today)
    lines.append("<fragments>")
    lines += [_fragment_line(f, today=today, since=since) for f in frags]
    lines.append("</fragments>")
    related = _adjacent_activity(conn, own, frags) if topics else []
    if related:
        lines.append("<related_activity>")
        lines += [_fragment_line(f, today=today, since=since) for f in related]
        lines.append("</related_activity>")
    allowed = {f.entry_id for f in frags} | {f.entry_id for f in related}
    open_now = [f for f in frags if f.is_open]
    result = Brief(
        target=",".join(brief_keys),
        label=label,
        type=ntype,
        since=since,
        output=BriefOutput(),
        open_items=open_now,
        sources={},
        summary=summaries[0][0] if len(summaries) == 1 else None,
        summary_as_of=summaries[0][1] if len(summaries) == 1 else None,
        fragment_count=len(frags),
        topics=topics,
    )
    if not frags:
        result.notes.append("No journal fragments are linked to this yet.")
        return result

    def validate(out: BriefOutput) -> None:
        _check_citations(out.all_claims(), allowed)

    result.output = llm.run("brief", "\n".join(lines), BriefOutput, validate=validate).output
    cited = {c for claim in result.output.all_claims() for c in claim.citations} | {f.entry_id for f in open_now}
    result.sources = _sources(conn, cited)
    result.recorded = _record_brief(vault, brief_keys, now) if record else False
    return result
