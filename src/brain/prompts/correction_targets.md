---
name: correction_targets
version: 1
role: layer
---
A person wrote an instruction about their work journal, and it has already
been decided which operation it asks for. Choose what the operation applies
to, from the candidates. Use only candidate ids.

By operation:
- same_as: the two candidates that are the same. Put first the one whose
  name should be kept.
- not_same_as: the two candidates that are different.
- rename: the one candidate to rename, and new_name, the new name exactly as
  the instruction gives it.
- close, reopen: the one candidate that is finished or active again.
- retract: every candidate statement the instruction says is wrong or should
  be ignored.
- not_part_of: first the candidate that does not belong, then the candidate
  it does not belong to.

When written_from_page is given, the instruction was written on that page:
"this", "it", or "this page" in the instruction means that candidate.

If the candidates do not include what the instruction is about, return no
targets.
