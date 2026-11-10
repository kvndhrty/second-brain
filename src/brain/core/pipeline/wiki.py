"""Render the wiki: one markdown page per entity, an index, and the change log.

The files under wiki/ are a rendering of the index. They are overwritten
whenever a page changes, so a hand edit does not last; to change a page,
write a note. Links use `[[file-name|label]]`, which Obsidian and similar
editors follow.
"""

from __future__ import annotations

from datetime import datetime

from brain.core import keys, views
from brain.core.formats.yamlio import dump_yaml
from brain.core.index import queries
from brain.core.index.queries import World
from brain.core.pipeline.context import PipelineContext
from brain.core.storage.base import NotFound
from brain.core.temporal import iso_day

LOG_EVENTS = 300
TITLES = {"thread": "Threads", "person": "People", "org": "Organizations", "topic": "Topics"}
KIND_LABEL = {"question": "question", "action": "to do", "decision": "decided", "fact": "", "idea": "idea"}


class Renderer:
    def __init__(self, ctx: PipelineContext, now: datetime) -> None:
        self.ctx = ctx
        self.now = now
        self.world: World = ctx.world()
        rows = ctx.conn.execute("SELECT id, key, created_at FROM notes").fetchall()
        self.notes = {r[0]: (r[1].rsplit("/", 1)[-1][:-3], iso_day(r[2])) for r in rows}

    def cite(self, note_ids: list[str]) -> str:
        links = [f"[[{self.notes[n][0]}|{self.notes[n][1]}]]" for n in note_ids if n in self.notes]
        return f" ({', '.join(links)})" if links else ""

    def link(self, entity_id: str, name: str) -> str:
        return f"[[{entity_id}|{name}]]"

    def _line(self, ln: views.Line, *, with_kind: bool = True) -> str:
        label = KIND_LABEL.get(ln.kind, ln.kind) if with_kind else ""
        text = ln.text
        if ln.status == "superseded":
            text = f"~~{text}~~" + (f" Replaced by: {ln.replaced_by}" if ln.replaced_by else " (replaced)")
        elif ln.status == "answered":
            text += f" Answered: {ln.replaced_by}" if ln.replaced_by else " (answered)"
        head = f"{ln.date}" + (f" · {label}" if label else "")
        return f"- {head} · {text}{self.cite([ln.note_id])}"

    def page(self, entity_id: str) -> str:
        p = views.page(self.ctx.conn, entity_id, now=self.now, world=self.world)
        fm = {"id": p.id, "type": p.type, "names": [p.name, *p.names], "status": p.status}
        if p.as_of:
            fm["as_of"] = p.as_of
        out = [f"---\n{dump_yaml(fm)}---", f"# {p.name}", ""]
        if p.status == "closed":
            out += [f"Closed{' ' + p.closed_at if p.closed_at else ''}. Everything below is history.", ""]
        facts = [f"{k.capitalize()}: {v}" for k, v in p.profile.items() if v]
        if p.names:
            facts.append("Also called: " + ", ".join(p.names))
        if facts:
            out += [" · ".join(facts), ""]
        if p.now:
            out += ["## Now", "", p.now.text + self.cite(p.now.citations), ""]
        if p.summary:
            out += ["## Summary", "", *[s.text + self.cite(s.citations) for s in p.summary], ""]
        if p.open:
            out += ["## Open", "", *[self._line(ln) for ln in p.open], ""]
        if p.decided:
            out += ["## Decided", "", *[self._line(ln, with_kind=False) for ln in p.decided], ""]
        if p.history:
            out += ["## History", "", *[self._line(ln) for ln in p.history], ""]
        if p.related:
            out += ["## Related", "", " · ".join(self.link(r.id, r.name) for r in p.related), ""]
        return "\n".join(out).rstrip("\n") + "\n"

    def index(self) -> str:
        out = [
            "# Wiki",
            "",
            f"Updated {iso_day(self.now)}. Written by brain from your notes; edits here are overwritten.",
            "",
        ]
        everything = views.entity_list(self.ctx.conn, world=self.world)
        threads = [e for e in everything if e.type == "thread"]
        for title, rows in (
            ("Active threads", [t for t in threads if t.status == "active"]),
            ("Closed threads", [t for t in threads if t.status == "closed"]),
        ):
            if rows:
                out += [f"## {title}", ""]
                for t in rows:
                    now_line = f": {t.now}" if t.now else ""
                    out.append(f"- {self.link(t.id, t.name)}{now_line}")
                out.append("")
        for etype in ("person", "org", "topic"):
            rows = sorted((e for e in everything if e.type == etype), key=lambda e: e.name.casefold())
            if rows:
                out += [f"## {TITLES[etype]}", "", " · ".join(self.link(e.id, e.name) for e in rows), ""]
        out += ["See also [[log|the change log]].", ""]
        return "\n".join(out)

    def log(self) -> str:
        out = ["# Change log", "", "What brain did after each note and in each dream pass, newest first.", ""]
        day = None
        for ev in queries.events(self.ctx.conn, limit=LOG_EVENTS):
            d = iso_day(ev.at)
            if d != day:
                out += ["", f"## {d}", ""]
                day = d
            source = self.cite([ev.source]) if ev.source in self.notes else " (dreaming)"
            out.append(f"- {ev.text}{source}")
        return "\n".join(out).rstrip("\n") + "\n"


def write(ctx: PipelineContext, entity_ids: set[str] | list[str], now: datetime, *, everything: bool = False) -> int:
    """Write the wiki files for these entities (or all of them), plus the index and the log. Returns files written."""
    r = Renderer(ctx, now)
    world = r.world
    ids = set(world.entities) if everything else set(entity_ids)
    written = 0
    for eid in sorted(ids):
        e = world.entities.get(eid)
        if e is None:
            continue
        key = keys.wiki_key(e.type, e.id)
        if e.merged_into or e.status == "provisional":
            try:
                ctx.vault.storage.get(key)
            except NotFound:
                continue
            ctx.vault.delete(key)  # merged away, or no longer confirmed
            written += 1
        else:
            written += ctx.vault.put_text(key, r.page(e.id))
    written += ctx.vault.put_text(keys.WIKI_INDEX, r.index())
    written += ctx.vault.put_text(keys.WIKI_LOG, r.log())
    return written
