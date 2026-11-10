"""Reads over the index.

The index stores what each layer wrote. What is true now is computed when
read, by `World`: merges are followed to the surviving entity, statements
moved by a correction note are counted in their new thread, and each statement
gets a status from the relations and retractions that apply to it.
"""

from __future__ import annotations

import difflib
import json
import sqlite3
from dataclasses import dataclass
from functools import cached_property

from brain.core.formats.records import OPEN_KINDS, EntityRecord, norm_name

# Statements shown on pages and given to models as knowledge. Corrections are
# instructions about the wiki, not knowledge.
CONTENT_KINDS = ("fact", "decision", "question", "action", "idea")


@dataclass(frozen=True)
class Ent:
    id: str
    type: str
    name: str
    status: str
    merged_into: str | None
    created_at: str
    data: str

    @cached_property
    def record(self) -> EntityRecord:
        return EntityRecord.model_validate_json(self.data)


@dataclass
class Stmt:
    id: str
    note_id: str
    seq: int
    text: str
    kind: str
    created_at: str
    thread: str | None  # the surviving thread it counts toward
    status: str = "current"  # current|superseded|answered|repeat|retracted
    by: str | None = None  # the statement that superseded, answered, or is repeated by this one

    @property
    def is_open(self) -> bool:
        return self.kind in OPEN_KINDS and self.status == "current"

    @property
    def is_current(self) -> bool:
        return self.status == "current"


@dataclass(frozen=True)
class LogEvent:
    at: str
    source: str
    kind: str
    text: str
    entity_id: str | None


class World:
    """A consistent reading of the index. Build one per operation; it caches what it loads."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.entities: dict[str, Ent] = {
            r["id"]: Ent(*r)
            for r in conn.execute("SELECT id, type, name, status, merged_into, created_at, data FROM entities")
        }
        self._load_effects()

    # --- entities ----------------------------------------------------------

    def root(self, entity_id: str | None) -> str | None:
        seen: set[str] = set()
        while entity_id in self.entities and entity_id not in seen:
            seen.add(entity_id)
            nxt = self.entities[entity_id].merged_into
            if not nxt or nxt not in self.entities:
                return entity_id
            entity_id = nxt
        return entity_id if entity_id in self.entities else None

    def get(self, entity_id: str | None) -> Ent | None:
        r = self.root(entity_id)
        return self.entities[r] if r else None

    @cached_property
    def _members(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for eid in self.entities:
            r = self.root(eid)
            if r:
                out.setdefault(r, []).append(eid)
        return out

    def members(self, root_id: str) -> list[str]:
        return self._members.get(root_id, [])

    def roots(self, etype: str | None = None, *, with_provisional: bool = True) -> list[Ent]:
        out = [self.entities[r] for r in self._members]
        if etype:
            out = [e for e in out if e.type == etype]
        if not with_provisional:
            out = [e for e in out if e.status != "provisional"]
        return sorted(out, key=lambda e: e.name.casefold())

    def names_of(self, root_id: str) -> list[str]:
        seen: dict[str, str] = {}
        for m in [root_id, *[x for x in self.members(root_id) if x != root_id]]:
            for n in self.entities[m].record.all_names():
                seen.setdefault(norm_name(n), n)
        return list(seen.values())

    def by_name(self, name: str, etype: str | None = None) -> list[Ent]:
        """Surviving entities one of whose names is exactly this."""
        rows = self.conn.execute("SELECT entity_id FROM names WHERE name = ?", (norm_name(name),)).fetchall()
        roots = {self.root(r[0]) for r in rows}
        out = [self.entities[r] for r in roots if r]
        return sorted((e for e in out if etype is None or e.type == etype), key=lambda e: e.id)

    def similar(self, name: str, etype: str | None = None, *, limit: int = 8, floor: float = 0.55) -> list[Ent]:
        """Surviving entities with a name close to this one: similar spelling, or sharing a whole word."""
        target = norm_name(name)
        words = set(target.split())
        scored: dict[str, float] = {}
        for e in self.roots(etype):
            best = 0.0
            for n in self.names_of(e.id):
                nn = norm_name(n)
                score = difflib.SequenceMatcher(None, target, nn).ratio()
                shared = words & set(nn.split())
                if shared and any(len(w) > 2 for w in shared):
                    score = max(score, 0.6 + 0.1 * len(shared))
                best = max(best, score)
            if best >= floor:
                scored[e.id] = best
        ranked = sorted(scored, key=lambda i: (-scored[i], i))[:limit]
        return [self.entities[i] for i in ranked]

    # --- effects -----------------------------------------------------------

    def _load_effects(self) -> None:
        c = self.conn
        self.retracted: set[str] = set()
        self.moves: dict[str, str] = {}
        ops = c.execute(
            "SELECT statement_id, op, targets, moved FROM operations ORDER BY created_at DESC, id DESC"
        ).fetchall()
        for sid, op, targets, _ in ops:  # newest first: a retraction that was itself retracted does nothing
            if op == "retract" and sid not in self.retracted:
                self.retracted.update(json.loads(targets))
        for sid, op, targets, moved in reversed(ops):  # oldest first: the latest move wins
            if op == "not_part_of" and sid not in self.retracted:
                t = json.loads(targets)
                for m in json.loads(moved):
                    self.moves[m] = t[0]
        self.by_rel: dict[str, dict[str, str]] = {"supersedes": {}, "answers": {}, "repeats": {}}
        for src, dst, rel in c.execute("SELECT src, dst, rel FROM relations ORDER BY src"):
            if src in self.retracted:
                continue
            if rel == "repeats":
                self.by_rel["repeats"][src] = dst
            else:
                self.by_rel[rel][dst] = src

    def _stmt(self, r: sqlite3.Row) -> Stmt:
        sid = r["id"]
        s = Stmt(
            sid,
            r["note_id"],
            r["seq"],
            r["text"],
            r["kind"],
            r["created_at"],
            self.root(self.moves.get(sid, r["thread_id"])),
        )
        if sid in self.retracted:
            s.status = "retracted"
        elif sid in self.by_rel["supersedes"]:
            s.status, s.by = "superseded", self.by_rel["supersedes"][sid]
        elif sid in self.by_rel["answers"]:
            s.status, s.by = "answered", self.by_rel["answers"][sid]
        elif sid in self.by_rel["repeats"]:
            s.status, s.by = "repeat", self.by_rel["repeats"][sid]
        return s

    # --- statements --------------------------------------------------------

    def select(self, where: str = "1", params: tuple | list = (), *, content: bool = True) -> list[Stmt]:
        rows = self.conn.execute(
            f"SELECT id, note_id, seq, text, kind, thread_id, created_at FROM statements s WHERE {where} "
            "ORDER BY created_at, note_id, seq",
            list(params),
        ).fetchall()
        out = [self._stmt(r) for r in rows]
        return [s for s in out if s.kind in CONTENT_KINDS] if content else out

    def by_id(self, ids: list[str] | set[str]) -> dict[str, Stmt]:
        ids = list(ids)
        if not ids:
            return {}
        return {s.id: s for s in self.select(f"id IN ({','.join('?' * len(ids))})", ids, content=False)}

    def of_note(self, note_id: str) -> list[Stmt]:
        return self.select("note_id = ?", (note_id,), content=False)

    def of_entity(self, root_id: str) -> list[Stmt]:
        """Every content statement that counts toward an entity, oldest first, whatever its status."""
        e = self.entities.get(root_id)
        if e is None:
            return []
        members = self.members(root_id)
        marks = ",".join("?" * len(members))
        if e.type == "thread":
            moved_in = [sid for sid, t in self.moves.items() if self.root(t) == root_id]
            where = f"thread_id IN ({marks})"
            params: list = list(members)
            if moved_in:
                where = f"({where} OR id IN ({','.join('?' * len(moved_in))}))"
                params += moved_in
            return [s for s in self.select(where, params) if s.thread == root_id]
        return self.select(f"id IN (SELECT statement_id FROM mentions WHERE entity_id IN ({marks}))", members)

    def entities_of(self, statement_ids: list[str]) -> dict[str, list[str]]:
        """Surviving entities each statement mentions."""
        out: dict[str, list[str]] = {s: [] for s in statement_ids}
        if not statement_ids:
            return out
        marks = ",".join("?" * len(statement_ids))
        for sid, eid in self.conn.execute(
            f"SELECT statement_id, entity_id FROM mentions WHERE statement_id IN ({marks})", statement_ids
        ):
            r = self.root(eid)
            if r and r not in out[sid]:
                out[sid].append(r)
        return out

    def open_items(self) -> list[Stmt]:
        closed = {e.id for e in self.entities.values() if e.status == "closed"}
        items = self.select("kind IN ('question', 'action')")
        return [s for s in items if s.is_open and s.thread not in closed]

    def last_activity(self, root_id: str) -> str | None:
        members = self.members(root_id)
        if not members:
            return None
        marks = ",".join("?" * len(members))
        e = self.entities[root_id]
        if e.type == "thread":
            stmts = [s for s in self.of_entity(root_id) if s.status != "retracted"]
            return stmts[-1].created_at if stmts else None
        row = self.conn.execute(
            f"SELECT MAX(created_at) FROM mentions WHERE entity_id IN ({marks})", members
        ).fetchone()
        return row[0]

    def note_count(self, root_id: str) -> int:
        members = self.members(root_id)
        marks = ",".join("?" * len(members))
        e = self.entities[root_id]
        if e.type == "thread":
            return len({s.note_id for s in self.of_entity(root_id)})
        return self.conn.execute(
            f"SELECT COUNT(DISTINCT note_id) FROM mentions WHERE entity_id IN ({marks})", members
        ).fetchone()[0]

    def related(self, root_id: str, *, limit: int = 12) -> list[Ent]:
        """Other entities that appear in the same statements, most frequent first."""
        counts: dict[str, int] = {}
        stmts = self.of_entity(root_id)
        for ents in self.entities_of([s.id for s in stmts]).values():
            for e in ents:
                counts[e] = counts.get(e, 0) + 1
        for s in stmts:
            if s.thread:
                counts[s.thread] = counts.get(s.thread, 0) + 1
        counts.pop(root_id, None)
        ranked = sorted(counts, key=lambda i: (-counts[i], i))[:limit]
        return [self.entities[i] for i in ranked if self.entities[i].status != "provisional"]


def pending_notes(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT id FROM notes WHERE done = 0 ORDER BY created_at, id")]


def note_key(conn: sqlite3.Connection, note_id: str) -> str | None:
    row = conn.execute("SELECT key FROM notes WHERE id = ?", (note_id,)).fetchone()
    return row[0] if row else None


def note_dates(conn: sqlite3.Connection, ids: list[str] | set[str]) -> dict[str, tuple[str, str]]:
    """Note id -> (created_at, label)."""
    ids = list(ids)
    if not ids:
        return {}
    rows = conn.execute(
        f"SELECT id, created_at, COALESCE(title, substr(body, 1, 80)) FROM notes WHERE id IN ({','.join('?' * len(ids))})",
        ids,
    ).fetchall()
    return {r[0]: (r[1], r[2].strip().splitlines()[0] if r[2].strip() else r[0]) for r in rows}


def events(conn: sqlite3.Connection, *, limit: int = 200, entity: list[str] | None = None) -> list[LogEvent]:
    sql = "SELECT at, source, kind, text, entity_id FROM events"
    params: list = []
    if entity:
        sql += f" WHERE entity_id IN ({','.join('?' * len(entity))})"
        params += entity
    sql += " ORDER BY at DESC, source DESC, seq LIMIT ?"
    params.append(limit)
    return [LogEvent(*r) for r in conn.execute(sql, params)]


def vectors(conn: sqlite3.Connection, ids: list[str]) -> dict[str, bytes]:
    if not ids:
        return {}
    rows = conn.execute(
        f"SELECT e.owner_id, v.embedding FROM embeddings e JOIN vec_items v ON v.rowid = e.rowid "
        f"WHERE e.owner_id IN ({','.join('?' * len(ids))})",
        ids,
    ).fetchall()
    return {r[0]: bytes(r[1]) for r in rows}
