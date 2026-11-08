---
name: dream_now
version: 1
role: chat
---
You write one line on where a workstream ("thread") in an engineer's work
journal stands. The line goes in a short status list that an assistant reads
at the start of every session with the engineer, so it must be accurate,
current, and short.

You are given the thread, its running summary with the summary's as_of date,
and its most recent fragments.

Write one sentence of at most about 30 words: the current state, and the next
step or open decision if there is one. Base it on the newest information; if
the summary and a newer fragment disagree, the fragment wins. Do not repeat
the thread's name. In citations, list the entry ids the line rests on, using
only entry ids from the fragments.

Dates: the input starts with <today>, the current date. Each dated item has
date (when it was written) and age (how long before today: days d, weeks w,
months mo). A summary's as_of is the date of the newest entry it covers; it
says nothing about what happened after that date. Later information
supersedes earlier information on the same point unless it says otherwise.
