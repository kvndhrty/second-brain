"""Thread operations: rename, pin, archive, overcome by events, merge, split, move.

Thread-level changes are written to the thread file. Moving fragments between
threads is written to each entry's decisions file (`threads: {add, remove}`),
so it survives re-extraction like any other decision.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections import defaultdict

from brain.core.formats.decisions import FragmentDecision, ThreadChange
from brain.core.formats.entity import ObeInfo, Thread
from brain.core.index import queries
from brain.core.index.build import norm_alias
from brain.core.storage.base import NotFound
from brain.core.vault import Vault


class ThreadError(Exception):
    pass


class ThreadOps:
    def __init__(self, vault: Vault, conn: sqlite3.Connection) -> None:
        self.vault = vault
        self.conn = conn

    def get(self, thread_id: str) -> Thread:
        try:
            t = self.vault.get_entity("thread", thread_id)
        except NotFound:
            raise ThreadError(f"no thread {thread_id}") from None
        assert isinstance(t, Thread)
        return t

    def _save(self, t: Thread) -> Thread:
        self.vault.put_entity(t)
        return t

    def rename(self, thread_id: str, name: str) -> Thread:
        t = self.get(thread_id)
        name = name.strip()
        if not name:
            raise ThreadError("a thread needs a name")
        if norm_alias(name) != norm_alias(t.name) and norm_alias(t.name) not in {norm_alias(a) for a in t.aliases}:
            t.aliases.append(t.name)  # keep the old name, so mentions of it still resolve
        t.name = name
        return self._save(t)

    def pin(self, thread_id: str, pinned: bool = True) -> Thread:
        t = self.get(thread_id)
        t.pinned = pinned
        return self._save(t)

    def set_state(self, thread_id: str, state: str) -> Thread:
        """archive ("archived") or reactivate ("active") a thread. Reactivating clears an OBE mark."""
        if state not in ("active", "archived"):
            raise ThreadError("state must be 'active' or 'archived'; use obe() or merge() for the others")
        t = self.get(thread_id)
        if t.state == "merged":
            raise ThreadError(f"{thread_id} is merged into {t.merged_into}; change that thread instead")
        t.state = state  # type: ignore[assignment]
        if state == "active":
            t.obe = ObeInfo()
        return self._save(t)

    def obe(
        self, thread_id: str, *, reason: str | None = None, on: dt.date | None = None, cascade: bool = False
    ) -> int:
        """Mark a whole thread overcome by events. With cascade, its open questions and
        action items are marked overcome by events too. Returns how many were."""
        t = self.get(thread_id)
        if t.state == "merged":
            raise ThreadError(f"{thread_id} is merged into {t.merged_into}; change that thread instead")
        when = on or dt.date.today()
        t.state = "obe"
        t.obe = ObeInfo(date=when, reason=reason)
        self._save(t)
        if not cascade:
            return 0
        family = [f"thread:{x}" for x in queries.thread_family(self.conn, thread_id)]
        open_items = [
            f
            for f in queries.fragments_linked_to(self.conn, family, ("in_thread",), limit=100_000)
            if f.state == "open" and f.status != "overcome_by_events"
        ]
        note = f"thread '{t.name}' overcome by events" + (f": {reason}" if reason else "")
        by_entry: dict[str, list[str]] = defaultdict(list)
        for f in open_items:
            by_entry[f.entry_id].append(f.id)
        for entry_id, fids in by_entry.items():
            d = self.vault.get_decisions(entry_id)
            for fid in fids:
                fd = d.fragments.get(fid) or FragmentDecision()
                fd.status = "overcome_by_events"
                fd.obe = ObeInfo(date=when, reason=note)
                d.fragments[fid] = fd
            self.vault.put_decisions(d)
        return len(open_items)

    def _root(self, thread_id: str) -> str:
        """Follow merged_into to the thread that is not merged."""
        seen = set()
        while True:
            t = self.get(thread_id)
            if t.state != "merged" or not t.merged_into:
                return thread_id
            if thread_id in seen:
                raise ThreadError(f"merge cycle at {thread_id}")
            seen.add(thread_id)
            thread_id = t.merged_into

    def merge(self, thread_id: str, into: str) -> Thread:
        """Merge a thread into another. Its fragments then count toward the target everywhere."""
        target_id = self._root(into)
        if target_id == thread_id or thread_id in queries.thread_family(self.conn, target_id):
            raise ThreadError("cannot merge a thread into itself or into a thread merged into it")
        t, target = self.get(thread_id), self.get(target_id)
        known = {norm_alias(target.name), *(norm_alias(a) for a in target.aliases)}
        for name in [t.name, *t.aliases]:
            if norm_alias(name) not in known:
                target.aliases.append(name)
                known.add(norm_alias(name))
        if t.pinned and not target.pinned:
            target.pinned = True
        self._save(target)
        t.state = "merged"
        t.merged_into = target_id
        t.pinned = False
        return self._save(t)

    def move(self, fragment_ids: list[str], from_thread: str, to_thread: str) -> int:
        """Move fragments from one thread (and threads merged into it) to another."""
        family = set(queries.thread_family(self.conn, from_thread))
        self.get(to_thread)
        frags = queries.fragments_by_id(self.conn, fragment_ids)
        missing = sorted(set(fragment_ids) - set(frags))
        if missing:
            raise ThreadError(f"no such fragments: {', '.join(missing)}")
        linked = {
            r[0][5:]
            for r in self.conn.execute(
                f"SELECT src FROM edges WHERE type = 'in_thread' AND dst IN ({','.join('?' * len(family))})",
                [f"thread:{x}" for x in family],
            )
        }
        outside = sorted(set(fragment_ids) - linked)
        if outside:
            raise ThreadError(f"not in thread {from_thread}: {', '.join(outside)}")
        by_entry: dict[str, list[str]] = defaultdict(list)
        for fid in fragment_ids:
            by_entry[frags[fid].entry_id].append(fid)
        for entry_id, fids in by_entry.items():
            d = self.vault.get_decisions(entry_id)
            for fid in fids:
                fd = d.fragments.get(fid) or FragmentDecision()
                change = fd.threads or ThreadChange()
                change.add = [x for x in change.add if x not in family]
                if to_thread not in change.add:
                    change.add.append(to_thread)
                change.remove = sorted((set(change.remove) | family) - {to_thread})
                fd.threads = change
                d.fragments[fid] = fd
            self.vault.put_decisions(d)
        return len(fragment_ids)

    def split(self, thread_id: str, fragment_ids: list[str], name: str, description: str | None = None) -> Thread:
        """Start a new thread from some of a thread's fragments."""
        if not fragment_ids:
            raise ThreadError("choose at least one fragment to split off")
        if not name.strip():
            raise ThreadError("the new thread needs a name")
        self.get(thread_id)
        new = self.vault.create_entity(
            "thread", name, lambda tid: Thread(id=tid, name=name.strip(), description=description or None)
        )
        self.move(fragment_ids, thread_id, new.id)
        assert isinstance(new, Thread)
        return new
