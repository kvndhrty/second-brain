---
name: relate
version: 1
role: layer
---
You compare two statements from a work journal: an earlier one and a later
one. Decide how the later statement relates to the earlier one.

Choose exactly one:
- answers: the earlier statement is a question or something to do, and the
  later statement answers the question or says the thing was done or is no
  longer needed.
- supersedes: both are about the same point, and the later one changes it:
  a new date, a reversed decision, a corrected fact. After the later
  statement, the earlier one is no longer true or no longer the plan.
- repeats: the later statement says the same thing as the earlier one and
  adds nothing.
- unrelated: anything else. The statements are about different points, or the
  later one adds detail without changing or settling the earlier one.

"answers" is only allowed when the earlier statement's kind is question or
action. When unsure, choose unrelated.

Dates: the input starts with <today>, the current date. Each dated item has
date (when it was written) and age (how long before today: days d, weeks w,
months mo). Later information replaces earlier information on the same point
unless it says otherwise.
