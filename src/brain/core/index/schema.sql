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

CREATE TABLE nodes (
  id         TEXT PRIMARY KEY,          -- 'entry:e-…', 'frag:f-…', 'person:jane-doe', …
  type       TEXT NOT NULL,             -- entry|fragment|person|org|theme|thread
  label      TEXT NOT NULL,
  created_at TEXT,
  updated_at TEXT,
  data       TEXT NOT NULL DEFAULT '{}',
  owner      TEXT NOT NULL
);
CREATE INDEX nodes_type ON nodes(type);
CREATE INDEX nodes_owner ON nodes(owner);

CREATE TABLE entries (
  id           TEXT PRIMARY KEY,
  key          TEXT NOT NULL UNIQUE,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  title        TEXT,
  body         TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  extraction   TEXT NOT NULL            -- current|pending|stale
);
CREATE INDEX entries_created ON entries(created_at);

CREATE TABLE fragments (
  id            TEXT PRIMARY KEY,
  entry_id      TEXT NOT NULL,
  kind          TEXT NOT NULL,
  text          TEXT NOT NULL,
  status        TEXT NOT NULL,          -- confirmed|speculative|overcome_by_events
  status_origin TEXT NOT NULL,          -- llm|user
  dream         INTEGER NOT NULL DEFAULT 0,
  state         TEXT,                   -- open|closed (questions and action items)
  closed_note   TEXT,
  obe_date      TEXT,
  obe_reason    TEXT,
  span_start    INTEGER,
  span_end      INTEGER,
  quote         TEXT,
  created_at    TEXT NOT NULL           -- the entry's timestamp
);
CREATE INDEX fragments_entry ON fragments(entry_id);
CREATE INDEX fragments_kind_status ON fragments(kind, status);

CREATE TABLE threads (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  state         TEXT NOT NULL,
  pinned        INTEGER NOT NULL,
  merged_into   TEXT,
  summary       TEXT,
  last_activity TEXT                    -- max(created_at) of linked fragments
);

CREATE TABLE aliases (
  alias   TEXT NOT NULL,                -- casefolded, whitespace-normalized
  node_id TEXT NOT NULL,
  owner   TEXT NOT NULL,
  PRIMARY KEY (alias, node_id)
);
CREATE INDEX aliases_owner ON aliases(owner);

CREATE TABLE edges (
  id          INTEGER PRIMARY KEY,
  src         TEXT NOT NULL,
  dst         TEXT NOT NULL,
  type        TEXT NOT NULL,            -- contains|mentions|about|in_thread|member_of|part_of|merged_into|answers
  weight      REAL NOT NULL DEFAULT 1.0,
  origin      TEXT NOT NULL,            -- llm|user
  confirmed   INTEGER NOT NULL,         -- 1 if you created or confirmed it
  entry_id    TEXT,                     -- provenance
  fragment_id TEXT,
  valid_from  TEXT,
  valid_to    TEXT,
  owner       TEXT NOT NULL
);
CREATE UNIQUE INDEX edges_uniq ON edges(src, dst, type, IFNULL(fragment_id, ''), IFNULL(valid_from, ''));
CREATE INDEX edges_src ON edges(src, type);
CREATE INDEX edges_dst ON edges(dst, type);
CREATE INDEX edges_owner ON edges(owner);

CREATE TABLE review_items (
  id         TEXT PRIMARY KEY,          -- stable hash of (kind, owner, subject)
  kind       TEXT NOT NULL,             -- new_person|new_org|ambiguous_mention|proposed_theme|proposed_thread|proposed_answer|orphaned_decision
  entry_id   TEXT,
  created_at TEXT NOT NULL,
  payload    TEXT NOT NULL,             -- JSON
  owner      TEXT NOT NULL
);
CREATE INDEX review_owner ON review_items(owner);

CREATE TABLE embeddings (               -- rowid is shared with vec_items
  rowid        INTEGER PRIMARY KEY,
  owner_id     TEXT NOT NULL UNIQUE,    -- entry or fragment id
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
