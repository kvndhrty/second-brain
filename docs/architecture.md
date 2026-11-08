# brain — architecture

This describes the design as built: milestones 1 to 5, then temporal context,
the injected context, and dreaming (section 6). Section 10 records the
decisions made along the way and why.

The memory layers follow Anthropic's post [Components of agent
memory](https://www.anthropic.com/research/components-of-agent-memory):

| Post | brain |
|---|---|
| Transcripts (ground truth) | Entries, never rewritten by a model |
| Memory notes | Fragments, written by the `extract` step |
| Representations | Thread, person, org, and theme files; thread summaries |
| Injected context | `brain context`: a short, dated summary loaded into agent sessions |
| Dreaming | `brain dream`: an offline pass that re-checks derived notes against the entries |

## 1. Summary

- Files in a store (local directory or S3 prefix) are the source of truth.
- A SQLite database on local disk is a derived index: graph tables, FTS5, and
  sqlite-vec vectors. `brain reindex` rebuilds it from the files without
  calling any model.
- One Python package. `brain.core` holds all behaviour and has no imports of
  Typer, FastAPI, or MCP. `brain.cli`, `brain.web`, and `brain.mcp` call a single
  façade class (`Brain`) and only format input and output.
- `brain start` is the one long-running process: web UI, HTTP API, MCP over
  HTTP, and a background worker that processes pending entries. All other
  commands are short-lived CLI calls.
- All model and S3 traffic uses one TLS configuration (same CA and client cert
  for both).
- Every note given to a model carries its date and its age relative to today,
  and derived text carries the as-of date of the newest entry it covers, so the
  model can tell newer information from older.

## 2. Package layout

```
second-brain/                     repository (public on GitHub)
├─ pyproject.toml                 dist name "brain-journal"; console script "brain"
├─ src/brain/
│  ├─ core/
│  │  ├─ config.py                settings model, load order, XDG paths
│  │  ├─ keys.py                  every storage key pattern, in one place
│  │  ├─ formats/                 pydantic models + parse/serialize for each file type
│  │  │  ├─ entry.py              (this directory is the durable contract)
│  │  │  ├─ entity.py
│  │  │  ├─ decisions.py
│  │  │  └─ sidecars.py
│  │  ├─ storage/
│  │  │  ├─ base.py               Storage protocol, ObjectInfo, errors
│  │  │  ├─ local.py
│  │  │  ├─ s3.py
│  │  │  ├─ probe.py              tests whether conditional writes are enforced
│  │  │  └─ lease.py
│  │  ├─ tls.py                   one SSLContext for httpx and boto3
│  │  ├─ vault.py                 typed file access over Storage; takes the lease on write
│  │  ├─ index/
│  │  │  ├─ schema.sql
│  │  │  ├─ db.py                 connection setup, sqlite-vec loading, capability checks
│  │  │  ├─ sync.py               store listing vs. index manifest
│  │  │  ├─ build.py              file -> rows (used by both sync and reindex)
│  │  │  ├─ search.py             FTS5 + vector + rank fusion
│  │  │  └─ graph.py              recursive-CTE traversals
│  │  ├─ llm/
│  │  │  ├─ client.py             OpenAI-compatible HTTP client, embeddings, doctor helpers
│  │  │  ├─ models.py             LLM: PydanticAI structured calls in tool/native/prompted mode
│  │  │  ├─ schemas.py            typed outputs requested from the models
│  │  │  ├─ prompts.py            loads prompts, computes version stamps
│  │  │  └─ fake.py               FakeLLM (scripted) and FakeOpenAI (HTTP) for tests
│  │  ├─ pipeline/
│  │  │  ├─ extract.py
│  │  │  ├─ match.py              carries fragment IDs across re-extraction
│  │  │  ├─ embed.py
│  │  │  ├─ resolve.py            entity resolution and answer proposals
│  │  │  ├─ threads.py            incremental thread summaries
│  │  │  ├─ runner.py             runs pending work; idempotent per entry
│  │  │  └─ worker.py             background loop used by `brain start` (milestone 4)
│  │  ├─ review.py                review queue: items and the decisions that settle them
│  │  ├─ answers.py               ask and brief: retrieval, synthesis, citation checks
│  │  ├─ services.py              Brain façade (the only API front-ends use)
│  │  └─ doctor.py                checks returning structured results
│  ├─ prompts/                    extract.md, resolve.md, thread_summary.md, brief.md, ask.md
│  ├─ cli/                        Typer app; output via rich
│  ├─ web/                        FastAPI app; static/ holds the built frontend
│  └─ mcp/                        MCP tools, served over HTTP (in `start`) or stdio
├─ frontend/                      Vite + React + TypeScript
├─ tests/
│  ├─ contract/                   storage contract, run against local + S3
│  ├─ index/                      schema, sync, reindex equivalence
│  ├─ pipeline/                   full pipeline with the fake provider
│  ├─ cli/
│  └─ live/                       real-model tests, gated by BRAIN_LIVE_TESTS=1
└─ docs/
```

A test asserts that nothing under `brain.core` imports `typer`, `fastapi`,
`starlette`, `uvicorn`, or `mcp`.

## 3. Storage layout

All keys are relative to the configured prefix (S3) or root directory (local).

```
entries/2026/10/30/20261030T143205Z-vendor-call.md     authored
people/jane-doe.md                                     curated
orgs/platform-team.md                                  curated
themes/retrieval-eval.md                               curated
threads/gpu-quota-migration.md                         curated
decisions/e-20261030T143205Z.yaml                      curated (your changes to one entry's extracted data)
generated/extractions/e-20261030T143205Z.json          generated by LLM
generated/resolutions/e-20261030T143205Z.json          generated by LLM
generated/embeddings/<model-slug>/e-20261030T143205Z.json
generated/summaries/threads/gpu-quota-migration.json
generated/dreams/now/gpu-quota-migration.json          generated by dreaming: the thread's Now line
generated/dreams/proposals/e-20261030T143205Z.json     generated by dreaming: proposals about this entry's fragments
generated/dreams/runs/20261108T030000Z.json            one dream pass: what it looked at, applied, proposed
generated/dreams/state.json                            dreaming watermarks
state/briefs.json                                      last brief time per node
_system/format.json                                    format version of the store
_system/lease.json
_system/generation.json                                counter bumped on every write
```

Three tiers, with different rules:

| Tier      | Written by        | Can it be regenerated?                       |
|-----------|-------------------|----------------------------------------------|
| authored  | you               | no                                           |
| curated   | you (via the tool, or by hand) | no                              |
| generated | pipeline          | yes, by calling models again (`reextract`)   |

The SQLite index is derived from all three and is rebuilt without models.

Rules:
- Paths are never identity. Every file carries its `id` in frontmatter or JSON.
- Filenames contain no `:`. Timestamps in keys use ISO 8601 basic format
  (`20261030T143205Z`, UTC). The directory date is the UTC date, so keys sort.
- Entry filenames are fixed at creation. Editing a title does not move the file.
- Curated entity IDs are readable slugs assigned once (`jane-doe`, `jane-doe-2`
  on collision). Renaming changes `name`, not `id`.
- Writes are whole-object. Local writes go to a temp file and `os.replace`.

## 4. File formats

Every file has `format: 1`. A reader that sees a higher major version refuses
to write and tells the user to upgrade.

### Entry (`entries/.../*.md`)

```markdown
---
format: 1
id: e-20261030T143205Z
created: 2026-10-30T14:32:05-04:00
updated: 2026-10-30T15:01:12-04:00
title: Vendor call, quota follow-up     # optional
tags: []                                # optional
---
Free text. The LLM never rewrites this file.
```

Edit detection uses the SHA-256 of the body (after normalizing line endings),
not `updated`, so hand edits outside the tool are detected.

### Person / org / theme / thread (markdown + YAML frontmatter)

```markdown
---
format: 1
id: jane-doe
type: person
name: Jane Doe
aliases: [Jane, JD]
role: Program manager
works_on: [GPU capacity planning]
memberships:
  - org: platform-team
    from: 2025-03
    to: null
---
Free notes.
```

```yaml
# org
id: platform-team
type: org
name: Platform Team
aliases: [PT, platform]
parent: infrastructure-office     # optional
```

```yaml
# theme
id: retrieval-eval
type: theme
kind: technique                   # domain | technique
name: Retrieval evaluation
state: active                     # proposed | active | merged
merged_into: null
aliases: []
```

```yaml
# thread
id: gpu-quota-migration
type: thread
name: GPU quota migration
state: active                     # proposed | active | archived | obe | merged
pinned: false
merged_into: null
obe: {date: null, reason: null}
aliases: []                       # old names, kept after rename/merge
```

Curated files are read and written with `ruamel.yaml` in round-trip mode, so
when the tool adds an alias it does not discard your comments or key order.

### Decisions file (`decisions/<entry-id>.yaml`)

For each entry there are up to three files: the entry itself (your text), the
extraction (what the LLM produced from it), and this file (what you changed
about the extraction). The entry is not edited because it is your writing. The
extraction is not edited because `reextract` replaces it. So your changes are
stored here, and the index is built by taking the extraction and applying this
file on top of it. The file exists only for entries where you changed something.

```yaml
format: 1
entry: e-20261030T143205Z
fragments:
  f-e-20261030T143205Z-03:
    status: overcome_by_events
    obe: {date: 2026-11-04, reason: "Vendor withdrew"}
    threads: {add: [gpu-quota-migration], remove: [vendor-eval]}
  f-e-20261030T143205Z-05:
    state: closed
    closed_note: "Answered in the 11-02 sync"
  f-e-20261030T143205Z-06:
    someday: true                 # a big idea not tied to current work
  f-e-20261030T143205Z-07:
    deleted: true                 # you rejected this fragment
  f-e-20261030T143205Z-08:
    answers: [f-e-20261012T090000Z-02]      # accepted: this fragment answers an earlier question
    not_answers: [f-e-20261001T090000Z-04]  # rejected proposal; not asked again
mentions:                         # name as written in this entry -> entity id, per type
  person:
    "Mike": mike-chen
    "the vendor rep": null        # null: not an entity, stop asking
  org:
    "the platform folks": platform-team
  theme:
    "capacity stuff": capacity-planning
```

Unknown keys in this file are errors (reported by `brain doctor`), so a typo
such as `stauts:` is not silently ignored. `someday` was called `dream` before
0.1.0; that key is still read as `someday`, and the file is written with the
new key the next time brain changes it.

When an entry is re-extracted, new fragments are matched to the old fragment
IDs (section 6, Pipeline) so the changes above still apply to the right
fragments. A change whose fragment no longer exists goes to the review queue.

### Extraction sidecar (`generated/extractions/<entry-id>.json`)

```json
{
  "format": 1,
  "entry_id": "e-20261030T143205Z",
  "entry_hash": "sha256:…",
  "model": "extract-model-name",
  "prompt": {"name": "extract", "version": 3, "sha256": "…"},
  "created_at": "2026-10-30T19:02:11Z",
  "fragments": [
    {
      "id": "f-e-20261030T143205Z-03",
      "kind": "decision",
      "text": "Move the eval cluster to the new quota pool in December.",
      "span": [412, 530],
      "quote": "we agreed to move…",
      "status": "confirmed",
      "someday": false,
      "state": null,
      "mentions": [{"surface": "Mike", "type": "person"}],
      "themes": [{"surface": "capacity planning", "kind": "domain"}],
      "threads": [{"thread": "gpu-quota-migration", "weight": 0.9},
                  {"new": {"name": "Vendor evaluation", "description": "…"}, "weight": 0.6}]
    }
  ]
}
```

`span` is character offsets into the body as hashed; `quote` is the exact text
so provenance survives if offsets drift. `state` is `open` for questions and
action items and `null` for other kinds.

### Resolution sidecar (`generated/resolutions/<entry-id>.json`)

`{format, entry_id, extraction_hash, model, prompt, created_at, mentions,
answers}`. `mentions` has one record per distinct mention: `{surface, type:
person|org|theme|thread, decision: link|new|pending|ignore, entity_id?,
proposed?, confidence, candidates: [{entity_id, score}]}`. A `new` or `pending`
record that is not settled in the decisions file becomes a review-queue item.
Proposed people and orgs are not written to `people/` or `orgs/` until you
confirm them. New themes and threads are written with `state: proposed` and
recorded here as `link`. `answers` holds proposals of the form `{fragment,
answers, confidence}` ("this fragment answers that earlier question").

### Embedding sidecar (`generated/embeddings/<model-slug>/<entry-id>.json`)

```json
{"format": 1, "model": "embed-model", "dim": 1024, "dtype": "float32-le",
 "items": [{"id": "e-…", "hash": "sha256:…", "vec": "<base64>"},
           {"id": "f-…-03", "hash": "sha256:…", "vec": "<base64>"}]}
```

Stored per model, so switching embedding models does not delete the old vectors.

### Thread summary (`generated/summaries/threads/<id>.json`)

`{format, thread_id, model, prompt, updated_at, through_entry, summary_md,
citations: [entry ids]}`. `through_entry` lets the summary be updated with only
the entries since then.

### Dream files (`generated/dreams/`)

All of these are generated and can be regenerated by dreaming again.

- `now/<thread-id>.json`: `{format, thread_id, line, citations, through_entry,
  model, prompt, created_at}`. One sentence on where the thread stands, for the
  injected context. It belongs to the thread's index unit, like the summary.
- `proposals/<entry-id>.json`: `{format, entry_id, run_id, model, created_at,
  proposals: [{kind, fragment, other, verdict, reason, confidence, citations,
  checked_through}]}`. Pending proposals about the fragments of one entry, so
  they belong to that entry's index unit. `kind` is `supersede` (fragment is an
  earlier fragment that `other`, a later one, replaced), `answer` (fragment
  answers the open item `other`), or `sweep` (a verdict on the stale item
  `fragment`, with `other` the later fragment it rests on). The index leaves
  out any proposal you have already decided on, so the file needs no clean-up
  when you decide; the next pass that looks at the same thing rewrites it.
- `runs/<run-id>.json`: `{run_id, started_at, finished_at, model, calls,
  max_calls, budget_exhausted, examined, applied, proposed, errors}`.
- `state.json`: per thread, the newest entry each step has seen
  (`rebuilt_through`, `rebuilt_at`, `supersede_through`, `answers_through`),
  plus `last_run`, `last_run_at`, and the entry count at the last pass.

## 5. SQLite index

Location: `$XDG_CACHE_HOME/brain/<store-hash>/index.db` (default `~/.cache`,
on macOS too). Overridable by `[index] path`.

The tool runs on uv-managed Python (python-build-standalone) on macOS and
Linux. That build bundles a recent SQLite with FTS5 and allows
extension loading, which sqlite-vec needs. `doctor` checks the SQLite version,
FTS5, and extension loading, and fails clearly if any is missing (for example,
if someone installs with RHEL's system Python by mistake). Because the index is
derived, there are no migrations: a schema version or embedding-model mismatch
triggers a rebuild.

The full DDL is in `src/brain/core/index/schema.sql`. Beyond the tables
below, every derived table has an `owner` column naming the unit whose files
produced the row. A unit is one entry plus its sidecars and decisions file, or
one entity plus (for threads) its summary. When any file changes, the rows of
the affected units are deleted and rebuilt from the current files, so the
result never depends on the order of changes. `objects.unit` maps each key to
its unit, and `problems` lists files that could not be parsed.

```sql
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- schema_version, store_format, generation, embed_model, embed_dim, last_full_sync

CREATE TABLE objects (                -- manifest: what the index was built from
  key   TEXT PRIMARY KEY,
  etag  TEXT NOT NULL,
  size  INTEGER NOT NULL,
  mtime TEXT
);

CREATE TABLE nodes (                  -- every graph node
  id         TEXT PRIMARY KEY,        -- 'entry:e-…', 'frag:f-…', 'person:jane-doe', …
  type       TEXT NOT NULL,           -- entry|fragment|person|org|theme|thread
  label      TEXT NOT NULL,
  created_at TEXT,
  updated_at TEXT,
  data       TEXT NOT NULL DEFAULT '{}'   -- JSON for fields not used in WHERE clauses
);
CREATE INDEX nodes_type ON nodes(type);

CREATE TABLE entries (
  id           TEXT PRIMARY KEY,
  key          TEXT NOT NULL UNIQUE,
  created_at   TEXT NOT NULL,
  title        TEXT,
  body         TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  extraction   TEXT NOT NULL          -- current|pending|stale|failed
);

CREATE TABLE fragments (
  id            TEXT PRIMARY KEY,
  entry_id      TEXT NOT NULL,
  kind          TEXT NOT NULL,        -- observation|decision|question|idea|claim|action_item
  text          TEXT NOT NULL,
  status        TEXT NOT NULL,        -- confirmed|speculative|overcome_by_events
  status_origin TEXT NOT NULL,        -- llm|user
  someday       INTEGER NOT NULL DEFAULT 0,
  state         TEXT,                 -- open|closed, for questions and action items
  closed_note   TEXT,
  obe_date      TEXT,
  obe_reason    TEXT,
  span_start    INTEGER,
  span_end      INTEGER,
  created_at    TEXT NOT NULL         -- entry's timestamp
);
CREATE INDEX fragments_entry ON fragments(entry_id);
CREATE INDEX fragments_kind_status ON fragments(kind, status);

CREATE TABLE threads (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  state         TEXT NOT NULL,
  pinned        INTEGER NOT NULL,
  merged_into   TEXT,
  last_activity TEXT                  -- max(created_at) of linked fragments
);

CREATE TABLE aliases (
  alias   TEXT NOT NULL,              -- casefolded, whitespace-normalized
  node_id TEXT NOT NULL,
  PRIMARY KEY (alias, node_id)
);

CREATE TABLE edges (
  id          INTEGER PRIMARY KEY,
  src         TEXT NOT NULL,
  dst         TEXT NOT NULL,
  type        TEXT NOT NULL,          -- contains|mentions|about|in_thread|member_of|merged_into|answers
  weight      REAL NOT NULL DEFAULT 1.0,
  origin      TEXT NOT NULL,          -- llm|user
  confirmed   INTEGER NOT NULL,       -- 1 if you created or confirmed it
  entry_id    TEXT,                   -- provenance
  fragment_id TEXT,
  valid_from  TEXT,
  valid_to    TEXT
);
CREATE UNIQUE INDEX edges_uniq ON edges(src, dst, type, IFNULL(fragment_id, ''));
CREATE INDEX edges_dst ON edges(dst, type);

CREATE TABLE review_items (
  id         TEXT PRIMARY KEY,        -- stable hash of (kind, entry, surface)
  kind       TEXT NOT NULL,           -- new_person|new_org|ambiguous_mention|proposed_theme|proposed_thread|proposed_answer|dream_obe|orphaned_decision
  entry_id   TEXT,
  created_at TEXT NOT NULL,
  payload    TEXT NOT NULL            -- JSON
);

CREATE TABLE embeddings (             -- rowid is shared with vec_items
  rowid        INTEGER PRIMARY KEY,
  owner_id     TEXT NOT NULL UNIQUE,  -- entry or fragment id
  model        TEXT NOT NULL,
  content_hash TEXT NOT NULL
);

-- created at build time, once the dimension is known; vectors are
-- L2-normalized on insert so L2 distance ranks the same as cosine
CREATE VIRTUAL TABLE vec_items USING vec0(embedding float[1024]);

CREATE VIRTUAL TABLE fts USING fts5(
  owner_id UNINDEXED, owner_type UNINDEXED, title, body,
  tokenize = 'porter unicode61'
);
```

Every table, including `review_items`, is built from files. The review queue
is the set of unsettled records in resolution sidecars, proposed themes and
threads, and decisions whose fragment no longer exists, so `reindex` rebuilds
it like everything else.

Thread activity: a thread is active if `state = 'active'` and
(`pinned = 1` or `last_activity >= now - inactivity_window`).

Traversal example (nodes within two hops of a person, ignoring structural edges):

```sql
WITH RECURSIVE walk(id, depth) AS (
  SELECT :start, 0
  UNION
  SELECT CASE WHEN e.src = w.id THEN e.dst ELSE e.src END, w.depth + 1
  FROM edges e JOIN walk w ON w.id IN (e.src, e.dst)
  WHERE w.depth < 2 AND e.type <> 'contains'
)
SELECT DISTINCT n.* FROM walk JOIN nodes n ON n.id = walk.id;
```

## 6. Key interfaces

### Storage

```python
@dataclass(frozen=True)
class ObjectInfo:
    key: str
    etag: str            # S3 ETag; local: f"{mtime_ns}-{size}"
    size: int
    mtime: datetime

class Storage(Protocol):
    supports_conditional_writes: bool          # set from the runtime probe, not assumed
    def get(self, key: str) -> bytes: ...      # raises NotFound
    def put(self, key: str, data: bytes, *,
            if_absent: bool = False,           # S3 If-None-Match: *
            if_match: str | None = None        # S3 If-Match: <etag>
            ) -> ObjectInfo: ...               # raises PreconditionFailed
    def list(self, prefix: str) -> Iterator[ObjectInfo]: ...
    def delete(self, key: str) -> None: ...
    def exists(self, key: str) -> bool: ...
```

This keeps the five operations from the spec. The additions are metadata in
`list` results (so sync does not need one HEAD request per object) and the two
precondition arguments on `put` (needed for the lease).

`S3Storage(bucket, prefix, endpoint_url, region, profile, addressing_style,
tls)` uses boto3. `addressing_style = "path"` is often required by
S3-compatible stores.

Conditional-write probe: some S3-compatible stores ignore `If-None-Match`
without returning an error. On the first write of each process, the store
writes a probe object twice with `If-None-Match: *` and checks that the second
write is rejected. The result is cached in local state for 24 hours. If the
probe fails, the lease uses read-back verification (below).

### Lease

The lease is a small JSON object in the store, `_system/lease.json`, that
records which machine is currently allowed to write and until when. S3 has no
locks, so without it two machines pointed at the same bucket would overwrite
each other's decisions files and generation counter without any error. With one
machine at a time this is a guard against mistakes, such as an old laptop or a
container still configured against the bucket. Removing it changes nothing in
normal use; it only matters when a second writer appears.

```python
class Lease:
    def acquire(self, *, force: bool = False) -> None   # raises LeaseHeld(holder, age)
    def ensure(self) -> None                            # called before each write batch
    def renew(self) -> None
    def release(self) -> None
```

Contents: `{holder_host, machine_id, pid, command, acquired_at, renewed_at,
ttl_seconds, token}`.

- The lease belongs to a machine (`machine_id` stored in local state), not a
  process. `brain start` and CLI commands on the same machine share it and take
  turns writing using a local `flock` on a file in the cache directory.
- Acquire: `put(if_absent=True)`. If it exists and is expired, replace it with
  `put(if_match=etag)`. If it is live and held by another machine, fail with the
  holder's hostname, command, and age, and mention `--force`.
- `brain start` renews every TTL/3. Short commands take the lease on first
  write and release it on exit if they were the ones that acquired it.
- Without conditional-write support: write, wait 2 seconds, read back, compare
  `token`. This is best effort and `doctor` says so.

### Vault

Typed access to files. Every write method calls `lease.ensure()`, writes the
object, then bumps `_system/generation.json`.

```python
class Vault:
    def save_entry(self, text: str, *, title: str | None = None) -> Entry
    def update_entry(self, entry: Entry) -> Entry
    def iter_entries(self) -> Iterator[Entry]
    def get_entity(self, id: str) -> Entity
    def put_entity(self, entity: Entity) -> None
    def get_decisions(self, entry_id: str) -> Decisions
    def put_decisions(self, d: Decisions) -> None
    def get_sidecar(self, kind: SidecarKind, entry_id: str) -> Sidecar | None
    def put_sidecar(self, s: Sidecar) -> None
```

### Index sync

On startup, read `_system/generation.json`. Each write through brain replaces
it with a new random token. If the token equals `meta.generation`, skip
listing. (A random token rather than a counter, so two processes that bump it
at the same moment cannot produce the same value.) Writes made by this process
update the index directly and advance `meta.generation` only if the index was
already in sync before the write. Otherwise list the store, diff against the
`objects` manifest by ETag, and rebuild rows for changed keys. A full listing
also runs if the last one is older than 24 hours, and on `brain sync --full`.
`brain reindex` deletes the database and rebuilds from a full listing.

Embedding endpoint usage: needed when new or changed text is embedded (in the
pipeline) and when a query is embedded (`ask`, `search`). Not needed by
`reindex`, which reads vectors from embedding sidecars.

### LLM

```python
class LLM:
    def structured(self, prompt: str, output: type[T], *, role: Role,
                   deps: dict | None = None) -> Stamped[T]
        # role in {"chat", "extract"}; Stamped carries model name + prompt stamp
    def embed(self, texts: list[str]) -> np.ndarray      # (n, dim) float32
```

- Chat and extraction use `pydantic-ai-slim[openai]` with an OpenAI-compatible
  model pointed at `base_url`. The slim package avoids pulling every provider SDK.
- Embeddings use the `openai` SDK's embeddings endpoint (already a dependency of
  the PydanticAI OpenAI model), sharing the same `httpx2.Client`. (openai 3.x and
  pydantic-ai 2.x use `httpx2`, a separate package from `httpx`; brain uses
  `httpx2` throughout so the TLS client it builds is the type the SDKs expect.) If the
  installed PydanticAI version has an embeddings API that accepts a custom
  client, use that instead; check at implementation time.
- `tls.py` builds one `ssl.SSLContext` from `[tls]`: OS trust store via
  `truststore` by default, or `ca_bundle` if set; then
  `load_cert_chain(client_cert, client_key)` if set. httpx2 gets the context;
  boto3 gets `verify=ca_bundle` and `Config(client_cert=...)`. Proxy env vars
  are honoured by both.
- `structured_output = "auto" | "tool" | "native" | "prompted"` maps to
  PydanticAI's output modes. OpenAI-compatible gateways differ in whether they
  support tool calling or `response_format: json_schema`; `doctor` tests each
  mode and reports which work.
- The fake provider uses PydanticAI's `FunctionModel` to return canned outputs
  from fixtures, and an embedder that seeds a RNG from a hash of the text.

Prompts are markdown files with a frontmatter `version`. The stamp recorded on
outputs is `{name, version, sha256}`, so an edit made without bumping the
version is still visible.

### Pipeline

Per entry, each step reads files and writes one sidecar that records the exact
version of its input. A step runs when its sidecar is missing or that input has
changed, which makes the pipeline idempotent and resumable without a job table.
The index records each entry's `next_step` (`extract`, `embed`, `resolve`, or
`done`), so `brain process` knows what is pending without reading the store.
A prompt change does not re-run anything by itself; `brain reextract
--stale-prompts` does that on request.

1. `extract` → extraction sidecar. The prompt includes the 40 most recently
   active threads (id, name, description, start of the summary) and the names
   of known people, orgs, and themes, so the model can reuse them. A validator
   makes the model retry if it names a thread id that was not offered. Spans
   are found by locating the model's `quote` in the entry (exactly, then
   case-insensitively, then by fuzzy match), since models are unreliable with
   character offsets. Fragment ids are carried over from the previous
   extraction by matching kind and text (`pipeline/match.py`); ids that do not
   match are never reused.
2. `embed` → embedding sidecar for the entry and each fragment. Texts whose
   hash did not change keep their vectors, so editing one sentence re-embeds
   only what changed. This runs before `resolve` because answer proposals use
   the vectors.
3. `resolve` → resolution sidecar.
   - People and orgs: an exact, unique alias links directly with no model
     call. Otherwise candidates come from close spellings, first-name or
     last-name matches, and acronyms; the model chooses among them. A link is
     made only when the model picks the top candidate with confidence ≥ 0.8.
     Anything else (a different candidate, "unsure", or "new") goes to review.
   - Themes: the same, plus embedding similarity of theme names. With no
     match, a theme file is created with `state: proposed` and used right away.
   - Threads proposed by the extraction: matched to existing threads by name;
     otherwise a thread file is created with `state: proposed`.
   - Answer proposals: open questions and action items from other entries
     that are close to this entry's fragments by vector similarity are shown
     to the model, which proposes `answers` links. They appear in review and
     close nothing on their own.
4. `threads` → after the entry's steps, summaries of the threads it touches
   are updated. A summary records `through_entry`; an update sends the
   previous summary plus only the later fragments.

Pending review items never block processing. An unresolved mention is stored
without a link until you settle it. A failed step leaves the entry saved with
its remaining steps pending; the error is kept in
`$XDG_STATE_HOME/brain/pipeline-failures.json` and shown by `brain doctor` and
`brain show`. Only one process on a machine runs the pipeline at a time
(a non-blocking lock in the state directory).

Prompts live in `src/brain/prompts/` (`extract`, `resolve`, `answers`,
`thread_summary`, `brief`, `ask`, and `probe` for `doctor`).

### Review queue

`brain review` walks through items one at a time. Each decision is written to
files: a decisions-file mention mapping (`person: {"Mike": mike-chen}`) for
each affected entry, a new or updated entity file, or a thread/theme `state`.
Linking or creating a person can apply to every entry with the same unsettled
name, and adds that name as an alias so future entries link without asking.
Accepting a proposed answer records it on the answering fragment and closes
the earlier question or action item.

Dreaming adds two kinds of item. A proposed answer it found is a
`proposed_answer` like any other, marked `source: dream`. A `dream_obe` item
says an earlier fragment may have been replaced by a later one. Accepting it
marks the earlier fragment overcome by events, with the later fragment's id
and date in the reason; rejecting it records the fragment's current status as
yours (`status_origin: user`), which keeps it and stops dreaming from asking
again. Both are written to the entry's decisions file.

### Testing without a model

`FakeLLM` (`core/llm/fake.py`) runs the real PydanticAI agents against a
scripted model: by default simple rules over each prompt's input (sentences
become fragments, capitalized names become people, `#tags` become themes,
`[[Name]]` assigns a thread), or canned outputs supplied by a test. Its
embeddings are bag-of-words vectors, so texts that share words are close.
`BRAIN_FAKE_LLM=1` makes the CLI use it, for trying brain without an endpoint.
`FakeOpenAI` is a fake OpenAI-compatible HTTP endpoint used to test the real
client path, including tool, native, and prompted structured output.

### Where processing runs

- `brain start` runs a worker thread that processes pending work on startup,
  after each entry saved through the web UI or MCP, and on a timer
  (`[worker] poll_seconds`, default 60). The timer checks the generation
  counter, and a full listing runs every 10 minutes to catch files edited
  outside the tool.
- `brain jot` saves the entry. If `brain start` is running on this machine
  (found through `server.json` in the state directory), `jot` notifies it and
  returns immediately. Otherwise `jot` processes the entry in the foreground;
  Ctrl-C or a failure leaves the entry saved and pending.
- `brain process` runs all pending work in the foreground, for use when
  `start` is not running.
- A processing lock in the state directory stops the worker and a CLI command
  from processing at the same time.
- Dreaming: `brain dream` runs a pass in the foreground. Inside `brain start`,
  a worker round that found no entries to process dreams if a pass is due:
  `[dream] after_entries` new entries since the last pass, or the nightly time
  `[dream] nightly_at` has passed and something is new. "Dream now" in the web
  UI (`POST /api/dream`) asks the worker for a pass on its next idle round.

### Thread operations

`core/threads.py`. Rename (the old name becomes an alias, so mentions of it
still resolve), pin, archive, reactivate, overcome by events, merge, split, and
move. Thread-level changes are written to the thread file. Merge sets
`state: merged` and `merged_into`, follows an existing merge chain to its
root, refuses cycles, and copies names and aliases to the target; everything
that reads a thread (briefs, timelines, activity, summaries) includes the
threads merged into it. Split and move write `threads: {add, remove}` to each
affected entry's decisions file, so the change survives re-extraction.
Marking a thread overcome by events can also mark its open questions and
action items overcome by events (`cascade`). Briefs on an archived or OBE
thread tell the model so, and it describes the thread as history.

### The overcome-by-events sweep

`core/sweep.py` lists items that may no longer hold: open questions and action
items older than 30 days, speculative fragments older than 30 days, and
unpinned threads with no activity for 60 days. Items in archived or OBE threads
are left out, and so are items whose status or open/closed state you set
yourself (the index records `status_origin` and `state_origin`), so "keep it"
is remembered. `brain sweep` and the "Tidy up" page offer: overcome by events
(with a reason), close or confirm, keep, and for threads archive, OBE, or pin.
When dreaming has checked an item, its verdict (still open, probably
resolved, or unclear, with a reason and the later fragment it rests on) is
shown next to it. You still decide.

### Temporal context

`core/temporal.py` renders every date a model sees. Rules:

- Every model input begins with `<today date="2026-11-08" weekday="Sunday"/>`,
  from the pipeline clock.
- Every dated element has `date` and `age`, computed in code:
  `date="2026-10-30" age="9d"`. Ages are days up to 13, weeks up to 8, then
  months (`9d`, `3w`, `5mo`), so the model never subtracts dates.
- Summaries and Now lines carry `as_of`: the date of the newest entry they
  cover (`<thread_summary as_of="2026-11-04" age="4d">`). Entities carry
  `last_mentioned`; their "recently" mentions are dated. Extraction's thread
  list gives each thread's last activity and its summary's as-of date.
- Within a group, items are ordered oldest to newest, so the latest is nearest
  the end. `ask` re-sorts its excerpts by date after retrieval chooses them.
- Dates are local calendar dates in the machine's time zone (`TZ`). Stored
  timestamps stay UTC; an entry written at 9 pm is dated that day.
- Each prompt that receives notes has the same paragraph: what today, date,
  age, and as_of mean, and that later information supersedes earlier
  information on the same point unless it says otherwise.

`tests/test_temporal.py` runs every model-calling operation, dreaming included,
against the fake LLM and fails if any input does not start with `<today>` or
any note element lacks its date and age, so a new prompt cannot leave dates
out. MCP results carry `today`, `age_days` next to each date, and
`summary_as_of`; their lists run oldest to newest.

### Injected context

`core/injected.py` builds a short summary of current state that every agent
session receives without asking: active and pinned threads (pinned first, then
by last activity) with last activity, open count, and a Now line with its
as-of date; the newest open items; decisions from the last
`[context] recent_days` days and fragments marked overcome by events in that
time; and what waits on the user (review items by kind, unprocessed entries).
Every line carries a date and age and the ids that lead to `brief`,
`get_entity`, and `get_entry`. Someday fragments and archived or OBE threads
are left out; proposed threads are included and marked as such.

It is rendered from the index on every read, so it is never stale. A thread's
Now line comes from dreaming; until a thread has been dreamed about, the first
sentence of its summary stands in, with the summary's as-of date. Sections are
filled in order and cut at `[context] max_lines` and `max_bytes`, with a line
saying what was left out; the closing lines always appear.

It reaches agents four ways: `brain context` (markdown, or `--format json`),
for example from a Claude Code `SessionStart` hook; the MCP resource
`brain://context` and tool `get_context`; the instructions of the stdio MCP
server, which starts fresh for each session (the HTTP server in `brain start`
runs for days, so it leaves this out); and a panel on the web UI's home page
showing exactly what an agent receives.

### Dreaming

`core/dream.py`. An offline pass over entries, fragments, and derived files. It
never edits entries. Threads are taken in this order: pinned, then briefed in
the last 14 days, then by last activity; archived and OBE threads are skipped.
For each thread, a step runs only if entries arrived since that step's
watermark in `state.json`:

1. Summary rebuild: rewrites the thread summary from all of its fragments
   (`summarize(full=True)`, the `thread_summary` prompt with no previous
   summary). It runs after `[dream] rebuild_after_updates` new entries since
   the last rebuild, or when the last rebuild is older than
   `rebuild_after_days` and there is anything new. Incremental updates carry
   errors and omissions forward; a rebuild does not.
2. Supersession (`dream_supersede`): finds earlier fragments that a later one
   replaced or contradicted. Pairs with confidence at least 0.7 become
   `dream_obe` review items; fragments whose status you set are never proposed.
3. Answers (`dream_answers`): compares the thread's open questions and action
   items with everything written after them in the thread. Write-time answer
   proposals only see the new entry; this sees the whole later history.
   Links you already accepted or rejected are not proposed again.
4. Now line (`dream_now`): one sentence on where the thread stands, written
   directly, for the injected context.

Then the sweep step (`dream_sweep`) checks old open and speculative items, in
batches of 8, against the fragments written after them in their threads and
fragments that share their words. Items with nothing written since are
skipped, and so are items whose verdict already covers the newest of those
fragments.

Derived output (summaries, Now lines, run log, watermarks) is written
directly; everything that changes what is recorded as true is a proposal, and
nothing applies until you accept it. Each prompt is told what the memory is
for rather than given narrow rules, and every output is validated: ids must
come from the input, a later fragment must be newer than the one it replaces
or answers, and citations must be entry ids from the input.

A pass stops after `[dream] max_calls` model calls (default 50) and records
`budget_exhausted`; work it did not reach comes first next time because its
watermarks did not move. `brain dream --dry-run` lists the steps and the number
of calls without calling a model. A pass takes the processing lock, so it
never overlaps the pipeline, and its writes go through the lease like any
other.

### Brain façade

```python
class Brain:
    def jot(self, text: str, *, title=None, process=True) -> Entry
    def process_pending(self) -> ProcessReport
    def review_items(self) -> list[ReviewItem]
    def resolve_review(self, item_id: str, decision: ReviewDecision) -> None
    def active_threads(self) -> list[ThreadActivity]
    def brief(self, target: str, *, since: date | None = None) -> Brief
    def ask(self, question: str) -> Answer
    def search(self, query: str, *, kinds=None, limit=20) -> list[Hit]
    def set_fragment(self, fragment_id: str, change: FragmentChange) -> None
        # status, OBE date/reason, someday flag, open/closed, thread links
    def thread_op(self, op: ThreadOp) -> None      # rename/merge/split/pin/archive/obe
    def get_entity(self, id: str) -> EntityView
    def reindex(self) -> None
    def reextract(self, selector: Selector) -> ProcessReport
    def doctor(self) -> list[CheckResult]
    def context(self) -> ContextDoc              # the injected context; context_text() renders it
    def dream(self, *, thread=None) -> DreamRun  # dream_plan() for a dry run
```

The core API is synchronous. FastAPI runs it in its threadpool (`def`
endpoints) and the MCP tools call it via `anyio.to_thread`. PydanticAI is
called with its sync entry point inside the core.

`Brief` and `Answer` are typed outputs where each claim carries
`citations: list[entry_id]`. An output validator rejects citations that are not
in the retrieved context and asks the model to retry (PydanticAI `ModelRetry`).

Brief `since` default: the last time a brief was generated for that node
(`state/briefs.json`); if none, everything. The "what changed" section also
covers activity in adjacent nodes (people, linked threads, themes) since the
last entry that touched the node directly. Closed questions and action items
appear under "recently closed", not in the open list.

`ask` retrieval: FTS5 BM25 hits and sqlite-vec hits, merged by reciprocal rank
fusion, expanded with one hop of graph neighbours, then passed to the chat model.

### Front-ends

- `brain start`: one uvicorn process on `127.0.0.1:<port>` serving the web UI,
  the HTTP API, MCP over streamable HTTP at `/mcp`, and the worker. Claude Code
  connects with `claude mcp add --transport http brain http://127.0.0.1:<port>/mcp`.
  No authentication in v1; it binds to localhost only.
- `brain mcp`: the same MCP tools over stdio, for when `start` is not running.
  The MCP tool definitions live in one module used by both transports.

### Web server (`brain start`)

One process runs uvicorn on 127.0.0.1 with a FastAPI app (`src/brain/web/app.py`):
the JSON API under `/api`, MCP over streamable HTTP at `/mcp`, and the built UI
(any other path returns `index.html`, so client-side routes work on reload).

- Requests share one Brain through `core/shared.py` (one lock, opened lazily).
- The background worker (`core/worker.py`) has its own Brain and SQLite
  connection, so model calls during processing do not block requests. It runs
  on startup, when woken (an entry was saved through the UI, MCP, or
  `brain jot`), every `worker.poll_seconds`, and lists the whole store every
  10 minutes to catch files edited outside brain. MCP `add_entry` hands the
  entry to the worker and waits for it.
- `$XDG_STATE_HOME/brain/server.json` records the pid, port, and store. `brain
  jot` reads it and wakes the server instead of processing in the foreground;
  a stale file (dead pid) is ignored.
- Shutdown work (stop the worker, release the lease, remove `server.json`)
  runs in the app's lifespan, because uvicorn re-raises SIGTERM/SIGINT after
  its graceful shutdown, which skips code after `uvicorn.run`.
- No login. The app rejects a Host header other than localhost (DNS
  rebinding) and any write that is not `application/json` (cross-site forms
  and simple requests cannot send it without a CORS preflight, which the app
  never approves). The MCP endpoint uses the SDK's own DNS-rebinding check.

The UI is React + TypeScript + Vite in `frontend/`, with `react-router-dom`
and Cytoscape.js for the graph (loaded only on the graph page). It is built
into `src/brain/web/static/`; a hatch build hook (`hatch_build.py`) runs the
build when a wheel is made from a checkout without it, and `ci/release.sh`
builds it for release wheels. The graph joins two entities when fragments link
to both, so a technique theme connected to two threads shows the same approach
used in both.

### MCP tools

Built on the official `mcp` SDK (2.x, `MCPServer`), in `src/brain/mcp/`. Each
tool returns a typed result (structured content plus the same data as JSON
text) and calls one Brain method.

| Tool | Read-only | What it returns |
|---|---|---|
| `search(query, limit, types)` | yes | entries, fragments, and entities by keyword and vector similarity |
| `ask(question)` | yes | cited claims and their sources |
| `brief(target, since, mark_seen)` | yes | the brief sections, open fragments, and sources |
| `get_entity(name_or_id)` | yes | details, notes, related nodes, open items, recent fragments |
| `get_entry(entry_id)` | yes | the entry text and its fragments, for following a citation |
| `list_active()` | yes | active and pinned threads with counts, people, and summaries |
| `add_entry(text, title)` | no | the saved entry and, after processing, its fragments |
| `get_context()` | yes | the injected context (also the resource `brain://context`) |

`get_entry` is an addition to the original list, so an agent can read a cited
entry in full. Every result has `today`, and every date has `age_days`
beside it. `brain mcp` also appends the injected context to its instructions. `brief` does not move the user's "last looked" date unless the
agent passes `mark_seen` (an agent often reads a brief for its own context
without showing it).

Expected failures (model errors, lease held elsewhere, ambiguous names,
unknown ids) are raised as `ToolError`, so the agent sees the message; other
exceptions reach it only as "Error executing tool". The Brain is opened on the
first call, so a storage problem is reported by the tool instead of stopping
the server. Sync tools run on worker threads, so all calls share one lock.

Several processes on one machine can use the same index file at once (for
example `brain mcp` and a CLI command). Index write transactions use
`BEGIN IMMEDIATE`, and entry and entity ids are chosen and written under the
machine-wide write lock, so concurrent writers cannot pick the same id.

## 7. Configuration

Load order (later wins): defaults → `$XDG_CONFIG_HOME/brain/config.toml`
(default `~/.config`, also on macOS) → file from `BRAIN_CONFIG` or `--config`
→ env vars `BRAIN_<SECTION>__<KEY>` (e.g. `BRAIN_STORAGE__BUCKET`). Implemented
with `pydantic-settings`.

```toml
[storage]
backend = "s3"                 # s3 | local
bucket = "team-x-brain"
prefix = "brain/"
endpoint_url = ""              # S3-compatible stores
region = ""
profile = ""                   # AWS profile; otherwise the standard AWS env chain
addressing_style = "auto"      # auto | path | virtual
path = ""                      # local backend root

[tls]                          # used for both the model endpoint and S3
ca_bundle = ""                 # empty: OS trust store
client_cert = ""
client_key = ""

[llm]
base_url = "https://gateway.internal/v1"
api_key_env = "BRAIN_API_KEY"
chat_model = "..."
extract_model = "..."
embed_model = "..."
doc_model = "..."              # unused until document attachments
embed_dim = 0                  # 0: detect and record in the index
structured_output = "auto"     # auto | tool | native | prompted
timeout_seconds = 120

[index]
path = ""                      # default under XDG cache

[threads]
inactivity_days = 21

[lease]
ttl_seconds = 900

[server]
port = 8765

[worker]
poll_seconds = 60

[dream]
enabled = true                 # whether `brain start` dreams on its own
nightly_at = "03:00"           # local time; "" turns the nightly pass off
after_entries = 10             # also dream after this many new entries; 0 turns it off
max_calls = 50                 # model calls per pass
rebuild_after_updates = 10     # rebuild a summary after this many new entries
rebuild_after_days = 30

[context]
max_lines = 60
max_bytes = 6000
recent_days = 7
open_items = 5
```

## 8. Distribution and CI

- Not published to PyPI. The repository is public on GitHub; you clone it and
  run `uv tool install --managed-python --python 3.12 .`. On a network with
  internal mirrors, Python comes from the uv mirror (`UV_PYTHON_INSTALL_MIRROR`)
  and dependencies from the PyPI mirror. `requires-python = ">=3.11"`;
  development uses 3.12.
- The dist name stays `brain-journal` so it can never be confused with the
  unrelated `brain` package on PyPI. The command is `brain`.
- The built frontend has to reach the installing machine. Options, to decide at
  milestone 4: build it at install time from the clone (needs Node and an npm
  registry), or attach a prebuilt wheel to GitHub releases (needs release
  downloads to be reachable).
- CI (GitHub Actions; each job is a plain shell script so GitLab CI can call the
  same scripts):
  - lint + type check (ruff, pyright)
  - tests on macOS and Ubuntu with uv-managed Python 3.11 and 3.13
  - tests inside a UBI9 container with uv-managed Python, to catch RHEL-specific
    differences (OpenSSL policy, FIPS mode, filesystem). The code uses SHA-256
    only, never MD5, so it works with FIPS enabled.
  - S3 contract tests against MinIO, started by the test fixture
  - frontend: `tsc --noEmit`, `vite build`, vitest
- S3 tests read `BRAIN_TEST_S3_ENDPOINT`; if unset, the fixture tries to start
  MinIO with `podman` or `docker`; if neither works, the tests are skipped with
  a reason. MinIO stopped publishing public images and binaries in late 2025,
  and its GitHub repository is archived, so the fixture uses Chainguard's free
  MinIO image pinned by digest (`BRAIN_TEST_MINIO_IMAGE` overrides it). `moto`
  is a pure-Python fallback that runs without containers, if its
  conditional-write behaviour is adequate (check when writing the tests).

## 9. Doctor

`brain doctor` runs these checks and prints a fix for each failure:

| Check | What it does |
|-------|--------------|
| config | parses, validates, shows which file and env vars were applied |
| python/sqlite | Python version, SQLite version, FTS5, extension loading, sqlite-vec loads |
| index path | writable; warns if on NFS (SQLite locking is unreliable there) |
| storage | put / get / list / delete a probe object under `_system/doctor/` |
| conditional writes | runs the probe; reports enforced or fallback mode |
| lease | current holder, age, whether it is expired |
| TLS | which trust source is used, CA file readable, cert/key pair loads |
| proxy | effective proxy for each endpoint; warns if a localhost URL is not in `NO_PROXY` |
| chat / extract | one tiny call per model; each structured-output mode tried |
| embedding | one call; dimension compared to config and to the index |
| index health | schema version, generation vs. store, pending extractions, row counts |
| server | whether `brain start` is running, and on which port |
| dream | when the last pass ran, its model calls, what it applied and proposed, and its last error |

## 10. Decision log

| # | Decision | Status |
|---|----------|--------|
| 1 | Lease per machine; `brain start` combines web, MCP (HTTP), and worker; `brain mcp` stdio kept | agreed |
| 2 | Conditional-write support probed at runtime | agreed |
| 3 | Your changes to extracted data stored in `decisions/<entry-id>.yaml`, applied on top of the extraction | agreed |
| 4 | Embeddings stored as generated files so `reindex` needs no endpoint | agreed |
| 5 | sqlite-vec as the vector index; uv-managed Python everywhere | agreed |
| 6 | `someday` is a boolean flag (called `dream` before 0.1.0) | agreed; renamed after milestone 5 |
| 7 | Questions and action items have `state: open | closed`, reversible; the pipeline only proposes closing | agreed |
| 8 | Brief `since` defaults to the last brief for that node | agreed, expected to change with use |
| 9 | People and orgs wait for review; themes and threads are created as `proposed` and used immediately | agreed |
| 10 | One `[tls]` section for both endpoints | agreed |
| 11 | `brain start` processes pending work in the background; `brain process` for manual runs | agreed |
| 12 | GitHub only, no PyPI. Both ways of getting the UI to work are supported: release wheel with UI included, or build from a clone with Node | agreed; choose per machine |
| 13 | FIPS mode must work: SHA-256 only, enforced by a test that rejects MD5/SHA-1 in the source | agreed |
| 14 | Mentions in the decisions file are grouped by type (`person`, `org`, `theme`) | made during milestone 1 |
| 15 | `get_entry` MCP tool added; MCP `brief` leaves "last looked" unchanged unless `mark_seen` | made during milestone 3 |
| 16 | One process for web, MCP over HTTP, and the worker (`brain start`); the worker has its own index connection | made during milestone 4 |
| 17 | Node 24 for building the UI; the wheel carries the built UI so installs need no Node | made during milestone 4 |
| 18 | Thread OBE can cascade to open items; the sweep skips items you already decided on | made during milestone 5 |
| 19 | "Dream" means offline consolidation, as in Anthropic's agent-memory post | agreed after milestone 5 |
| 20 | Every note given to a model carries its date and age; derived text carries an as-of date; dates are local | agreed after milestone 5 |
| 21 | Dreaming applies derived output (summaries, Now lines) directly and proposes anything that changes what is recorded as true | agreed after milestone 5 |
| 22 | Dream proposals are stored per entry, so the entry's index rebuild turns them into review items and drops decided ones | made while building dreaming |
| 23 | Rejecting a supersession records the current status as yours; no new decisions-file field | made while building dreaming |
| 24 | The injected context includes proposed threads, marked, because new threads stay proposed until reviewed | made while building the injected context |

## 11. Not in v1

- Attaching documents (PDFs and similar) through `llm.doc_model`.
- Scheduled digests.
- Merging duplicate people and organizations after they have been created.
  Review is the guard against duplicates; a merge would rewrite the mention
  mappings of every affected entry and remove the duplicate file, and needs no
  file-format change.
- Authentication for `brain start`, which listens on localhost only.
- Dreaming that suggests updates to person and org profiles (role, current
  work, memberships) or flags likely duplicate people; acting on duplicates
  needs the entity merge above.
