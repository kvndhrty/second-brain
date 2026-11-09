# Using brain

## Writing

Write a few paragraphs whenever something happens: a meeting, a decision, a
dead end, an idea. Write the way you would in a notebook. brain splits each
entry into fragments (observations, decisions, questions, ideas, claims,
action items), notes whether each is confirmed or speculative, and links them
to people, organizations, themes, and threads.

```sh
brain jot                     # opens $EDITOR
brain jot -m "short note"
echo "from a script" | brain jot
```

In the web UI, use "New entry". Entries can be edited later; editing re-runs
extraction, and anything you changed about the old fragments carries over.

## Reviewing names

New people and organizations are not created until you confirm them, because
two records for one person split their history. `brain review` (or the Review
page) shows each new name with the sentence it came from. You can:

- create it (with a role and organization for people),
- link it to someone who already exists (the name becomes an alias, so it
  links automatically next time),
- mark it as not a person or organization.

A decision can apply to every entry with the same name. Proposed themes and
threads are already in use; accept, rename, or merge them when convenient.
Proposed answers ("this new fragment seems to answer that earlier question")
close the earlier item only if you accept them.

## Coming back to something

- `brain active` (or the Active page): workstreams with recent activity.
  Threads you pin stay listed; others drop off after `threads.inactivity_days`.
- `brain brief "GPU quota migration"`: where things stand, recent decisions,
  open items, who is involved, what is speculative or overcome by events, and
  what changed since you last asked. Works for threads, people, orgs, and
  themes by name or id, with no extra model call. Every claim cites the
  entries it comes from.
- `brain brief "where are we on the GPU move, and what is Priya waiting on?"`:
  a request in plain words. The model picks the threads, people, orgs, or
  themes it is about (several if it asks about several), and the brief
  answers the request first. If the request could mean two different things,
  brief asks which one and lists the ids to use.
- `brain ask "when did we decide to move the eval cluster?"`: a direct answer
  with citations.

## Keeping it true

Things go stale. A question gets answered elsewhere, a plan changes, a rumor
is never confirmed, a project is cancelled.

- One fragment: `brain status <fragment-id> --obe --reason "vendor left"`,
  `--closed`, `--speculative`, `--confirmed`, `--someday` (a big idea not tied
  to current work; these are listed on the Someday page). In the web UI, hover
  a fragment for the same actions. `brain show` lists an entry's fragment ids.
- A whole thread: `brain thread obe <thread> --reason "..." --cascade` marks
  the thread overcome by events and, with `--cascade`, its open items too.
  `brain thread archive` is for work that finished normally.
- The sweep: `brain sweep` (or "Tidy up" in the web UI) goes through open
  questions and action items older than 30 days, speculation that was never
  confirmed, and threads with no activity for 60 days. Each one can be marked
  overcome by events, closed, confirmed, or kept. Items you have decided on
  are not shown again.

All of these are stored in files in the store (the entry's decisions file, or
the thread file), so they survive re-extraction and reindexing.

## Dreaming

`brain dream` is an offline pass that does the tidying you would otherwise do
by hand, using the model. For each thread with something new since the last
pass, it:

- rebuilds the thread summary from all of its fragments, every
  `[dream] rebuild_after_updates` new entries, so errors in the running summary
  do not build up;
- looks for earlier fragments that a later one replaced: a plan that changed, a
  date that moved. Each appears in `brain review` as "Has this been overcome by
  events?" with both fragments and their dates;
- looks for open questions and action items that later entries answered. These
  appear in review as proposed answers;
- writes one line on where the thread stands, which agents see at the start of
  every session (below).

It also checks old items in the sweep, so `brain sweep` shows a verdict next to
each one ("dream: probably resolved", with the reason).

Nothing you wrote changes, and nothing is marked closed or overcome by events
until you accept it. `brain dream --dry-run` lists what a pass would do and how
many model calls it would take; a pass stops after `[dream] max_calls` and the
next one continues. `brain start` dreams on its own after
`[dream] after_entries` new entries or at `[dream] nightly_at` (local time), and
the Active page has a "Dream now" button. `brain doctor` shows the last pass.

## Threads

Threads are created as the work emerges; brain proposes them, you adjust:

```sh
brain thread list [--all]
brain thread rename <id> "Better name"      # the old name stays as an alias
brain thread pin <id> / unpin <id>
brain thread merge <id> <into>              # its fragments count toward <into>
brain thread split <id> <fragment-ids...> --name "New thread"
brain thread move <from> <to> <fragment-ids...>
brain thread show <id>                      # fragments with their ids
```

The thread page in the web UI has the same actions; "Split…" lets you pick
fragments in the timeline.

## Agents

With brain registered in Claude Code (see the README), an agent can search
the journal, ask questions, read briefs, open cited entries, and, when you ask
it to, record an entry. A brief requested by an agent does not move your
"last looked" date unless the agent says it is showing you the brief.

`brain context` prints a short summary of current state: active threads and
where each stands, the newest open items, recent decisions and things that no
longer hold, and what waits on you. Every line has a date and an age
(`2026-11-06 (2d)`), so an agent can tell what is current. It is kept short
(`[context] max_lines`, `max_bytes`). The Active page shows exactly what an
agent receives.

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
  `.claude/settings.json`). If that file is generated, for example by chezmoi,
  add it to the source instead.

## When models or prompts change

`brain reextract --stale-prompts` re-runs extraction for entries made with an
older prompt version; `brain reextract --since 2026-10-01` or with entry ids
re-runs specific ones. Your decisions are kept: fragments are matched to their
previous ids, and decisions that no longer match anything appear in review.
