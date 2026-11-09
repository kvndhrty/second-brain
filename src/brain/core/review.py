"""The review queue: items the pipeline will not decide on its own.

Every decision is written to files (decisions files and entity files), never
only to the index, so it survives `reindex` and `reextract`.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from brain.core.formats.decisions import FragmentDecision
from brain.core.formats.entity import Membership, ObeInfo, Org, Person, Theme, Thread
from brain.core.formats.yamlio import FormatError
from brain.core.index.build import norm_alias
from brain.core.index.queries import entry_dates, fragments_by_id, fuzzy_candidates
from brain.core.storage.base import NotFound
from brain.core.temporal import iso_day
from brain.core.vault import Vault

MENTION_KINDS = ("new_person", "new_org", "ambiguous_mention")


@dataclass
class ReviewItem:
    id: str
    kind: str
    entry_id: str | None
    payload: dict
    entry_date: str | None = None
    entry_label: str | None = None
    context: list[str] = field(default_factory=list)  # fragment texts that show where the name came from
    others: int = 0  # other entries with the same unsettled name


@dataclass(frozen=True)
class LinkMention:
    entity_id: str
    apply_to_all: bool = True
    add_alias: bool = True


@dataclass(frozen=True)
class CreateEntity:
    name: str
    role: str | None = None
    org: str | None = None
    kind: str = "domain"  # themes only
    apply_to_all: bool = True


@dataclass(frozen=True)
class IgnoreMention:
    apply_to_all: bool = True


@dataclass(frozen=True)
class AcceptProposed:
    rename: str | None = None


@dataclass(frozen=True)
class MergeProposed:
    into: str


@dataclass(frozen=True)
class AnswerDecision:
    accept: bool


@dataclass(frozen=True)
class SupersedeDecision:
    accept: bool  # True: mark the older fragment overcome by events; False: keep it as it is


@dataclass(frozen=True)
class DropDecision:
    pass


@dataclass(frozen=True)
class Skip:
    pass


ReviewDecision = (
    LinkMention
    | CreateEntity
    | IgnoreMention
    | AcceptProposed
    | MergeProposed
    | AnswerDecision
    | SupersedeDecision
    | DropDecision
    | Skip
)


class ReviewError(Exception):
    pass


class Review:
    def __init__(self, vault: Vault, conn: sqlite3.Connection) -> None:
        self.vault = vault
        self.conn = conn

    def items(self) -> list[ReviewItem]:
        rows = self.conn.execute(
            "SELECT id, kind, entry_id, payload FROM review_items ORDER BY "
            "CASE kind WHEN 'new_person' THEN 0 WHEN 'new_org' THEN 1 WHEN 'ambiguous_mention' THEN 2 "
            "WHEN 'proposed_answer' THEN 3 WHEN 'dream_obe' THEN 4 ELSE 5 END, created_at, id"
        ).fetchall()
        items = [ReviewItem(r[0], r[1], r[2], json.loads(r[3])) for r in rows]
        dates = entry_dates(self.conn, sorted({i.entry_id for i in items if i.entry_id}))
        same_name: dict[tuple, int] = {}
        for i in items:
            if i.kind in MENTION_KINDS:
                k = (i.payload["type"], i.payload["surface"])
                same_name[k] = same_name.get(k, 0) + 1
        for i in items:
            if i.entry_id in dates:
                i.entry_date, i.entry_label = iso_day(dates[i.entry_id][0]), dates[i.entry_id][1]
            if i.kind in MENTION_KINDS:
                frags = fragments_by_id(self.conn, i.payload.get("fragments", []))
                i.context = [f.text for f in frags.values()]
                i.others = same_name[(i.payload["type"], i.payload["surface"])] - 1
                if i.kind != "ambiguous_mention" and not i.payload.get("candidates"):
                    # Entities created since this item was queued, such as "Priya Raman" for "Priya".
                    i.payload["candidates"] = [
                        {"entity_id": c.entity_id, "score": c.score}
                        for c in fuzzy_candidates(self.conn, i.payload["type"], i.payload["surface"])
                    ]
            elif i.kind == "proposed_answer":
                frags = fragments_by_id(self.conn, [i.payload["fragment"], i.payload["answers"]])
                i.context = [frags[x].text for x in (i.payload["answers"], i.payload["fragment"]) if x in frags]
            elif i.kind == "dream_obe":
                frags = fragments_by_id(self.conn, [i.payload["fragment"], i.payload["superseded_by"]])
                i.context = [
                    f"{iso_day(frags[x].created_at)}: {frags[x].text}"
                    for x in (i.payload["fragment"], i.payload["superseded_by"])
                    if x in frags
                ]
        return items

    def get(self, item_id: str) -> ReviewItem:
        for i in self.items():
            if i.id == item_id:
                return i
        raise ReviewError(f"no review item {item_id}")

    # --- applying decisions ----------------------------------------------------

    def resolve(self, item_id: str, decision: ReviewDecision) -> None:
        item = self.get(item_id)
        if isinstance(decision, Skip):
            return
        if item.kind in MENTION_KINDS:
            self._mention(item, decision)
        elif item.kind in ("proposed_theme", "proposed_thread"):
            self._proposed(item, decision)
        elif item.kind == "proposed_answer":
            self._answer(item, decision)
        elif item.kind == "dream_obe":
            if isinstance(decision, AnswerDecision):  # a plain yes/no from a front-end
                decision = SupersedeDecision(decision.accept)
            self._supersede(item, decision)
        elif item.kind == "orphaned_decision":
            if not isinstance(decision, DropDecision):
                raise ReviewError("an orphaned decision can only be dropped or skipped")
            d = self.vault.get_decisions(item.entry_id or "")
            d.fragments.pop(item.payload["fragment"], None)
            self.vault.put_decisions(d)
        else:
            raise ReviewError(f"unknown review item kind {item.kind}")

    def _entries_for(self, item: ReviewItem, apply_to_all: bool) -> list[str]:
        entries: list[str] = [item.entry_id] if item.entry_id else []
        if apply_to_all:
            mtype, surface = item.payload["type"], item.payload["surface"]
            for other in self.items():
                if (
                    other.kind in MENTION_KINDS
                    and other.entry_id
                    and other.entry_id not in entries
                    and (other.payload["type"], other.payload["surface"]) == (mtype, surface)
                ):
                    entries.append(other.entry_id)
        return entries

    def _map_mention(self, item: ReviewItem, entity_id: str | None, apply_to_all: bool) -> None:
        mtype, surface = item.payload["type"], item.payload["surface"]
        for entry_id in self._entries_for(item, apply_to_all):
            d = self.vault.get_decisions(entry_id)
            d.mentions.setdefault(mtype, {})[surface] = entity_id
            self.vault.put_decisions(d)

    def _add_alias(self, etype: str, entity_id: str, alias: str) -> None:
        entity = self.vault.get_entity(etype, entity_id)  # type: ignore[arg-type]
        known = {norm_alias(entity.name), *(norm_alias(a) for a in entity.aliases)}
        if norm_alias(alias) not in known:
            entity.aliases.append(alias)
            self.vault.put_entity(entity)

    def _mention(self, item: ReviewItem, decision: ReviewDecision) -> None:
        mtype, surface = item.payload["type"], item.payload["surface"]
        if isinstance(decision, LinkMention):
            try:
                self.vault.get_entity(mtype, decision.entity_id)
            except (NotFound, FormatError) as e:
                raise ReviewError(f"no {mtype} with id {decision.entity_id}") from e
            if decision.add_alias:
                self._add_alias(mtype, decision.entity_id, surface)
            self._map_mention(item, decision.entity_id, decision.apply_to_all)
        elif isinstance(decision, CreateEntity):
            aliases = [surface] if norm_alias(surface) != norm_alias(decision.name) else []

            def build(new_id: str):
                if mtype == "person":
                    return Person(
                        id=new_id,
                        name=decision.name,
                        aliases=aliases,
                        role=decision.role,
                        memberships=[Membership(org=decision.org)] if decision.org else [],
                    )
                if mtype == "org":
                    return Org(id=new_id, name=decision.name, aliases=aliases)
                return Theme(id=new_id, name=decision.name, aliases=aliases, kind=decision.kind)  # type: ignore[arg-type]

            entity = self.vault.create_entity(mtype, decision.name, build)
            self._map_mention(item, entity.id, decision.apply_to_all)
        elif isinstance(decision, IgnoreMention):
            self._map_mention(item, None, decision.apply_to_all)
        else:
            raise ReviewError("a mention can be linked, created, ignored, or skipped")

    def _proposed(self, item: ReviewItem, decision: ReviewDecision) -> None:
        etype = "theme" if item.kind == "proposed_theme" else "thread"
        eid = item.payload[etype]
        entity = self.vault.get_entity(etype, eid)  # type: ignore[arg-type]
        assert isinstance(entity, Theme | Thread)
        if isinstance(decision, AcceptProposed):
            if decision.rename and decision.rename != entity.name:
                entity.aliases.append(entity.name)
                entity.name = decision.rename
            entity.state = "active"
            self.vault.put_entity(entity)
        elif isinstance(decision, MergeProposed):
            if decision.into == eid:
                raise ReviewError("cannot merge into itself")
            self._add_alias(etype, decision.into, entity.name)  # raises if the target does not exist
            entity.state = "merged"
            entity.merged_into = decision.into
            self.vault.put_entity(entity)
        else:
            raise ReviewError(f"a proposed {etype} can be accepted, renamed, merged, or skipped")

    def _supersede(self, item: ReviewItem, decision: ReviewDecision) -> None:
        if not isinstance(decision, SupersedeDecision):
            raise ReviewError("a proposed overcome-by-events can be accepted, rejected, or skipped")
        fid, newer = item.payload["fragment"], item.payload["superseded_by"]
        frags = fragments_by_id(self.conn, [fid, newer])
        if fid not in frags:
            raise ReviewError(f"no fragment {fid}")
        d = self.vault.get_decisions(item.entry_id or "")
        fd = d.fragments.setdefault(fid, FragmentDecision())
        if decision.accept:
            later = frags.get(newer)
            reason = item.payload.get("reason") or "replaced by a later entry"
            if later is not None:
                reason = f"{reason} (superseded by {newer}, {iso_day(later.created_at)})"
            fd.status = "overcome_by_events"
            fd.obe = ObeInfo(date=self.vault.clock().date(), reason=reason)
        else:
            # Recording the current status as yours keeps it, and dreaming does not ask again.
            fd.status = frags[fid].status  # type: ignore[assignment]
        self.vault.put_decisions(d)

    def _answer(self, item: ReviewItem, decision: ReviewDecision) -> None:
        if not isinstance(decision, AnswerDecision):
            raise ReviewError("a proposed answer can be accepted, rejected, or skipped")
        fid, qid = item.payload["fragment"], item.payload["answers"]
        d = self.vault.get_decisions(item.entry_id or "")
        fd = d.fragments.setdefault(fid, FragmentDecision())
        field_name = "answers" if decision.accept else "not_answers"
        current = getattr(fd, field_name) or []
        if qid not in current:
            setattr(fd, field_name, [*current, qid])
        self.vault.put_decisions(d)
        if decision.accept:
            q = fragments_by_id(self.conn, [qid]).get(qid)
            if q is not None:
                qd = self.vault.get_decisions(q.entry_id)
                qf = qd.fragments.setdefault(qid, FragmentDecision())
                qf.state = "closed"
                qf.closed_note = qf.closed_note or f"answered by {fid}"
                self.vault.put_decisions(qd)
