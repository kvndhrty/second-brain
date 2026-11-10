---
name: correction_op
version: 1
role: layer
---
A person wrote an instruction about their work journal. Decide which one
operation it asks for.

- same_as: two names are the same person, organization, topic, or piece of
  work ("Jane Doe and J. Doe are the same person").
- not_same_as: two names are different people or things and must be kept
  apart ("Mike Chen in finance is a different Mike from Mike Ross").
- rename: something should be called by a new name ("call the quota thing GPU
  quota migration").
- close: a piece of work is finished, done, cancelled, or closed.
- reopen: a piece of work that was closed is active again.
- retract: something written earlier is wrong and should be ignored.
- not_part_of: something does not belong to a piece of work it was filed
  under ("the label work is not part of the quota migration").
- none: the instruction is none of these.
