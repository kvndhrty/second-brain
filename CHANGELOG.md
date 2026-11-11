# Changelog

## 0.2.1

Fixes from an independent review of 0.2.0. The index is rebuilt on first run.
Statement ids changed, so run `brain rebuild` on a store that 0.2.0 wrote.

Wrong results:

- Notes are now always processed in date order. Before, a note saved while an
  earlier one was still pending or failed was processed first, and a
  replaced decision could stay current or a close could win over a later
  reopen. A failed note now holds the notes after it and is tried again;
  `brain process --skip NOTE_ID` leaves one out.
- A failure while writing a note's pages left the note marked as done, so
  the pages were never written. It is now tried again.
- A retracted statement could stay in a page's Summary when the summary
  could not be rewritten. The summary is now removed until it can be.
- Editing a note file could move a later note's retraction or "replaced by"
  onto a different sentence. Statement ids now come from the statement's
  words, not its position.
- Dreaming could merge two pages a note had said are different, when a third
  page was merged with one of them in the same pass.
- Names were matched as parts of words: a person named "Ann" was credited
  with statements containing "planning".
- Corrections are checked by type: a person cannot be merged into a thread,
  and only a thread can be closed. "X and Y are different" no longer takes X
  out of an unrelated page it was merged into. Undoing a merge no longer
  removes a name the surviving page had before it. A thread created by "X is
  not part of Y" kept no page after a rebuild.
- A correction note with several corrections is decided in full before any
  is applied, so a failed model call leaves nothing half done.
- With `structured_output = "auto"`, one invalid answer switched every later
  call to prompted JSON for the life of the process.

Lost or duplicated notes:

- A short command (`brain jot`) deleted the write lease that `brain start`
  on the same machine was still using, so another machine could write at the
  same time.
- A file being saved by an editor under `notes/` was taken half-written, and
  taken again when the editor saved. A file is now taken once it has not
  changed for 30 seconds.
- One file under `notes/` with a name brain does not use (for example in
  `.obsidian/` on S3) stopped all syncing and processing.
- `brain jot` and the `jot` tool could report a failure after the note was
  saved, so a retry saved it twice.
- Text typed into `$EDITOR` or piped to `brain jot` was lost if the command
  failed before saving. It is kept in a draft file, and the message says
  where.
- Cmd/Ctrl+Enter pressed twice in the web note box saved two notes.
- A complete note file put under `notes/` by hand was not noticed for up to
  ten minutes.
- A failing note was retried every minute without limit. The wait now
  doubles after each failure, up to 30 minutes.

Other:

- `pipeline/state.py` has one function per correction operation.
- The test suite went from 218 tests to 168: repeated checks were merged or
  removed, and tests were added for the defects above.

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
