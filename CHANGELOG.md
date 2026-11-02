# Changelog

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
