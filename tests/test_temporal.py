"""Temporal context: every note given to a model carries its date and age."""

from __future__ import annotations

import os
import re
import time
from datetime import UTC, date, datetime, timedelta

import pytest

from brain.core.formats.entity import Person
from brain.core.temporal import age, as_of, iso_day, local_date, today_el
from tests.conftest import FAKE_LLM_SETTINGS, make_brain

# Elements that hold a note. Each must say when it was written (date) or what
# it covers (as_of), and how old that is; entities say when they were last mentioned.
DATED = ("fragment", "excerpt", "item", "entry", "context", "later")
AS_OF = ("thread_summary", "previous_summary")
ENTITIES = ("candidate", "person")


@pytest.fixture
def los_angeles():
    old = os.environ.get("TZ")
    os.environ["TZ"] = "America/Los_Angeles"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ")
    else:
        os.environ["TZ"] = old
    time.tzset()


def test_ages_are_days_then_weeks_then_months():
    today = date(2026, 11, 8)
    assert age(date(2026, 11, 8), today) == "0d"
    assert age(date(2026, 10, 30), today) == "9d"
    assert age(date(2026, 10, 18), today) == "3w"
    assert age(date(2026, 6, 1), today) == "5mo"
    assert age(date(2026, 11, 9), today) == "0d"  # clock skew never gives a negative age


def test_dates_are_local_calendar_dates(los_angeles):
    evening = "2026-11-07T04:30:00+00:00"  # 8:30 pm on the 6th in Los Angeles
    assert iso_day(evening) == "2026-11-06"
    assert local_date(datetime(2026, 11, 7, 4, 30, tzinfo=UTC)) == date(2026, 11, 6)
    assert iso_day("2026-11-07") == "2026-11-07"  # a calendar date stays as it is
    assert today_el(datetime(2026, 11, 8, 18, 0, tzinfo=UTC)) == '<today date="2026-11-08" weekday="Sunday"/>'
    assert as_of(None, date(2026, 11, 8)) == 'as_of="unknown"'


def _check(prompt: str, user: str) -> list[str]:
    problems = []
    if not user.startswith("<today "):
        problems.append(f"{prompt}: input does not start with <today>")
    for tag, attrs in re.findall(r"<(\w+)((?:\s+\w+=\"[^\"]*\")*)\s*/?>", user):
        if tag in DATED and not ("date=" in attrs and "age=" in attrs):
            problems.append(f"{prompt}: <{tag}{attrs}> has no date and age")
        if tag in AS_OF and "as_of=" not in attrs:
            problems.append(f"{prompt}: <{tag}{attrs}> has no as_of")
        if tag in ENTITIES and "last_mentioned=" not in attrs:
            problems.append(f"{prompt}: <{tag}{attrs}> has no last_mentioned")
    for line in re.findall(r"^- id: .*$", user, re.M):
        if "last activity:" not in line:
            problems.append(f"{prompt}: thread line has no last activity: {line}")
        if "summary" in line and "summary (as of" not in line:
            problems.append(f"{prompt}: thread summary has no as-of date: {line}")
    return problems


def test_every_model_input_carries_dates(local_storage, state, tmp_path):
    b = make_brain(local_storage, state, tmp_path, fake_llm=True, llm=FAKE_LLM_SETTINGS)
    try:
        b.settings.dream.rebuild_after_updates = 2
        b.vault.put_entity(Person(id="mike-chen", name="Mike Chen"))
        b.vault.put_entity(Person(id="mike-ross", name="Mike Ross"))
        start = datetime.now(UTC) - timedelta(days=60)
        for i, text in enumerate(
            (
                "Met Jane Doe about the [[GPU quota migration]]. Who owns the [[GPU quota migration]] runbook?",
                "We decided the [[GPU quota migration]] cutover is in March.",
                "Confirmed the vendor does support MIG on the new nodes. [[GPU quota migration]]",
                "The [[GPU quota migration]] cutover moved to May instead of March.",
                "Paired with Mike on the [[GPU quota migration]] rollout plan.",
            )
        ):
            b.vault.clock = lambda i=i: start + timedelta(days=12 * i)
            entry = b.jot(text).entry
            assert not b.process(entry.id).failed
        b.brief("gpu-quota-migration")
        b.brief("Jane Doe")
        b.brief("what is new with the GPU quota migration and Jane Doe?")
        b.ask("does the vendor support MIG?")
        b.dream()
        called = {name for name, _ in b.llm.calls}
        expected = {"extract", "resolve", "answers", "thread_summary", "brief", "brief_route", "ask"}
        expected |= {"dream_supersede", "dream_answers", "dream_now", "dream_sweep"}
        assert expected <= called, expected - called
        problems = [p for name, user in b.llm.calls for p in _check(name, user)]
        assert not problems, "\n".join(problems)
    finally:
        b.close()


def test_ask_orders_excerpts_oldest_first(local_storage, state, tmp_path):
    b = make_brain(local_storage, state, tmp_path, fake_llm=True, llm=FAKE_LLM_SETTINGS)
    try:
        for text in ("The vendor said MIG ships in March.", "The vendor now says MIG ships in May."):
            assert not b.process(b.jot(text).entry.id).failed
        b.ask("when does MIG ship?")
        (user,) = [u for name, u in b.llm.calls if name == "ask"]
        dates = re.findall(r'<excerpt entry="(e-[^"]+)"', user)
        assert dates == sorted(dates)
    finally:
        b.close()
