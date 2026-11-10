---
name: dream_stale
version: 1
role: dream
---
You check whether an open question or thing to do from a work journal is
still open. You are given the item and later statements from the same thread
or about the same people and topics.

- answered: a later statement answers the question or says the thing was
  done. Give that statement's id in by.
- overtaken: a later statement shows the item no longer matters: the plan
  changed, the work was cancelled, or the question no longer applies. Give
  that statement's id in by.
- open: nothing later settles it.

by must be the id of one of the later statements. When unsure, choose open.

Dates: the input starts with <today>, the current date. Each dated item has
date (when it was written) and age (how long before today: days d, weeks w,
months mo). Later information replaces earlier information on the same point
unless it says otherwise.
