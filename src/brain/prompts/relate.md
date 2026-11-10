---
name: relate
version: 2
role: layer
---
You compare two statements from a work journal: an earlier one and a later
one. Decide how the later statement relates to the earlier one.

Choose exactly one:
- answers: the earlier statement is a question or something to do, and the
  later statement answers the question, or says the thing was done or is no
  longer needed.
- supersedes: the later statement changes the very point the earlier one
  makes: a new date for the same event, a reversed decision, a different
  number for the same thing. After the later statement, the earlier one is
  wrong or out of date.
- repeats: the later statement says the same thing as the earlier one and
  adds nothing.
- unrelated: anything else.

Test for supersedes: read the earlier statement on its own. Is it still true
after the later statement? If yes, choose unrelated. Being about the same
piece of work is not enough: "the migration was kicked off with Priya" is
still true after "the cutover moved to January", so that pair is unrelated.
A statement that adds detail, gives a reason, or reports progress does not
supersede anything.

"answers" is only allowed when the earlier statement's kind is question or
action. When unsure, choose unrelated.

Dates: the input starts with <today>, the current date. Each dated item has
date (when it was written) and age (how long before today: days d, weeks w,
months mo). Later information replaces earlier information on the same point
unless it says otherwise.
