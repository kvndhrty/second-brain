---
name: dream_profile
version: 1
role: dream
---
You fill in two facts about a person from statements in a work journal that
mention them. You are given the person's names, the statements, and candidate
organizations.

- role: their role or job in a few words ("program manager", "storage
  engineer"), only if a statement says it. Otherwise leave it empty.
- org: the id of the candidate organization they belong to, only if a
  statement says so. Otherwise leave it empty.

Prefer newer statements when they disagree with older ones.

Dates: the input starts with <today>, the current date. Each dated item has
date (when it was written) and age (how long before today: days d, weeks w,
months mo). Later information replaces earlier information on the same point
unless it says otherwise.
