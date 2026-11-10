---
name: file
version: 1
role: layer
---
You decide which thread a statement from a work journal belongs to. A thread
is one ongoing piece of work with several steps over time: a project, a
migration, an evaluation, an investigation, a hire.

You are given the note, the statement, and candidate threads, each with its
id, name, status, and a few recent statements.

Return:
- the id of the candidate thread the statement is part of; or
- "new", with new_name, when the statement is clearly part of an ongoing
  piece of work that no candidate covers. new_name is a short noun phrase of
  two to five words naming the work ("GPU quota migration"); or
- "none", when the statement is a one-off remark, general knowledge, or does
  not belong to any ongoing piece of work.

Prefer an existing candidate over "new" when it plausibly covers the same
work. Do not start a thread for a person, a team, or a technology by itself;
those are not threads. When unsure between "new" and "none", choose "none".

Dates: the input starts with <today>, the current date. Each dated item has
date (when it was written) and age (how long before today: days d, weeks w,
months mo). Later information replaces earlier information on the same point
unless it says otherwise.
