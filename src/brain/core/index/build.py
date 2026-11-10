"""Turn the files of one unit into index rows.

A unit is a note (the note file, its layer files, its embeddings), an entity
(its record and its page), or a dream run. Each rebuild deletes every row the
unit owns and inserts them again, so the result depends only on the current
files, never on the order of changes.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from brain.core import keys
from brain.core.formats.note import Note
from brain.core.formats.records import (
    LAYER_MODELS,
    DreamRun,
    EmbeddingFile,
    EntityRecord,
    Event,
    FilingFile,
    LayerFile,
    LinksFile,
    PageFile,
    StateFile,
    decode_vector,
    norm_name,
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


OWNED = ("names", "mentions", "relations", "operations", "events", "pages", "embeddings", "fts")


def in_statement(surface: str, text: str, quote: str) -> bool:
    s = norm_name(surface)
    return s in norm_name(text) or s in norm_name(quote)


class UnitBuilder:
    def __init__(self, db: IndexDB, embed_model: str) -> None:
        self.db = db
        self.conn: sqlite3.Connection = db.conn
        self.embed_slug = keys.model_slug(embed_model) if embed_model else None

    def clear(self, unit: str) -> None:
        c = self.conn
        kind, _, ident = unit.partition(":")
        if kind == "note":
            c.execute("DELETE FROM statements WHERE note_id = ?", (ident,))
            c.execute("DELETE FROM notes WHERE id = ?", (ident,))
        if kind == "entity":
            c.execute("DELETE FROM entities WHERE id = ?", (ident,))
        if self.db.has_vec():
            c.execute("DELETE FROM vec_items WHERE rowid IN (SELECT rowid FROM embeddings WHERE owner = ?)", (unit,))
        for table in OWNED:
            c.execute(f"DELETE FROM {table} WHERE owner = ?", (unit,))

    def _problem(self, key: str, message: str) -> None:
        self.conn.execute(
            "INSERT INTO problems(key, message) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET message = excluded.message",
            (key, message),
        )

    def _parse(self, f: UnitFile | None, model):
        if f is None:
            return None
        try:
            return model.parse(f.data, where=f.key)
        except FormatError as e:
            self._problem(f.key, str(e))
            return None

    def _events(self, events: list[Event], *, at: str, source: str, owner: str, start: int = 0) -> int:
        for i, ev in enumerate(events, start):
            self.conn.execute(
                "INSERT INTO events(at, source, seq, kind, text, entity_id, owner) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (at, source, i, ev.kind, ev.text, ev.entity, owner),
            )
        return start + len(events)

    # --- notes -------------------------------------------------------------

    def build(self, files: UnitFiles) -> None:
        kind = files.unit.partition(":")[0]
        {"note": self.build_note, "entity": self.build_entity, "dream": self.build_dream}[kind](files)

    def build_note(self, files: UnitFiles) -> None:
        unit = files.unit
        nf = files.one("note")
        if nf is None:
            return  # generated files without their note: nothing to show
        try:
            note = Note.parse(nf.data.decode(), where=nf.key)
        except (FormatError, UnicodeDecodeError) as e:
            self._problem(nf.key, str(e))
            return
        layers: dict[str, LayerFile] = {}
        for f in files.by_role.get("layer", []):
            info = keys.classify(f.key)
            parsed = self._parse(f, LAYER_MODELS[info.layer]) if info and info.layer else None
            if parsed is not None and info and info.layer:
                layers[info.layer] = parsed
        split = layers.get("split")
        current = split is not None and split.input_hash == note.content_hash
        if not current:
            layers = {}
        created = note.created.isoformat()
        c = self.conn
        c.execute(
            "INSERT INTO notes(id, key, created_at, title, about, body, content_hash, layers, done) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                note.id,
                nf.key,
                created,
                note.title,
                note.about,
                note.body,
                note.content_hash,
                ",".join(k for k in keys.LAYERS if k in layers),
                int(all(k in layers for k in keys.LAYERS)),
            ),
        )
        c.execute(
            "INSERT INTO fts(item_id, item_type, owner, title, body) VALUES (?, 'note', ?, ?, ?)",
            (note.id, unit, note.title or "", note.body),
        )
        seq = 0
        texts: dict[str, str] = {note.id: note.body}
        statements = getattr(split, "statements", []) if current else []
        kinds = getattr(layers.get("classify"), "kinds", {})
        filing = layers.get("filing")
        threads = filing.threads if isinstance(filing, FilingFile) else {}
        for n, s in enumerate(statements):
            texts[s.id] = s.text
            c.execute(
                "INSERT OR REPLACE INTO statements(id, note_id, seq, text, quote, kind, thread_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (s.id, note.id, n, s.text, s.quote, kinds.get(s.id, "fact"), threads.get(s.id), created),
            )
            c.execute(
                "INSERT INTO fts(item_id, item_type, owner, title, body) VALUES (?, 'statement', ?, '', ?)",
                (s.id, unit, s.text),
            )
        links = layers.get("links")
        if isinstance(links, LinksFile):
            for link in links.links:
                hit = [s.id for s in statements if in_statement(link.surface, s.text, s.quote)] or [""]
                for sid in hit:
                    c.execute(
                        "INSERT OR IGNORE INTO mentions(note_id, statement_id, entity_id, created_at, owner) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (note.id, sid, link.entity, created, unit),
                    )
        state = layers.get("state")
        if isinstance(state, StateFile):
            for r in state.relations:
                c.execute(
                    "INSERT OR IGNORE INTO relations(src, dst, rel, origin, owner) VALUES (?, ?, ?, 'note', ?)",
                    (r.src, r.dst, r.rel, unit),
                )
            for op in state.operations:
                c.execute(
                    "INSERT INTO operations(note_id, statement_id, op, targets, name, moved, created_at, owner) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        note.id,
                        op.statement,
                        op.op,
                        json.dumps(op.targets),
                        op.name,
                        json.dumps(op.moved),
                        created,
                        unit,
                    ),
                )
        for name in keys.LAYERS:
            if name in layers:
                seq = self._events(layers[name].events, at=created, source=note.id, owner=unit, start=seq)
        self._build_embeddings(unit, files, texts)

    def _build_embeddings(self, unit: str, files: UnitFiles, texts: dict[str, str]) -> None:
        from brain.core.formats.note import content_hash

        for f in files.by_role.get("embedding", []):
            info = keys.classify(f.key)
            if info is None or info.model_slug != self.embed_slug:
                continue
            emb = self._parse(f, EmbeddingFile)
            if emb is None or not self.db.ensure_vec(emb.dim):
                continue
            for item in emb.items:
                if item.id not in texts or item.hash != content_hash(texts[item.id]):
                    continue  # stale: the text changed after it was embedded
                cur = self.conn.execute(
                    "INSERT INTO embeddings(owner_id, model, content_hash, owner) VALUES (?, ?, ?, ?)",
                    (item.id, emb.model, item.hash, unit),
                )
                self.conn.execute(
                    "INSERT INTO vec_items(rowid, embedding) VALUES (?, ?)",
                    (cur.lastrowid, vec_blob(decode_vector(item.vec))),
                )

    # --- entities ----------------------------------------------------------

    def build_entity(self, files: UnitFiles) -> None:
        unit = files.unit
        e = self._parse(files.one("entity"), EntityRecord)
        if e is None:
            return
        c = self.conn
        c.execute(
            "INSERT INTO entities(id, type, name, status, merged_into, created_at, data) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (e.id, e.type, e.name, e.status, e.merged_into, e.created_at.isoformat(), e.model_dump_json()),
        )
        for name in e.all_names():
            c.execute(
                "INSERT OR IGNORE INTO names(name, entity_id, owner) VALUES (?, ?, ?)", (norm_name(name), e.id, unit)
            )
        c.execute(
            "INSERT INTO fts(item_id, item_type, owner, title, body) VALUES (?, 'entity', ?, ?, ?)",
            (e.id, unit, e.name, " ".join(e.names)),
        )
        page = self._parse(files.one("page"), PageFile)
        if page is not None:
            now = page.sections.get("now")
            summary = page.sections.get("summary")
            c.execute(
                "INSERT INTO pages(entity_id, updated_at, now, summary, data, owner) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    e.id,
                    page.updated_at.isoformat(),
                    now.text if now else None,
                    summary.text if summary else None,
                    page.model_dump_json(),
                    unit,
                ),
            )

    # --- dream runs --------------------------------------------------------

    def build_dream(self, files: UnitFiles) -> None:
        run = self._parse(files.one("dream_run"), DreamRun)
        if run is None:
            return
        for r in run.relations:
            self.conn.execute(
                "INSERT OR IGNORE INTO relations(src, dst, rel, origin, owner) VALUES (?, ?, ?, 'dream', ?)",
                (r.src, r.dst, r.rel, files.unit),
            )
        at = (run.finished_at or run.started_at).isoformat()
        self._events(run.events, at=at, source=f"dream:{run.run_id}", owner=files.unit)


def unit_of(key: str, data: bytes) -> str | None:
    """The unit of a note file, read from its content. None for a bare file not yet adopted."""
    text = data.decode()
    if not text.startswith("---"):
        return None
    return keys.note_unit(Note.parse(text, where=key).id)
