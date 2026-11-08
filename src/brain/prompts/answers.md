---
name: answers
version: 2
role: extract
---
You are given new fragments from a journal entry and earlier open items (open
questions and action items) from previous entries.

Return a link for each new fragment that answers an earlier open question, or
reports that an earlier action item was done or dropped. Only link when the new
fragment directly addresses the earlier item; a shared topic is not enough.
These links are suggestions that a person will confirm, so return nothing
rather than a weak guess. confidence is your probability, from 0 to 1, that
the link is correct.

Dates: the input starts with <today>, the current date. Each dated item has
date (when it was written) and age (how long before today: days d, weeks w,
months mo). A summary's as_of is the date of the newest entry it covers; it
says nothing about what happened after that date. Later information
supersedes earlier information on the same point unless it says otherwise.
An open item can only be answered by something written after it.
