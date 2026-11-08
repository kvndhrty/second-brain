"""Turn the files of one unit into index rows.

A unit is either an entry (entry file, extraction, resolution, decisions,
embeddings) or an entity (entity file, plus a summary for threads). Each
rebuild deletes every row the unit owns and inserts them again, so the result
depends only on the current files, never on the order of changes.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field

from brain.core import keys
from brain.core.formats.decisions import Decisions, FragmentDecision
from brain.core.formats.entity import Entity, Org, Person, Theme, Thread, parse_entity
from brain.core.formats.entry import Entry, content_hash
from brain.core.formats.sidecars import (
    OPEN_KINDS,
    DreamProposals,
    EmbeddingFile,
    ExtractedFragment,
    Extraction,
    ResolutionFile,
    ThreadNow,
    ThreadSummary,
    bytes_hash,
)
from brain.core.formats.yamlio import FormatError
from brain.core.index.db import IndexDB, vec_blob


@dataclass
class UnitFile:
    key: str
    data: bytes


@dataclass
class UnitFiles:
    unit: str
    by_role: dict[str, list[UnitFile]] = field(default_factory=dict)

    def one(self, role: str) -> UnitFile | None:
        files = self.by_role.get(role, [])
        return files[0] if files else None


def norm_alias(text: str) -> str:
    return " ".join(text.casefold().split())


def node_id(kind: str, ident: str) -> str:
    return f"{kind}:{ident}"


def _review_id(kind: str, owner: str, subject: str) -> str:
    return hashlib.sha256(f"{kind}|{owner}|{subject}".encode()).hexdigest()[:16]


class UnitBuilder:
    def __init__(self, db: IndexDB, embed_model: str) -> None:
        self.db = db
        self.conn: sqlite3.Connection = db.conn
        self.embed_slug = keys.model_slug(embed_model) if embed_model else None

    # --- deletion ----------------------------------------------------------

    def clear(self, unit: str) -> None:
        c = self.conn
        if unit.startswith("entry:"):
            entry_id = unit.split(":", 1)[1]
            c.execute("DELETE FROM fragments WHERE entry_id = ?", (entry_id,))
            c.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
        if unit.startswith("thread:"):
            c.execute("DELETE FROM threads WHERE id = ?", (unit.split(":", 1)[1],))
        if self.db.has_vec():
            c.execute("DELETE FROM vec_items WHERE rowid IN (SELECT rowid FROM embeddings WHERE owner = ?)", (unit,))
        for table in ("nodes", "aliases", "edges", "review_items", "suggestions", "embeddings", "fts"):
            c.execute(f"DELETE FROM {table} WHERE owner = ?", (unit,))

    # --- helpers -----------------------------------------------------------

    def _problem(self, key: str, message: str) -> None:
        self.conn.execute(
            "INSERT INTO problems(key, message) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET message = excluded.message",
            (key, message),
        )

    def _node(self, nid: str, ntype: str, label: str, owner: str, *, created=None, updated=None, data=None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO nodes(id, type, label, created_at, updated_at, data, owner) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (nid, ntype, label, created, updated, json.dumps(data or {}, sort_keys=True), owner),
        )

    def _edge(
        self,
        src: str,
        dst: str,
        etype: str,
        owner: str,
        *,
        origin: str,
        confirmed: bool,
        weight: float = 1.0,
        entry_id: str | None = None,
        fragment_id: str | None = None,
        valid_from: str | None = None,
        valid_to: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO edges(src, dst, type, weight, origin, confirmed, entry_id, fragment_id, "
            "valid_from, valid_to, owner) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (src, dst, etype, weight, origin, int(confirmed), entry_id, fragment_id, valid_from, valid_to, owner),
        )

    def _fts(self, item_id: str, item_type: str, owner: str, title: str, body: str) -> None:
        self.conn.execute(
            "INSERT INTO fts(item_id, item_type, owner, title, body) VALUES (?, ?, ?, ?, ?)",
            (item_id, item_type, owner, title, body),
        )

    def _review(self, kind: str, owner: str, subject: str, created_at: str, payload: dict, entry_id=None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO review_items(id, kind, entry_id, created_at, payload, owner) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_review_id(kind, owner, subject), kind, entry_id, created_at, json.dumps(payload, sort_keys=True), owner),
        )

    def _parse(self, f: UnitFile | None, parse):
        if f is None:
            return None
        try:
            return parse(f)
        except FormatError as e:
            self._problem(f.key, str(e))
            return None

    # --- entry units -------------------------------------------------------

    def build_entry(self, files: UnitFiles) -> None:
        unit = files.unit
        entry_files = sorted(files.by_role.get("entry", []), key=lambda f: f.key)
        entry: Entry | None = None
        entry_key = ""
        for f in entry_files:
            parsed = self._parse(f, lambda f: Entry.parse(f.data.decode("utf-8"), where=f.key))
            if parsed is None:
                continue
            if entry is None:
                entry, entry_key = parsed, f.key
            else:
                self._problem(f.key, f"duplicate entry id {parsed.id} (also in {entry_key})")
        if entry is None:
            return  # sidecars without an entry: nothing to index

        extraction: Extraction | None = self._parse(
            files.one("extraction"), lambda f: Extraction.parse(f.data, where=f.key)
        )
        resolution: ResolutionFile | None = self._parse(
            files.one("resolution"), lambda f: ResolutionFile.parse(f.data, where=f.key)
        )
        decisions: Decisions = self._parse(
            files.one("decisions"), lambda f: Decisions.parse(f.data.decode("utf-8"), where=f.key)
        ) or Decisions(entry=entry.id)

        created = entry.created.isoformat()
        if extraction is None:
            state = "pending"
        elif extraction.entry_hash != entry.content_hash:
            state = "stale"
        else:
            state = "current"
        self.conn.execute(
            "INSERT INTO entries(id, key, created_at, updated_at, title, body, content_hash, extraction, next_step) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'extract')",
            (
                entry.id,
                entry_key,
                created,
                entry.updated.isoformat(),
                entry.title,
                entry.body,
                entry.content_hash,
                state,
            ),
        )
        enode = node_id("entry", entry.id)
        self._node(
            enode,
            "entry",
            entry.label,
            unit,
            created=created,
            updated=entry.updated.isoformat(),
            data={"key": entry_key, "tags": entry.tags},
        )
        self._fts(entry.id, "entry", unit, entry.title or "", entry.body)

        fragment_texts: dict[str, str] = {}
        if extraction is not None:
            fragment_texts = self._build_fragments(unit, entry, created, extraction, resolution, decisions)
            dreams = self._parse(files.one("dream_proposals"), lambda f: DreamProposals.parse(f.data, where=f.key))
            if dreams is not None:
                self._build_dream_proposals(unit, entry, created, dreams, decisions, fragment_texts)
        embedded = self._build_embeddings(unit, files, entry, fragment_texts)

        # Which pipeline step this entry needs next. Each step records the
        # exact version of its input, so a changed input re-runs only what follows.
        xfile = files.one("extraction")
        if state != "current" or xfile is None:
            next_step = "extract"
        elif self.embed_slug and not {entry.id, *fragment_texts} <= embedded:
            next_step = "embed"
        elif resolution is None or resolution.extraction_hash != bytes_hash(xfile.data):
            next_step = "resolve"
        else:
            next_step = "done"
        self.conn.execute("UPDATE entries SET next_step = ? WHERE id = ?", (next_step, entry.id))

    def _build_fragments(
        self,
        unit: str,
        entry: Entry,
        created: str,
        extraction: Extraction,
        resolution: ResolutionFile | None,
        decisions: Decisions,
    ) -> dict[str, str]:
        enode = node_id("entry", entry.id)
        resolved = {(r.type, r.surface): r for r in resolution.mentions} if resolution else {}

        def target(mtype: str, surface: str) -> tuple[str, str, bool] | None:
            decided, ent = decisions.mention_target(mtype, surface)  # type: ignore[arg-type]
            if decided:
                return (ent, "user", True) if ent else None
            r = resolved.get((mtype, surface))
            if r and r.decision == "link" and r.entity_id:
                return r.entity_id, "llm", False
            return None

        texts: dict[str, str] = {}
        seen_ids = set()
        unsettled: dict[tuple[str, str], list[str]] = {}
        for frag in extraction.fragments:
            seen_ids.add(frag.id)
            d = decisions.fragments.get(frag.id) or FragmentDecision()
            if d.deleted:
                continue
            fid = frag.id
            fnode = node_id("frag", fid)
            texts[fid] = frag.text
            self._insert_fragment(unit, entry, created, frag, d)
            self._edge(
                enode, fnode, "contains", unit, origin="system", confirmed=True, entry_id=entry.id, fragment_id=fid
            )
            self._fts(fid, "fragment", unit, frag.kind, frag.text)

            refs = [(m.type, m.surface, "mentions") for m in frag.mentions]
            refs += [("theme", t.surface, "about") for t in frag.themes]
            for mtype, surface, etype in refs:
                t = target(mtype, surface)
                if t is None:
                    if not decisions.mention_target(mtype, surface)[0]:  # type: ignore[arg-type]
                        unsettled.setdefault((mtype, surface), []).append(fid)
                    continue
                ent, origin, confirmed = t
                self._edge(
                    fnode,
                    node_id(mtype, ent),
                    etype,
                    unit,
                    origin=origin,
                    confirmed=confirmed,
                    entry_id=entry.id,
                    fragment_id=fid,
                )

            removed = set(d.threads.remove) if d.threads else set()
            for ref in frag.threads:
                if ref.thread:
                    tid: str | None = ref.thread
                else:
                    # A proposed new thread; the resolve step records which thread file it became.
                    r = resolved.get(("thread", ref.surface))
                    tid = r.entity_id if r and r.decision == "link" else None
                if tid and tid not in removed:
                    self._edge(
                        fnode,
                        node_id("thread", tid),
                        "in_thread",
                        unit,
                        origin="llm",
                        confirmed=False,
                        weight=ref.weight,
                        entry_id=entry.id,
                        fragment_id=fid,
                    )
            for tid in d.threads.add if d.threads else []:
                self.conn.execute(
                    "DELETE FROM edges WHERE src = ? AND dst = ? AND type = 'in_thread' AND fragment_id = ?",
                    (fnode, node_id("thread", tid), fid),
                )
                self._edge(
                    fnode,
                    node_id("thread", tid),
                    "in_thread",
                    unit,
                    origin="user",
                    confirmed=True,
                    entry_id=entry.id,
                    fragment_id=fid,
                )
            for answered in d.answers or []:
                self._edge(
                    fnode,
                    node_id("frag", answered),
                    "answers",
                    unit,
                    origin="user",
                    confirmed=True,
                    entry_id=entry.id,
                    fragment_id=fid,
                )

        # Review items: mentions nobody has settled, answer proposals, and
        # decisions whose fragment no longer exists.
        if resolution is not None:
            for (mtype, surface), fids in sorted(unsettled.items()):
                r = resolved.get((mtype, surface))
                if r is None or r.decision in ("new", "pending"):
                    kind = (
                        f"new_{mtype}"
                        if r and r.decision == "new" and mtype in ("person", "org")
                        else ("ambiguous_mention")
                    )
                    payload = {"type": mtype, "surface": surface, "fragments": fids}
                    if r:
                        payload["proposed"] = r.proposed
                        payload["candidates"] = [c.model_dump() for c in r.candidates]
                    self._review(kind, unit, f"{mtype}|{surface}", created, payload, entry.id)
            for p in resolution.answers:
                d = decisions.fragments.get(p.fragment) or FragmentDecision()
                if p.answers in (d.answers or []) or p.answers in (d.not_answers or []):
                    continue
                if p.fragment in texts:
                    self._review(
                        "proposed_answer",
                        unit,
                        f"{p.fragment}|{p.answers}",
                        created,
                        {"fragment": p.fragment, "answers": p.answers, "confidence": p.confidence},
                        entry.id,
                    )
        for fid, d in sorted(decisions.fragments.items()):
            if fid not in seen_ids and d != FragmentDecision(deleted=True):
                self._review(
                    "orphaned_decision",
                    unit,
                    fid,
                    created,
                    {"fragment": fid, "decision": d.model_dump(mode="json", exclude_none=True)},
                    entry.id,
                )
        return texts

    def _build_dream_proposals(
        self,
        unit: str,
        entry: Entry,
        created: str,
        dreams: DreamProposals,
        decisions: Decisions,
        texts: dict[str, str],
    ) -> None:
        """Review items and sweep suggestions from dreaming. A proposal you have
        already decided on is left out, so the file needs no clean-up."""
        for p in dreams.proposals:
            if p.fragment not in texts:
                continue  # the fragment was deleted or re-extracted away
            d = decisions.fragments.get(p.fragment) or FragmentDecision()
            if p.kind == "supersede" and p.other:
                if d.status is not None or d.someday:
                    continue  # you set this fragment's status yourself
                self._review(
                    "dream_obe",
                    unit,
                    f"{p.fragment}|{p.other}",
                    created,
                    {
                        "fragment": p.fragment,
                        "superseded_by": p.other,
                        "reason": p.reason,
                        "confidence": p.confidence,
                        "citations": p.citations,
                    },
                    entry.id,
                )
            elif p.kind == "answer" and p.other:
                if p.other in (d.answers or []) or p.other in (d.not_answers or []):
                    continue
                self._review(
                    "proposed_answer",
                    unit,
                    f"{p.fragment}|{p.other}",
                    created,
                    {
                        "fragment": p.fragment,
                        "answers": p.other,
                        "confidence": p.confidence,
                        "source": "dream",
                        "reason": p.reason,
                    },
                    entry.id,
                )
            elif p.kind == "sweep" and p.verdict:
                if d.status is not None or d.state is not None:
                    continue  # already decided in the sweep or elsewhere
                self.conn.execute(
                    "INSERT OR REPLACE INTO suggestions(fragment_id, verdict, evidence, reason, citations, "
                    "checked_through, owner) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (p.fragment, p.verdict, p.other, p.reason, json.dumps(p.citations), p.checked_through, unit),
                )

    def _insert_fragment(
        self, unit: str, entry: Entry, created: str, frag: ExtractedFragment, d: FragmentDecision
    ) -> None:
        status = d.status or frag.status
        state = d.state or frag.state or ("open" if frag.kind in OPEN_KINDS else None)
        obe_date = obe_reason = None
        if status == "overcome_by_events" and d.obe:
            obe_date = d.obe.date.isoformat() if d.obe.date else None
            obe_reason = d.obe.reason
        someday = frag.someday if d.someday is None else d.someday
        span = frag.span or (None, None)
        self.conn.execute(
            "INSERT OR REPLACE INTO fragments(id, entry_id, kind, text, status, status_origin, someday, state, "
            "state_origin, closed_note, obe_date, obe_reason, span_start, span_end, quote, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                frag.id,
                entry.id,
                frag.kind,
                frag.text,
                status,
                "user" if d.status else "llm",
                int(someday),
                state,
                "user" if d.state else "llm",
                d.closed_note,
                obe_date,
                obe_reason,
                span[0],
                span[1],
                frag.quote,
                created,
            ),
        )
        self._node(
            node_id("frag", frag.id),
            "fragment",
            frag.text[:120],
            unit,
            created=created,
            data={"kind": frag.kind, "entry": entry.id},
        )

    def _build_embeddings(self, unit: str, files: UnitFiles, entry: Entry, fragment_texts: dict[str, str]) -> set[str]:
        """Index current vectors; return the ids that have one."""
        embedded: set[str] = set()
        if not self.embed_slug:
            return embedded
        for f in files.by_role.get("embedding", []):
            if keys.classify(f.key).model_slug != self.embed_slug:  # type: ignore[union-attr]
                continue
            emb = self._parse(f, lambda f: EmbeddingFile.parse(f.data, where=f.key))
            if emb is None:
                continue
            if not self.db.ensure_vec(emb.dim):
                self._problem(f.key, f"embedding dimension {emb.dim} does not match the index; run brain reindex")
                continue
            current = {entry.id: entry.content_hash}
            current.update({fid: content_hash(text) for fid, text in fragment_texts.items()})
            for item in emb.items:
                if current.get(item.id) != item.hash:
                    continue  # stale or orphaned vector; the pipeline will replace it
                cur = self.conn.execute(
                    "INSERT INTO embeddings(owner_id, model, content_hash, owner) VALUES (?, ?, ?, ?)",
                    (item.id, emb.model, item.hash, unit),
                )
                self.conn.execute(
                    "INSERT INTO vec_items(rowid, embedding) VALUES (?, ?)", (cur.lastrowid, vec_blob(item.vector()))
                )
                embedded.add(item.id)
        return embedded

    # --- entity units ------------------------------------------------------

    def build_entity(self, files: UnitFiles) -> None:
        unit = files.unit
        etype = unit.split(":", 1)[0]
        candidates = sorted(files.by_role.get("entity", []), key=lambda f: f.key)
        entity = None
        ekey = ""

        def parse(f: UnitFile) -> Entity:
            kinfo = keys.classify(f.key)
            return parse_entity(f.data.decode("utf-8"), where=f.key, default_type=kinfo.entity_type if kinfo else None)

        for f in candidates:
            parsed = self._parse(f, parse)
            if parsed is None:
                continue
            if entity is None:
                entity, ekey = parsed, f.key
            else:
                self._problem(f.key, f"duplicate {etype} id {parsed.id} (also in {ekey})")
        if entity is None:
            return
        nid = entity.node_id
        data = entity.model_dump(mode="json", by_alias=True, exclude={"format", "id", "type", "name", "aliases"})
        data["key"] = ekey
        if entity.notes.strip():
            data["notes"] = entity.notes.strip()
        self._node(nid, entity.type, entity.name, unit, data=data)
        for alias in {norm_alias(entity.name), *(norm_alias(a) for a in entity.aliases)}:
            if alias:
                self.conn.execute(
                    "INSERT OR IGNORE INTO aliases(alias, node_id, owner) VALUES (?, ?, ?)", (alias, nid, unit)
                )
        self._fts(entity.id, entity.type, unit, entity.name, "\n".join([*entity.aliases, entity.notes]))
        seen = ""  # entity files carry no creation time

        if isinstance(entity, Person):
            for m in entity.memberships:
                self._edge(
                    nid,
                    node_id("org", m.org),
                    "member_of",
                    unit,
                    origin="user",
                    confirmed=True,
                    valid_from=m.from_,
                    valid_to=m.to,
                )
        elif isinstance(entity, Org):
            if entity.parent:
                self._edge(nid, node_id("org", entity.parent), "part_of", unit, origin="user", confirmed=True)
        elif isinstance(entity, Theme):
            if entity.merged_into:
                self._edge(
                    nid, node_id("theme", entity.merged_into), "merged_into", unit, origin="user", confirmed=True
                )
            if entity.state == "proposed":
                self._review("proposed_theme", unit, entity.id, seen, {"theme": entity.id, "name": entity.name})
        elif isinstance(entity, Thread):
            summary = self._parse(files.one("thread_summary"), lambda f: ThreadSummary.parse(f.data, where=f.key))
            now = self._parse(files.one("thread_now"), lambda f: ThreadNow.parse(f.data, where=f.key))
            self.conn.execute(
                "INSERT INTO threads(id, name, description, state, pinned, merged_into, summary, summary_through, "
                "now_line, now_through) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity.id,
                    entity.name,
                    entity.description,
                    entity.state,
                    int(entity.pinned),
                    entity.merged_into,
                    summary.summary_md if summary else None,
                    summary.through_entry if summary else None,
                    now.line if now else None,
                    now.through_entry if now else None,
                ),
            )
            if entity.merged_into:
                self._edge(
                    nid, node_id("thread", entity.merged_into), "merged_into", unit, origin="user", confirmed=True
                )
            if entity.state == "proposed":
                self._review("proposed_thread", unit, entity.id, seen, {"thread": entity.id, "name": entity.name})

    def refresh_thread_activity(self) -> None:
        self.conn.execute(
            """
            UPDATE threads SET
              last_activity = (
                SELECT MAX(f.created_at) FROM edges e JOIN fragments f ON e.src = 'frag:' || f.id
                WHERE e.type = 'in_thread' AND e.dst = 'thread:' || threads.id
              ),
              last_entry = (
                SELECT f.entry_id FROM edges e JOIN fragments f ON e.src = 'frag:' || f.id
                WHERE e.type = 'in_thread' AND e.dst = 'thread:' || threads.id
                ORDER BY f.created_at DESC, f.entry_id DESC LIMIT 1
              )
            """
        )


def unit_of(key: str, data: bytes) -> str:
    """Unit for an entry or entity file, read from its content. Raises FormatError."""
    info = keys.classify(key)
    assert info is not None
    if info.unit:
        return info.unit
    text = data.decode("utf-8", errors="replace")
    if info.role == "entry":
        return keys.entry_unit(Entry.parse(text, where=key).id)
    entity = parse_entity(text, where=key, default_type=info.entity_type)
    return keys.entity_unit(entity.type, entity.id)
