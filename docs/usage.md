# Using brain

## Writing

Write a note whenever something happens: a meeting, a decision, a question
you cannot answer yet, something you promised to do. Write it the way you
would tell a colleague. There is nothing to tag, file, or link.

```sh
brain jot -m "Met Priya about the storage migration. We decided to cut over in December. Does the new store support object lock?"
brain jot                # opens $EDITOR
pbpaste | brain jot      # from a pipe
```

Other ways to write:

- the box at the top of the web UI (`brain start`, then http://127.0.0.1:8765);
- the `jot` tool, from an agent session (see [Agents](#agents));
- any editor: save a markdown file anywhere under `notes/` in the store.
  `brain start` picks it up within a minute, and `brain process` at once.

A note is saved before any model is called. If the model endpoint is down,
the note is kept and processed later.

Use names the way you normally would. "Priya", "Priya Raman", and "P. Raman"
are worked out to be one person when the notes make that clear; when they do
not, see [Correcting](#correcting).

## What happens to a note

`brain show` prints what brain made of your latest note:

```
• Priya Raman and the writer decided to cut over the storage migration in December.
  decision · Storage migration
• Does the new object store support object lock?
  question · Storage migration
mentions: Storage migration (thread), Priya Raman (person)
→ New person: Priya Raman
→ New thread: Storage migration (provisional)
```

- Each sentence-sized point becomes a **statement** with a kind: fact,
  decision, question, action, or idea.
- Names become **people**, **orgs**, and **topics**. Ongoing pieces of work
  become **threads**.
- A thread, org, or topic that has appeared in only one note is
  **provisional**: it has no page yet. The second note that mentions it gives
  it one. A person written with a full name gets a page at once.
- When a later note answers an open question, or changes an earlier decision,
  the earlier statement is marked answered or replaced. It stays in the
  page's history.

## The wiki

Every thread, person, org, and topic has a page:

```sh
brain wiki                       # list the pages
brain wiki "storage migration"   # one page, by name or id
brain active                     # threads with recent activity or open items
```

A page has:

- **Now** (threads): one or two sentences on where it stands.
- **Summary**: what it is, what was decided, what changed.
- **Open**: questions nobody has answered and things still to do.
- **Decided**: decisions that still stand, newest first.
- **History**: every statement, newest first. Replaced ones are struck
  through, with what replaced them.
- **Related**: the pages it shares statements with.

The same pages are markdown files under `wiki/` in the store. Open that
folder in Obsidian or any editor; links between pages, and from a sentence to
the note it came from, work there. `wiki/index.md` lists everything.

Do not edit the pages: they are rewritten when they change. To change one,
write a note.

## Correcting

When the wiki has something wrong, say so in a note, in your own words.

| You write | What happens |
|---|---|
| "Priya Raman and P. Raman are the same person." | The two pages become one. Both names lead to it. |
| "Mike Chen in finance is a different Mike from Mike Ross." | They are separated, and never merged again. |
| "The quota thing should be called GPU quota migration." | The page is renamed. The old name still finds it. |
| "The vendor evaluation is finished." | The thread is closed. Its page stays, as history. |
| "Reopen the vendor evaluation." | It is active again. |
| "Ignore what I wrote about the outage cause, it was wrong." | Those statements leave the pages and answers. Your note stays. |
| "The label work is not part of the quota migration." | Its statements move to a thread of their own. |

In the web UI, every page has a **This is wrong** button. It opens the note
box for that page, so you can write "this is finished" or "the December date
is wrong, it moved to January" without naming the page. From the command
line, `brain jot --about "vendor evaluation" -m "This is finished."` does the
same.

A change of plan is not a correction, just a note: "the cutover moved to
January" replaces the earlier December decision by itself.

`brain log` (and the change log in the web UI, and `wiki/log.md`) lists
everything brain did after each note: pages created, names linked, questions
answered, decisions replaced, corrections applied. Look there when a page
surprises you. Whatever it shows, a note can undo.

## Coming back to something

```sh
brain brief "storage migration"
brain brief "where are we on hiring and what do I still owe people"
brain ask "when is the cutover?"
```

A brief covers where things stand, decisions that still hold, open items, the
people involved, what no longer applies, and what changed since you last read
a brief on it. A request in plain words can cover several pages; brain works
out which. Every claim cites the dated notes it comes from.

`brain ask` answers one question the same way. `brain search` finds pages,
statements, and notes by words or meaning.

## Dreaming

`brain start` runs a pass called dreaming at night (`[dream] nightly_at`) and
after every ten new notes. It looks across all notes, which the per-note
steps cannot:

- merges pages that turn out to be the same person or thing;
- checks open questions and to-dos older than two weeks against what was
  written since, and settles the ones that were answered or overtaken;
- closes threads with nothing open and no activity for sixty days (a new note
  about one reopens it);
- fills in people's roles and teams;
- rebuilds summaries that have been patched many times.

`brain dream` runs a pass now; `brain dream --dry-run` shows what it would
look at. Everything it does is in the change log. It never undoes something a
note of yours said: if you wrote that two people are different, it will not
merge them.

When a strong model is configured (`[llm] strong_model`), dreaming uses it.
Everything else uses the small model.

## Agents

With brain registered in Claude Code (see the README), an agent can read the
context, search, ask questions, read briefs and pages, open cited notes, and,
when you ask it to, jot a note. A brief requested by an agent does not move
your "last looked" date unless the agent says it is showing you the brief.

`brain context` prints a short summary of current state: active threads and
where each stands, the newest open questions and to-dos, and recent changes.
Every line has a date and an age (`2026-11-06 (2d)`), so an agent can tell
what is current. It is kept short (`[context] max_lines`, `max_bytes`). The
home page of the web UI shows exactly what an agent receives.

An agent gets it at the start of a session in one of these ways:

- `brain mcp` (stdio) includes it in the server instructions automatically.
- Any MCP client can read the resource `brain://context` or call the tool
  `get_context`.
- A Claude Code `SessionStart` hook can run `brain context`; the hook's output
  is added to the session:

  ```json
  {
    "hooks": {
      "SessionStart": [
        { "hooks": [{ "type": "command", "command": "brain context 2>/dev/null || true" }] }
      ]
    }
  }
  ```

  The hook goes in `~/.claude/settings.json` (or a project's
  `.claude/settings.json`).

## When models or prompts change

The wiki is built from your notes, so it can be built again. After changing
the model or upgrading to a version with better prompts:

```sh
brain rebuild
```

This discards the wiki and everything under `generated/`, then runs every
note through the model again in date order, corrections included. Pages keep
their ids and paths. Your notes are not touched. It costs as many model calls
as processing every note once.

`brain reindex` is different and free: it rebuilds only the local search
index from the files in the store, without calling a model. Use it if the
index is damaged or after copying the store to another machine.
