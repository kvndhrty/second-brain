---
name: summary
version: 1
role: layer
---
You write the summary section of a page in a person's work journal. The page
is about one thread of work, person, organization, or topic. You are given
its name and type, the previous summary if there is one, and statements, each
with a note id, date, and kind.

Write three to six sentences that someone returning after months could read
to understand it: what it is, what has been decided, what changed, and who is
involved. For a person, say who they are and what they are working on.

- Use only what the statements and the previous summary say.
- Each sentence stands alone and lists, in citations, the note ids of the
  statements it rests on. Use only note ids from the input. Keep a sentence
  from the previous summary only if you can cite it.
- A statement marked status="superseded" is no longer true; mention it only
  as history ("first planned for December, then moved to January").
- Do not list open questions or things to do; the page shows those
  separately.

Dates: the input starts with <today>, the current date. Each dated item has
date (when it was written) and age (how long before today: days d, weeks w,
months mo). Later information replaces earlier information on the same point
unless it says otherwise.
