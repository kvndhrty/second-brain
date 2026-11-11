# brain — architecture

This describes the design as built in version 0.2: notes in, wiki out. It
replaces the review-based design of 0.1. Section 14 records the decisions
made along the way and why.

## 1. Summary

- The person only writes notes and asks questions. Notes are the only input.
- Models build and maintain everything else: people, orgs, topics, threads,
  and a wiki page for each.
- The work is split into layers of small calls, so a small model does it
  reliably. A strong model is optional and is used only where configured.
- Files in a store (local directory or S3 prefix) are the source of truth. A
  SQLite database on local disk is a derived index (tables, FTS5, sqlite-vec
  vectors); `brain reindex` rebuilds it from the files without calling a model.
- Everything except the notes is generated, and `brain rebuild` regenerates
  it from the notes by running the layers again in date order.
- One Python package. `brain.core` holds all behaviour and has no imports of
  Typer, FastAPI, or MCP. `brain.cli`, `brain.web`, and `brain.mcp` call a
  single façade class (`Brain`) and only format input and output.
- `brain start` is the one long-running process: web UI, HTTP API, MCP over
  HTTP, and a background worker that processes new notes and dreams.

The memory layers follow Anthropic's post [Components of agent
memory](https://www.anthropic.com/research/components-of-agent-memory):

| Post | brain |
|---|---|
| Transcripts (ground truth) | Notes, never changed by the tool |
| Memory notes | Statements, written by layers 1 and 2 |
| Representations | Entity records and wiki pages |
| Injected context | `brain context`: a short, dated summary loaded into agent sessions |
| Dreaming | Layer 9: an offline pass across all notes |

## 2. Rules

1. **Notes are never changed by the tool and never edited to correct them.**
   A correction is a new note. Later notes take precedence over earlier ones.
2. **Everything except notes is generated** and can be rebuilt from the notes.
3. **The tool never asks the person to decide anything.** When a layer is
   unsure, it records the result as provisional, and a later layer or a later
   note settles it.
4. **Each call does one thing.** Its input is small and bounded, its output
   is a choice from a list or a short structure, and code validates the
   output before using it.
5. **Code does everything that does not need a model:** finding candidates,
   exact name matches, date arithmetic, citation checks, and every list on a
   wiki page.
6. **A failed call never blocks a note.** See section 8.

## 3. Package layout

```
src/brain/
├─ core/
│  ├─ config.py            settings model, load order, XDG paths
│  ├─ keys.py              every storage key pattern, in one place
│  ├─ formats/             note.py, records.py (generated files), yamlio.py
│  ├─ storage/             Storage protocol, local, s3, lease, conditional-write probe
│  ├─ tls.py               one SSLContext for httpx and boto3
│  ├─ vault.py             typed file access over Storage; takes the lease on write
│  ├─ index/               schema.sql, db.py, build.py, sync.py, queries.py (World), search.py
│  ├─ llm/                 client, models (structured calls), prompts, schemas, fake
│  ├─ pipeline/
│  │  ├─ layers.py         1 split, 2 classify, 3 names
│  │  ├─ link.py           4 link names to entities
│  │  ├─ filing.py         5 file statements under threads
│  │  ├─ state.py          6 relate statements; 6b correction notes
│  │  ├─ pages.py          7 write Now and Summary; 8 check each sentence
│  │  ├─ wiki.py           render wiki/ from the index
│  │  ├─ embed.py, text.py, context.py (Calls: fallback and escalation)
│  │  └─ runner.py         runs the layers for a note; idempotent
│  ├─ dream.py             9 dreaming
│  ├─ answers.py           ask, brief (with routing)
│  ├─ injected.py          `brain context`
│  ├─ views.py             read models for pages, notes, search, the change log
│  ├─ services.py          the Brain façade every front-end calls
│  ├─ worker.py, shared.py, runtime.py, localstate.py, doctor.py, temporal.py
├─ prompts/                one versioned .md file per prompt
├─ cli/  web/  mcp/        thin front-ends
frontend/                  React/Vite web UI, built into src/brain/web/static
```

## 4. Store layout (format 2)

All keys are relative to the configured prefix (S3) or root directory (local).

```
notes/2026/11/09/20261109T143205Z-vendor-call.md      authored; the only input
wiki/index.md                                         generated; readable
wiki/log.md                                           what changed, newest first
wiki/threads/gpu-quota-migration.md
wiki/people/jane-doe.md
wiki/orgs/platform-team.md
wiki/topics/retrieval-eval.md
generated/notes/<note-id>/split.json                  one file per layer per note
generated/notes/<note-id>/{classify,names,links,filing,state,pages}.json
generated/entities/<entity-id>.json                   identity: type, names, status
generated/pages/<entity-id>.json                      the model-written page sections
generated/embeddings/<model-slug>/<note-id>.json
generated/dreams/runs/<timestamp>.json                one dream pass: what it changed
generated/dreams/state.json                           what dreaming has already checked
state/briefs.json                                     when each page was last briefed
_system/format.json, lease.json, generation.json
```

- `notes/` is the only directory a person writes to. A markdown file saved
  anywhere under it by any editor becomes a note: it is given an id and its
  modification time, and the bare file is replaced by the note. The text is
  kept as written, apart from line endings and blank lines at its start and
  end. A file is taken once it has not changed for 30 seconds, so a file an
  editor is still saving is not taken half-written; `brain start` looks
  every minute and `brain process` when it is run. Files whose names brain
  does not use (anything under a dot directory, such as `.obsidian/`) and
  files that are not UTF-8 text are left alone.
- `wiki/` holds plain markdown with `[[file-name|label]]` links, which
  Obsidian and similar editors follow. The tool overwrites a page when it
  changes, so a hand edit does not last. To change a page, write a note.
- An entity id is a readable slug assigned once (`jane-doe`, `jane-doe-2` on
  collision) and kept across renames, merges, and rebuilds, so links and page
  paths stay stable.
- Paths are never identity: every file carries its id. Timestamps in keys use
  ISO 8601 basic format in UTC. Writes are whole-object; local writes go to a
  temp file and `os.replace`.
- There is no migration from format 1. A format 2 store starts empty.

### Note

```markdown
---
format: 2
id: n-20261109T143205Z
created: '2026-11-09T14:32:05Z'
title: Vendor call            # optional
about: gpu-quota-migration    # optional: the page a "This is wrong" note was written from
---
The text, exactly as written.
```

### Layer files

Each has `note_id`, `input_hash`, `model`, `prompts` (name, version, and
SHA-256 of each prompt the layer uses), `created_at`, and `events` (lines for
the change log), plus its own data:

| File | Data |
|---|---|
| split | `statements`: id (`s-<note timestamp>-<hash>`), text, and the quote it came from |
| classify | `kinds`: statement id → fact, decision, question, action, idea, or correction |
| names | `names`: the name as written, and person, org, or topic |
| links | `links`: name → entity id, and how: `exact` (code), `model`, or `new` |
| filing | `threads`: statement id → thread entity id, or none |
| state | `relations` (later statement answers, supersedes, or repeats an earlier one) and `operations` (from correction notes) |
| pages | `pages`: the entities whose page changed. Written last; a note is pending until it has this file |

A statement id ends in a hash of the words the statement was taken from, not
its position in the note. When a note file is edited, statements whose words
did not change keep their ids, so a later note's relation or retraction
still points at the same words. One that pointed at words that were edited
away points at nothing and is ignored.

A layer is skipped when its file has the same `input_hash` and the same
prompt stamps. Changing a prompt reruns that layer, and the layers after it,
the next time the note is processed; `brain rebuild` does it for every note.

### Entity record

```json
{"id": "jane-doe", "type": "person", "name": "Jane Doe", "names": ["Jane", "JD"],
 "status": "active", "merged_into": null, "not_same_as": [],
 "created_at": "...", "created_from": "n-...", "closed_at": null,
 "set_by": {"status": "note:n-..."}, "profile": {"role": "program manager", "org": "Platform Team"}}
```

`status` is `provisional` (no page yet), `active`, or `closed`. `set_by`
records which fields a note set, as opposed to a model; dreaming does not
change those.

### Wiki page

```markdown
---
id: gpu-quota-migration
type: thread
names: [GPU quota migration, quota work]
status: active
as_of: '2026-11-09'           # date of the newest note it covers
---
# GPU quota migration

## Now          one or two sentences, written by a model, with citations
## Summary      three to six sentences, written by a model, with citations
## Open         open questions and things to do          (built by code)
## Decided      decisions that still stand, newest first (built by code)
## History      every statement, newest first; superseded ones struck through
## Related      links to the pages that share statements with this one
```

Only Now (threads) and Summary are written by a model. The rest is built from
the statements, so it cannot drift from them.

## 5. Layers

Layers 1 to 6 run per note, in order, as soon as a note exists. Layers 7 and
8 run for each page the note touched. Layer 9 runs offline.

| # | Layer | One call per | Output | Code checks |
|---|---|---|---|---|
| 0 | Capture | — | the note file | — |
| 1 | Split | note | statements, each with the words it came from | every quote is in the note |
| 2 | Classify | statement | one kind from a fixed list | value is in the list |
| 3 | Spot names | note | names as written, each person, org, or topic | every name occurs in the note |
| 4 | Link | unresolved name | a candidate id, or `new` | id is one of the candidates |
| 5 | File | statement | a candidate thread id, `new` with a name, or `none` | id is one of the candidates |
| 6 | Relate | (new, earlier) statement pair | answers, supersedes, repeats, or unrelated | "answers" only for a question or action |
| 6b | Correct | correction statement | one operation, then its targets | targets are candidates; the right number of them |
| 7 | Write | page section | Now (≤ 40 words) or Summary sentences, each with citations | every citation is a note in the input |
| 8 | Check | written sentence | supported: yes or no | — |
| 9 | Dream | see section 7 | | |

- **Layer 4.** Code links a name with no call when it matches exactly one
  known name. A topic name may turn out to be a thread ("the storage
  migration"), so threads are candidates for topics. Candidates for the rest have a similar spelling or share a
  whole word ("Jane" and "Jane Doe"). `new` creates a provisional entity. It
  gets a page when a second note mentions it; a person written with a full
  name gets one at once. Names are processed longest first, so "Jane Doe"
  exists before "Jane" in the same note is resolved.
- **Layer 5.** Candidates are threads named in the note, threads already
  chosen for this note, and the threads of the nearest earlier statements (by
  embedding) and of statements about the same entities. A new thread is
  provisional until a second note files something under it.
- **Layer 6.** A new statement is compared with at most four earlier ones,
  nearest in meaning first: up to two open questions or actions from its
  thread and from the pages it mentions (a question itself answers nothing),
  and, for a decision or fact, one or two earlier decisions or facts in the
  same thread, which it may replace. A statement with no thread and no names
  is compared with its nearest neighbours by embedding. At most 24 pairs per
  note. A statement that repeats an earlier one is hidden; if what it repeats
  was replaced or retracted later, the repeat goes with it.
- **Layer 7.** A thread's summary is updated from the previous summary plus
  the new statements once five have arrived (eight for a person, org, or
  topic). It is rebuilt from all statements when one it covered was
  retracted or a decision it covered was replaced, and by dreaming. A
  sentence carried over unchanged is not checked again.
- **Layer 8.** A sentence that is not supported is dropped. When nothing
  survives, the section stays as it was.

Every dated statement given to a model carries its date and its age relative
to today, and the input starts with `<today>`, so the model can tell newer
information from older without date arithmetic.

## 6. Changing things with later notes

Because notes are the only input, every correction is a note. Two kinds are
recognised.

**A later statement about the same thing.** "The migration moved to January"
after "We decided to migrate in December". Layer 6 records `supersedes`.
Pages show the January statement as current; the December one stays in the
history, struck through, with what replaced it. Nothing is deleted.

**An instruction about the wiki.** Layer 2 classifies it as `correction`.
Layer 6b turns it into one operation from a fixed list, in two calls: which
operation, then which candidates it applies to.

| Operation | Example note | Effect |
|---|---|---|
| `same_as` | "Jane Doe and J. Doe are the same person" | the second is merged into the first |
| `not_same_as` | "Mike Chen in finance is a different Mike from Mike Ross" | undoes a merge; the two are never merged again |
| `rename` | "The quota thing should be called GPU quota migration" | new name; the old one still leads to the page |
| `close` / `reopen` | "The vendor evaluation is finished" | the thread is closed, and its open items with it |
| `retract` | "Ignore what I wrote about the outage cause" | those statements leave pages and answers; the note stays |
| `not_part_of` | "The label work is not part of the quota migration" | its statements move to their own thread |

Every correction in a note is decided before any is applied, so a model call
that fails partway leaves nothing half done. Each operation is then applied
to the entity records by its own function (`pipeline/state.py`) and stored
in the note's state file. Code checks what the model chose: `close` and
`reopen` apply only to threads; two pages can be the same only if they are
the same type (an org and a topic may be merged, since a team is read as
either); `not_same_as` undoes a merge only between the two pages it names. A
merge records which names the surviving page gained, and undoing it removes
only those. The later note wins when two conflict. An operation from a note wins over anything a model
inferred, dreaming included. Retracting the note that retracted something
restores it.

In the web UI every page has a **This is wrong** button. It opens the note
box with the page named; what is written is saved as an ordinary note with
`about` set, so "this is finished" is understood to mean that page.

## 7. Dreaming (layer 9)

Dreaming looks across notes. It runs nightly and after a set number of new
notes while `brain start` is running, or on demand with `brain dream`.

| Step | What it does | Calls |
|---|---|---|
| Duplicates | Compares pairs of entities with similar names; merges those the statements show to be the same | one per pair |
| Stale items | Checks open questions and actions older than `stale_days` against later statements: answered, overtaken, or still open | one per item |
| Quiet threads | Closes a thread with nothing open and no activity for `close_after_days`; new activity reopens it | none |
| Profiles | Fills in a person's role and org from the statements that mention them | one per person |
| Rebuilds | Rebuilds summaries that have been updated many times or not rebuilt for a long while | as layer 7 and 8 |

It uses the strong model when `dream_uses = "strong"` and one is set,
otherwise the small one; its calls have the same closed form as the other
layers, so either can run them. A pass stops after `max_calls`. What it has
checked is remembered in `generated/dreams/state.json`, so the next pass
starts with what this one did not reach and does not repeat settled work.

Dreaming never overrides an operation that came from a note. Everything it
does is in the change log and can be reversed by a note.

## 8. Models

```toml
[llm]
base_url = "..."            # any OpenAI-compatible endpoint
api_key_env = "BRAIN_API_KEY"
small_model = "..."         # layers 1 to 8, and brief routing
strong_model = ""           # optional
embed_model = "..."
dream_uses = "strong"       # strong | small; small when no strong model is set
answer_uses = "small"       # which model writes ask and brief answers
escalate = "off"            # off | strong
```

Each prompt file has a role: `layer` prompts run on the small model, `dream`
and `answer` prompts on whichever model the config assigns.

A call whose output fails validation is retried by the model with the
validation message (PydanticAI's output retries). If it still fails:

- with `escalate = "strong"` and a strong model set, the call is made once on
  the strong model;
- otherwise the layer takes its safe result: one statement per sentence
  (split), `fact` (classify), no names, a provisional `new` entity (link),
  unfiled (file), `unrelated` (relate), no operation (correct), the section
  left as it was (write), not supported (check). The fallback is written to
  the change log.

A failure that is not about the model's answer (the endpoint is down, the key
is wrong, the store cannot be written, a page could not be written) stops
the note at that layer. The note stays saved and pending, the error is shown
by `brain doctor`, and the next attempt continues from the layer that
failed.

Notes are processed in date order, and a note is never processed while an
earlier one is pending, because what a note answers, replaces, or corrects
depends on the notes before it. So a failed note holds the notes after it:

- `brain jot` and the `jot` tool process the new note after any earlier
  pending ones, and say so when it has to wait;
- the worker tries a failed note again after 1 minute, then 2, 4, and so on
  up to 30; `brain process` tries at once;
- `brain rebuild` stops at the first note that fails, and `brain process`
  continues from there;
- `brain process --skip NOTE_ID` leaves a note out of the wiki for good (it
  is recorded as processed with no statements; `brain rebuild` tries it
  again). Nothing is left out automatically, because an error that looks
  specific to one note is often a setting that is wrong for all of them.

A note that arrives with a date before notes already processed (a file with
an old modification time) is processed, and the change log says that
`brain rebuild` is needed to apply it in order.

`structured_output = "auto" | "tool" | "native" | "prompted"` maps to
PydanticAI's output modes; gateways differ in which they support, and
`brain doctor` tests each. Chat calls use `pydantic-ai-slim[openai]`;
embeddings use the `openai` SDK. Both share one TLS configuration (`[tls]`:
the OS trust store by default, or `ca_bundle`, plus an optional client
certificate), which S3 uses too.

Tests run every layer against a scripted fake model
(`brain.core.llm.fake`): PydanticAI's `FunctionModel` answering each prompt
from simple rules, and deterministic bag-of-words embeddings.
`BRAIN_FAKE_LLM=1` runs the CLI against it.

## 9. Index

The index stores what each layer wrote, by unit: a note (its file, layer
files, and embeddings), an entity (its record and page), or a dream run. A
changed file rebuilds exactly the rows of its unit, so the index depends only
on the current files. Tables: `notes`, `statements`, `entities`, `names`,
`mentions`, `relations`, `operations`, `events`, `pages`, `embeddings`,
`fts`, `vec_items`.

What is true now is computed when read, by `World` (`index/queries.py`):

- merges are followed to the surviving entity;
- a statement moved by a `not_part_of` note counts in its new thread;
- each statement gets a status: `retracted`, `superseded`, `answered`,
  `repeat`, or `current`. A retraction that was itself retracted does
  nothing, and relations from a retracted statement are ignored.

Sync: every write through brain replaces `_system/generation.json` with a new
random token. If it equals the one recorded in the index, listing the store
is skipped. Otherwise the store is listed, compared with the manifest by
ETag, and the affected units are rebuilt. A full listing also runs at least
once a day. There are no migrations: a schema-version or embedding-model
mismatch rebuilds the index.

The tool runs on uv-managed Python, whose SQLite has FTS5 and allows
extension loading, which sqlite-vec needs. `brain doctor` checks all three.

## 10. Storage and the lease

`Storage` has five operations (`get`, `put`, `list`, `delete`, `exists`);
`list` returns ETag, size, and modification time, and `put` takes `if_absent`
and `if_match` for conditional writes. `LocalStorage` and `S3Storage` (boto3)
implement it.

The lease is a small JSON object, `_system/lease.json`, that records which
machine may write and until when. S3 has no locks, so without it two machines
pointed at one bucket would overwrite each other's files with no error. It
belongs to a machine, not a process: `brain start` and CLI commands on one
machine share it and take turns with a local file lock. It is taken with a
conditional write, renewed while in use, and expires after
`[lease] ttl_seconds`. On exit a process deletes it only if no other brain
process on the machine is using it (each holds a shared lock on a local file
while it does); otherwise `brain jot` exiting would let another machine
write while `brain start` still believed it held the lease. Some S3-compatible stores ignore `If-None-Match`; a
probe on first write detects that, and the lease falls back to write, wait,
and read back.

## 11. Reading

- **`ask`** retrieves statements by keyword (FTS5) and meaning (vectors),
  fused by reciprocal rank, plus the summaries of the threads they belong to.
  Retracted statements are never shown to the model; superseded ones are
  shown marked. Every claim must cite note ids from the input.
- **`brief`** takes a page name or a request in plain words. One exact match
  needs no extra call; otherwise one routing call picks up to four pages from
  candidates, or asks which was meant. The brief covers each page's summary
  and statements, and marks what is new since the last brief on it.
- **`brain context`** is a short, dated summary for agent sessions: active
  threads with their Now lines, the newest open items, and recent changes,
  within `[context] max_lines` and `max_bytes`. It is rendered on every read.
- **The change log** (`brain log`, `wiki/log.md`, the web UI) lists what was
  created, linked, merged, closed, answered, or superseded after each note
  and in each dream pass. It replaces review: it is where to see a mistake
  and decide to write a correction note.

## 12. Front-ends

**CLI.** `jot`, `process`, `rebuild`, `active`, `wiki`, `show`, `log`,
`ask`, `brief`, `search`, `context`, `dream`, `start`, `mcp`, `sync`,
`reindex`, `doctor`, `init`. `brain jot --about PAGE` writes a correction to
a page.

**Web** (`brain start`, 127.0.0.1 only). The home page is the note box, the
active threads, and recent changes. The only write in the API is
`POST /api/notes`. Two checks stop other websites in the browser from using
it: the Host header must be localhost, and every write must be JSON.

**MCP.** `get_context`, `brief`, `ask`, `search`, `get_page`, `get_note`,
`list_active`, and `jot`, over stdio (`brain mcp`) and over HTTP at `/mcp`
inside `brain start`. `brain mcp` puts the injected context in the server
instructions, so an agent session starts with it.

**Worker.** `brain start` runs a background thread with its own index
connection. It processes pending notes on startup, when a note is saved, and
every `[worker] poll_seconds`; it dreams when a round had nothing to process
and a pass is due.

## 13. Distribution, CI, and doctor

- Not published to PyPI. Install from a release wheel (the web UI is
  included) or from a clone (`uv tool install --managed-python --python 3.12 .`,
  which builds the web UI and needs Node 22+). The dist name is
  `brain-journal`; the command is `brain`. `requires-python = ">=3.11"`.
- CI (GitHub Actions; each job is a shell script in `ci/`): lint and type
  check (ruff, pyright); tests on macOS and Ubuntu with Python 3.11 and 3.13;
  tests in a UBI 9 container; S3 contract tests against MinIO and moto;
  frontend typecheck, tests, and build. Pushing a `v*` tag builds the wheel
  and attaches it to a GitHub release.
- The code uses SHA-256 only, so it works with FIPS mode enabled; a test
  rejects MD5 and SHA-1 in the source.
- `brain doctor` checks the config, Python and SQLite capabilities, the index
  path (warns on a network filesystem), TLS files, storage access,
  conditional writes, the lease, the proxy settings, each configured model,
  structured-output modes, the embedding dimension, the index, files that
  failed to parse, notes that failed processing, and the last dream pass.
  Every failure says what to change.

## 14. Decisions

| # | Decision | Why |
|---|---|---|
| 1 | Notes are the only input; the wiki is generated | The tool is only useful if it is used often, and review queues were work that got skipped |
| 2 | Corrections are notes, applied by a later-wins rule | One input surface; a rebuild replays corrections with everything else |
| 3 | Many small calls instead of one large extraction | A small model is reliable when each answer is a choice from a list that code can check |
| 4 | A failed call takes a safe result instead of blocking | An unfiled statement or an extra provisional entity is cheap, and dreaming or a note repairs it |
| 5 | New entities and threads start provisional | One mention is not enough to earn a page; this keeps one-off names out of the wiki |
| 6 | Page lists are built by code; only Now and Summary are model-written | Lists cannot drift from the statements, and it saves calls |
| 7 | Every model-written sentence is checked against what it cites | Unsupported sentences are the failure a reader cannot see |
| 8 | The wiki is markdown files in the store | It can be read in any editor and travels with the notes |
| 9 | Effective state (merges, moves, statuses) is computed on read | Layer files stay append-only per note; nothing has to be rewritten when a later note changes things |
| 10 | Entity ids are kept across rebuilds | Page paths and links do not change when prompts improve |
| 11 | Dreaming uses the strong model when one is set | It runs rarely and makes the cross-note judgments where a wrong merge costs most |
| 12 | No migration from format 1 | Format 1 held curated files that format 2 has no place for; the notes can be re-entered |
| 13 | Files are the source of truth; SQLite is a derived index | Carried over from 0.1: the store can be read without brain, and the index can always be rebuilt |
| 14 | One machine writes at a time, enforced by a lease | Carried over from 0.1: S3 has no locks |

## 15. Not built

- Dreaming does not yet split an entity that was wrongly merged; a
  `not_same_as` note does it.
- Dreaming does not re-run layer 6 on old pairs. Stale open items are
  re-checked; superseded decisions are found only when the later note
  arrives.
- A fixed set of sample notes with expected results, run per layer against a
  real small model, to measure whether a prompt change helps.
- Attaching documents (PDFs and similar).
- Authentication for `brain start`, which listens on localhost only.
