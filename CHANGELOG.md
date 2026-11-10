# Changelog

## 0.2.0

Notes in, wiki out. The person only writes notes; models build and maintain
the wiki. This is a new store format (2) with no migration from 0.1: start
with an empty store.

- The wiki (threads, people, orgs, topics) is written by the model as
  markdown files under `wiki/`, readable in any editor, and shown by
  `brain wiki` and the web UI.
- Processing a note is split into small steps, each a choice from a list
  that code checks: split into statements, label each, list the names, link
  names to known people and things, file statements under threads, work out
  what the note answers or replaces, write the page's Now line and summary,
  and check each written sentence against what it cites. A small model is
  enough. A step whose answer never passes its checks takes a safe result
  instead of blocking the note.
- Corrections are notes: "X and Y are the same person", "X is different
  from Y", "call it Z", "this is finished", "reopen it", "ignore what I wrote
  about X", "X is not part of Y". A later note wins over an earlier one, and
  a note wins over anything the model inferred. Every page in the web UI has
  a "This is wrong" button that writes such a note.
- Dreaming merges duplicate pages, settles old open questions and to-dos
  that later notes answered, closes quiet threads, fills in people's roles,
  and rebuilds worn summaries. It uses `[llm] strong_model` when one is set.
- `[llm]` now has `small_model` and an optional `strong_model`, with
  `dream_uses`, `answer_uses`, and `escalate` to choose where the strong one
  is used. `chat_model`, `extract_model`, and `doc_model` are gone.
- New commands: `brain wiki`, `brain log`, `brain search`, `brain rebuild`
  (run every note again after a prompt or model change). `brain jot --about
  PAGE` writes a correction to a page.
- A markdown file saved anywhere under `notes/` becomes a note.
- The change log (`brain log`, `wiki/log.md`, the web UI) lists what brain
  did after each note and in each dream pass.
- Removed: the review queue, the tidy-up sweep, thread commands, fragment
  status commands, `brain reextract`, the graph page, and decisions files.
  MCP tools are now `get_context`, `brief`, `ask`, `search`, `get_page`,
  `get_note`, `list_active`, and `jot`.

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
