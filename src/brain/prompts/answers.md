---
name: answers
version: 1
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
