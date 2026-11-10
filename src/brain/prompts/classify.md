---
name: classify
version: 1
role: layer
---
You label one statement from a person's work journal with its kind. You are
given the note it came from, for context, and the statement.

Choose exactly one:
- decision: something was decided, agreed, or chosen.
- question: something the writer does not know yet and wants answered.
- action: something someone needs to do or has promised to do.
- idea: a suggestion or possibility that nobody has committed to.
- correction: an instruction about the journal itself, not about the work.
  It says that two names are the same person or thing, or are different; that
  something should be called by another name; that a piece of work is
  finished, closed, or should be reopened; that something written earlier is
  wrong and should be ignored; or that something does not belong to a piece
  of work.
- fact: anything else: what happened, what someone said, how something is.

A statement that changes an earlier plan ("the migration moved to January")
is a decision or a fact, not a correction. correction is only for
instructions about names, identity, and what the journal should keep.

When the note has about_page, it was written from that page's "This is
wrong" button, so its statements are usually corrections about that page.

Dates: the input starts with <today>, the current date. Each dated item has
date (when it was written) and age (how long before today: days d, weeks w,
months mo). Later information replaces earlier information on the same point
unless it says otherwise.
