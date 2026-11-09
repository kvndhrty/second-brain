# Changelog

## Unreleased

- `brief` takes a request in plain words. A request that does not name
  exactly one thing is routed by the model, which picks one or more threads,
  people, orgs, or themes, or asks which one you mean.
- A name shared by a thread and a theme resolves to the thread, and
  extraction no longer creates themes named after their own thread.
- Dreaming no longer proposes an answered question as overcome by events.
  Now lines are capped at 35 words.
- Extraction is told about people and orgs still waiting in review, skips
  generic organizations ("the vendor"), and records each point once. A new
  name in review offers links to people created since it was queued.

## 0.1.0

First version.

- Entries as markdown files in a local directory or S3, with a single-writer
  lease and a local SQLite index (FTS5 and sqlite-vec) rebuilt from the files.
- Extraction into fragments, people, orgs, themes, and threads through any
  OpenAI-compatible endpoint, with CA bundle and mTLS support; new people and
  orgs wait for review.
- `ask` and `brief` with citations to dated entries; `active`; thread
  operations; the overcome-by-events sweep.
- CLI, web UI (`brain start`), and MCP server (stdio and HTTP).
- `brain doctor` for checking a new machine.
