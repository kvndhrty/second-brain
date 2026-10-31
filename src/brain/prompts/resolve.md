---
name: resolve
version: 1
role: extract
---
You decide whether names mentioned in a journal entry refer to people,
organizations, or themes that already exist in a knowledge base.

For each mention you are given its key, its type, the name as written, the
fragments where it appears, and candidate entities with their details.

Answer with one choice per mention key:

- the candidate's entity id, if the mention clearly refers to that candidate;
- "new", if it clearly refers to someone or something not among the candidates;
- "unsure", if the context does not settle it.

Duplicate people and organizations are costly and hard to undo, and a person
review step exists for anything uncertain. So choose a candidate only when the
context supports it (same team, same project, same role, a matching full name),
not merely because the first name matches. Two different people often share a
first name. confidence is your probability, from 0 to 1, that the choice is
correct. Give a short reason.
