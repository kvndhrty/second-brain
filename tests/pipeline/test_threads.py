"""Thread operations and the overcome-by-events sweep."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from brain.core.formats.entity import Thread
from brain.core.index.db import snapshot
from brain.core.services import FragmentChange
from brain.core.threads import ThreadError

QUOTA = (
    "Kicked off the [[GPU quota migration]] with Priya. Does the vendor support MIG on the nodes? "
    "Maybe the new pool is faster. Need to file the [[GPU quota migration]] budget request."
)


def jot(b, text):
    eid = b.jot(text).entry.id
    assert not b.process(eid).failed
    return eid


def rows(b, sql, *args):
    return [tuple(r) for r in b.db.conn.execute(sql, args).fetchall()]


def thread_of(b, fid):
    return sorted(r[0] for r in rows(b, "SELECT dst FROM edges WHERE src = ? AND type = 'in_thread'", f"frag:{fid}"))


def test_rename_keeps_old_name_as_alias(brain):
    jot(brain, QUOTA)
    brain.threads.rename("gpu-quota-migration", "Quota pool move")
    t = brain.vault.get_entity("thread", "gpu-quota-migration")
    assert t.name == "Quota pool move" and "GPU quota migration" in t.aliases
    # a later entry using the old name still lands in the same thread
    e2 = jot(brain, "More on the [[GPU quota migration]] today.")
    assert thread_of(brain, f"f-{e2}-01") == ["thread:gpu-quota-migration"]


def test_pin_archive_activate(brain):
    jot(brain, QUOTA)
    later = datetime.now().astimezone() + timedelta(days=90)
    assert brain.active_threads(now=later) == []
    brain.threads.pin("gpu-quota-migration")
    assert [t.id for t in brain.active_threads(now=later)] == ["gpu-quota-migration"]
    brain.threads.set_state("gpu-quota-migration", "archived")
    assert brain.active_threads() == []
    brain.threads.set_state("gpu-quota-migration", "active")
    assert [t.id for t in brain.active_threads()] == ["gpu-quota-migration"]


def test_obe_thread_with_cascade(brain):
    eid = jot(brain, QUOTA)
    n = brain.threads.obe("gpu-quota-migration", reason="project cancelled", cascade=True)
    assert n == 1  # the budget action item; the question is not in the thread
    t = brain.vault.get_entity("thread", "gpu-quota-migration")
    assert t.state == "obe" and t.obe.reason == "project cancelled"
    assert rows(brain, "SELECT status, obe_reason FROM fragments WHERE id = ?", f"f-{eid}-04") == [
        ("overcome_by_events", "thread 'GPU quota migration' overcome by events: project cancelled")
    ]
    assert brain.active_threads() == []
    brief = brain.brief("GPU quota migration")
    assert 'state="overcome_by_events"' in brain.llm.calls[-1][1] and brief.type == "thread"
    brain.threads.set_state("gpu-quota-migration", "active")
    assert brain.vault.get_entity("thread", "gpu-quota-migration").obe.reason is None


def test_merge_follows_chain_and_refuses_cycles(brain):
    jot(brain, "Started [[Alpha]].")
    jot(brain, "Started [[Beta]].")
    jot(brain, "Started [[Gamma]].")
    brain.threads.merge("beta", "alpha")
    brain.threads.merge("gamma", "beta")  # beta is merged, so gamma goes to alpha
    assert brain.vault.get_entity("thread", "gamma").merged_into == "alpha"
    assert set(brain.vault.get_entity("thread", "alpha").aliases) >= {"Beta", "Gamma"}
    with pytest.raises(ThreadError):
        brain.threads.merge("alpha", "beta")
    assert brain.brief("Alpha").fragment_count == 3
    with pytest.raises(ThreadError):
        brain.threads.set_state("beta", "archived")


def test_split_moves_fragments_and_survives_reextract_and_reindex(brain):
    e1 = jot(brain, QUOTA)
    budget = f"f-{e1}-04"
    new = brain.threads.split("gpu-quota-migration", [budget], "Budget requests", "Money asks")
    assert new.id == "budget-requests" and new.state == "active"
    assert thread_of(brain, budget) == ["thread:budget-requests"]
    assert thread_of(brain, f"f-{e1}-01") == ["thread:gpu-quota-migration"]
    brain.reextract([e1])
    assert thread_of(brain, budget) == ["thread:budget-requests"]
    before = snapshot(brain.db)
    brain.reindex()
    assert snapshot(brain.db) == before
    with pytest.raises(ThreadError, match="not in thread"):
        brain.threads.move([f"f-{e1}-02"], "gpu-quota-migration", "budget-requests")
    with pytest.raises(ThreadError):
        brain.threads.split("gpu-quota-migration", [], "Nothing")


def test_sweep_lists_stale_items_until_decided(brain):
    eid = jot(brain, QUOTA)
    brain.vault.put_entity(Thread(id="quiet", name="Quiet work"))
    jot(brain, "Started [[Quiet work]].")
    now = datetime.now().astimezone() + timedelta(days=45)
    kinds = sorted((i.kind, i.fragment.id if i.fragment else i.thread_id) for i in brain.sweep(now=now, quiet_days=30))
    assert kinds == [
        ("quiet_thread", "gpu-quota-migration"),
        ("quiet_thread", "quiet"),
        ("stale_open", f"f-{eid}-02"),
        ("stale_open", f"f-{eid}-04"),
        ("stale_speculative", f"f-{eid}-03"),
    ]
    assert brain.sweep(now=datetime.now().astimezone()) == []  # nothing is old yet
    brain.set_fragment(f"f-{eid}-02", FragmentChange(state="open"))  # "keep open"
    brain.set_fragment(f"f-{eid}-03", FragmentChange(status="confirmed"))
    brain.threads.set_state("quiet", "archived")
    brain.threads.pin("gpu-quota-migration")
    left = [(i.kind, i.fragment.id if i.fragment else i.thread_id) for i in brain.sweep(now=now, quiet_days=30)]
    assert left == [("stale_open", f"f-{eid}-04")]
    brain.threads.obe("gpu-quota-migration")
    assert brain.sweep(now=now, quiet_days=30) == []  # items in retired threads are not listed
