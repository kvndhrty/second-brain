"""Every note given to a model carries its date and its age relative to today."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from importlib import resources

from brain.core import temporal
from brain.core.llm.prompts import load_prompt
from tests.conftest import set_now, write

DATED = re.compile(r'date="\d{4}-\d{2}-\d{2}" age="\d+(d|w|mo)"')
# Prompts whose input is a single note or sentence with nothing to compare by date.
UNDATED = {"split", "names", "check", "correction_op", "correction_targets", "probe"}


def test_age_is_days_then_weeks_then_months():
    today = date(2026, 11, 8)
    assert temporal.age("2026-11-08", today) == "0d"
    assert temporal.age("2026-10-30", today) == "9d"
    assert temporal.age("2026-10-20", today) == "2w"
    assert temporal.age("2026-06-01", today) == "5mo"
    assert temporal.today_el(datetime(2026, 11, 8, 12, tzinfo=UTC)).startswith('<today date="2026-11-0')


def test_prompts_that_compare_notes_explain_dates():
    names = [p.name[:-3] for p in resources.files("brain.prompts").iterdir() if p.name.endswith(".md")]
    for name in names:
        text = load_prompt(name).instructions
        assert ("the input starts with <today>" in text) == (name not in UNDATED), name


def test_model_inputs_start_with_today_and_date_every_statement(brain):
    write(brain, "Met Jane Doe about the [[GPU quota migration]]. We decided to migrate in December. Does MIG work?")
    write(brain, "Then Jane says MIG does work for the [[GPU quota migration]].", 3)
    write(brain, "We decided to migrate the [[GPU quota migration]] in January instead.", 5)
    set_now(brain, 20)
    brain.ask("When do we migrate?")
    brain.brief("GPU quota migration")
    brain.dream()
    seen = set()
    for name, user in brain.llm.calls:
        seen.add(name)
        if name in UNDATED:
            continue
        assert user.startswith("<today date="), name
        for tag in re.findall(r"<(?:statement|earlier|later|item|candidate) [^>]*note=[^>]*>", user):
            assert DATED.search(tag), (name, tag)
    assert {"classify", "link", "file", "relate", "now", "summary", "ask", "brief"} <= seen
    # Ages are counted from today, not from when the note was processed.
    ask_input = next(user for name, user in reversed(brain.llm.calls) if name == "ask")
    assert '<today date="2026-10-21"' in ask_input and 'date="2026-10-01" age="2w"' in ask_input
