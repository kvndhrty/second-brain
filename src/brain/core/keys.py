"""Every storage key pattern, in one place.

Keys are locations, not identity: each file also carries its id. The index
maps keys to "units" (an entry and its sidecars, or one entity and its
sidecars) so a change to any file rebuilds exactly the rows that depend on it.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

ENTRIES = "entries/"
DECISIONS = "decisions/"
GENERATED = "generated/"
EXTRACTIONS = "generated/extractions/"
RESOLUTIONS = "generated/resolutions/"
EMBEDDINGS = "generated/embeddings/"
THREAD_SUMMARIES = "generated/summaries/threads/"
STATE = "state/"
BRIEF_STATE = "state/briefs.json"
SYSTEM = "_system/"
FORMAT = "_system/format.json"
LEASE = "_system/lease.json"
GENERATION = "_system/generation.json"
PROBE_PREFIX = "_system/probe/"
DOCTOR_PREFIX = "_system/doctor/"

EntityType = Literal["person", "org", "theme", "thread"]
ENTITY_DIRS: dict[EntityType, str] = {
    "person": "people/",
    "org": "orgs/",
    "theme": "themes/",
    "thread": "threads/",
}

_TS_FORMAT = "%Y%m%dT%H%M%SZ"


def basic_timestamp(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime(_TS_FORMAT)


def entry_id_for(dt: datetime, suffix: int = 0) -> str:
    base = f"e-{basic_timestamp(dt)}"
    return f"{base}-{suffix}" if suffix else base


def entry_dir(dt: datetime) -> str:
    u = dt.astimezone(UTC)
    return f"{ENTRIES}{u:%Y/%m/%d}/"


def entry_key(dt: datetime, entry_id: str, slug: str) -> str:
    stem = entry_id.removeprefix("e-")
    return f"{entry_dir(dt)}{stem}-{slug}.md"


def slugify(text: str, *, max_words: int = 6, max_len: int = 48, default: str = "entry") -> str:
    norm = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    words = re.findall(r"[a-z0-9]+", norm.lower())[:max_words]
    slug = "-".join(words)[:max_len].strip("-")
    return slug or default


def entity_key(entity_type: EntityType, entity_id: str) -> str:
    return f"{ENTITY_DIRS[entity_type]}{entity_id}.md"


def decisions_key(entry_id: str) -> str:
    return f"{DECISIONS}{entry_id}.yaml"


def extraction_key(entry_id: str) -> str:
    return f"{EXTRACTIONS}{entry_id}.json"


def resolution_key(entry_id: str) -> str:
    return f"{RESOLUTIONS}{entry_id}.json"


def model_slug(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", model).strip("_") or "default"


def embedding_key(model: str, entry_id: str) -> str:
    return f"{EMBEDDINGS}{model_slug(model)}/{entry_id}.json"


def thread_summary_key(thread_id: str) -> str:
    return f"{THREAD_SUMMARIES}{thread_id}.json"


Role = Literal["entry", "decisions", "extraction", "resolution", "embedding", "entity", "thread_summary"]


@dataclass(frozen=True)
class KeyInfo:
    role: Role
    # Unit id derivable from the key alone; None for entry and entity files,
    # whose id is read from the file content.
    unit: str | None
    entity_type: EntityType | None = None
    model_slug: str | None = None


def entry_unit(entry_id: str) -> str:
    return f"entry:{entry_id}"


def entity_unit(entity_type: EntityType, entity_id: str) -> str:
    return f"{entity_type}:{entity_id}"


def _stem(key: str, prefix: str, suffix: str) -> str | None:
    rest = key[len(prefix) :]
    if "/" in rest or not rest.endswith(suffix):
        return None
    return rest[: -len(suffix)] or None


def classify(key: str) -> KeyInfo | None:
    """Return what a key is, or None for keys the index ignores."""
    if key.startswith(ENTRIES):
        return KeyInfo("entry", None) if key.endswith(".md") else None
    if key.startswith(DECISIONS):
        stem = _stem(key, DECISIONS, ".yaml")
        return KeyInfo("decisions", entry_unit(stem)) if stem else None
    if key.startswith(EXTRACTIONS):
        stem = _stem(key, EXTRACTIONS, ".json")
        return KeyInfo("extraction", entry_unit(stem)) if stem else None
    if key.startswith(RESOLUTIONS):
        stem = _stem(key, RESOLUTIONS, ".json")
        return KeyInfo("resolution", entry_unit(stem)) if stem else None
    if key.startswith(EMBEDDINGS):
        parts = key[len(EMBEDDINGS) :].split("/")
        if len(parts) == 2 and parts[1].endswith(".json") and parts[0]:
            return KeyInfo("embedding", entry_unit(parts[1][: -len(".json")]), model_slug=parts[0])
        return None
    if key.startswith(THREAD_SUMMARIES):
        stem = _stem(key, THREAD_SUMMARIES, ".json")
        return KeyInfo("thread_summary", entity_unit("thread", stem)) if stem else None
    for etype, prefix in ENTITY_DIRS.items():
        if key.startswith(prefix):
            return KeyInfo("entity", None, entity_type=etype) if _stem(key, prefix, ".md") else None
    return None
