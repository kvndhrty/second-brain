-- Derived index. Every row is rebuilt from files in the store; nothing here is
-- the only copy of anything. `owner` is the unit (see core/keys.py) whose files
-- produced the row, so a changed file rebuilds exactly the rows that depend on it.

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE objects (                  -- manifest: what the index was built from
  key   TEXT PRIMARY KEY,
  etag  TEXT NOT NULL,
  size  INTEGER NOT NULL,
  mtime TEXT,
  unit  TEXT                            -- NULL for keys the index ignores
);
CREATE INDEX objects_unit ON objects(unit);

CREATE TABLE problems (                 -- files that could not be parsed
  key     TEXT PRIMARY KEY,
  message TEXT NOT NULL
);

CREATE TABLE notes (
  id           TEXT PRIMARY KEY,
  key          TEXT NOT NULL UNIQUE,
  created_at   TEXT NOT NULL,
  title        TEXT,
  about        TEXT,
  body         TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  layers       TEXT NOT NULL,           -- comma-separated layers that are present
  done         INTEGER NOT NULL         -- 1 when every layer is present and current
);
CREATE INDEX notes_created ON notes(created_at);
CREATE INDEX notes_done ON notes(done);

CREATE TABLE statements (
  id         TEXT PRIMARY KEY,
  note_id    TEXT NOT NULL,
  seq        INTEGER NOT NULL,
  text       TEXT NOT NULL,
  quote      TEXT NOT NULL,
  kind       TEXT NOT NULL,             -- fact|decision|question|action|idea|correction
  thread_id  TEXT,                      -- as filed by layer 5; moves and merges are applied when read
  created_at TEXT NOT NULL              -- the note's timestamp
);
CREATE INDEX statements_note ON statements(note_id);
CREATE INDEX statements_thread ON statements(thread_id);

CREATE TABLE entities (
  id          TEXT PRIMARY KEY,
  type        TEXT NOT NULL,            -- person|org|topic|thread
  name        TEXT NOT NULL,
  status      TEXT NOT NULL,            -- provisional|active|closed
  merged_into TEXT,
  created_at  TEXT NOT NULL,
  data        TEXT NOT NULL             -- the whole record, JSON
);
CREATE INDEX entities_type ON entities(type);

CREATE TABLE names (
  name      TEXT NOT NULL,              -- casefolded, whitespace-normalized
  entity_id TEXT NOT NULL,
  owner     TEXT NOT NULL,
  PRIMARY KEY (name, entity_id)
);
CREATE INDEX names_owner ON names(owner);

CREATE TABLE mentions (                 -- a statement (or a whole note, statement_id '') names an entity
  note_id      TEXT NOT NULL,
  statement_id TEXT NOT NULL,
  entity_id    TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  owner        TEXT NOT NULL,
  PRIMARY KEY (statement_id, entity_id, note_id)
);
CREATE INDEX mentions_entity ON mentions(entity_id);
CREATE INDEX mentions_owner ON mentions(owner);

CREATE TABLE relations (
  src    TEXT NOT NULL,                 -- the later statement
  dst    TEXT NOT NULL,                 -- the earlier one
  rel    TEXT NOT NULL,                 -- answers|supersedes|repeats
  origin TEXT NOT NULL,                 -- note|dream
  owner  TEXT NOT NULL,
  PRIMARY KEY (src, dst, rel)
);
CREATE INDEX relations_dst ON relations(dst);
CREATE INDEX relations_owner ON relations(owner);

CREATE TABLE operations (               -- instructions that came from correction notes
  id           INTEGER PRIMARY KEY,
  note_id      TEXT NOT NULL,
  statement_id TEXT NOT NULL,
  op           TEXT NOT NULL,
  targets      TEXT NOT NULL,           -- JSON list
  name         TEXT,
  moved        TEXT NOT NULL,           -- JSON list of statements moved to targets[0]
  created_at   TEXT NOT NULL,
  owner        TEXT NOT NULL
);
CREATE INDEX operations_owner ON operations(owner);

CREATE TABLE events (                   -- the change log
  id        INTEGER PRIMARY KEY,
  at        TEXT NOT NULL,
  source    TEXT NOT NULL,              -- note id or 'dream:<run>'
  seq       INTEGER NOT NULL,
  kind      TEXT NOT NULL,
  text      TEXT NOT NULL,
  entity_id TEXT,
  owner     TEXT NOT NULL
);
CREATE INDEX events_at ON events(at);
CREATE INDEX events_owner ON events(owner);

CREATE TABLE pages (
  entity_id  TEXT PRIMARY KEY,
  updated_at TEXT NOT NULL,
  now        TEXT,
  summary    TEXT,
  data       TEXT NOT NULL,             -- the whole page file, JSON
  owner      TEXT NOT NULL
);

CREATE TABLE embeddings (               -- rowid is shared with vec_items
  rowid        INTEGER PRIMARY KEY,
  owner_id     TEXT NOT NULL UNIQUE,    -- note or statement id
  model        TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  owner        TEXT NOT NULL
);
CREATE INDEX embeddings_owner ON embeddings(owner);

CREATE VIRTUAL TABLE fts USING fts5(
  item_id UNINDEXED, item_type UNINDEXED, owner UNINDEXED, title, body,
  tokenize = 'porter unicode61'
);

-- vec_items (sqlite-vec) is created on first use, once the dimension is known.
