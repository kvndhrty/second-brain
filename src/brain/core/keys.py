"""Every storage key pattern, in one place.

Keys are locations, not identity: each file also carries its id. The index
maps keys to "units" (a note and its generated files, one entity and its page,
or one dream run) so a change to any file rebuilds exactly the rows that
depend on it.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

NOTES = "notes/"
WIKI = "wiki/"
WIKI_INDEX = "wiki/index.md"
WIKI_LOG = "wiki/log.md"
GENERATED = "generated/"
LAYERS_DIR = "generated/notes/"
ENTITIES = "generated/entities/"
PAGES = "generated/pages/"
EMBEDDINGS = "generated/embeddings/"
DREAM_RUNS = "generated/dreams/runs/"
DREAM_STATE = "generated/dreams/state.json"
STATE = "state/"
BRIEF_STATE = "state/briefs.json"
SYSTEM = "_system/"
FORMAT = "_system/format.json"
LEASE = "_system/lease.json"
GENERATION = "_system/generation.json"
PROBE_PREFIX = "_system/probe/"
DOCTOR_PREFIX = "_system/doctor/"

LAYERS = ("split", "classify", "names", "links", "filing", "state")
WIKI_DIRS = {"thread": "threads", "person": "people", "org": "orgs", "topic": "topics"}

_TS_FORMAT = "%Y%m%dT%H%M%SZ"


def basic_timestamp(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime(_TS_FORMAT)


def note_id_for(dt: datetime, suffix: int = 0) -> str:
    base = f"n-{basic_timestamp(dt)}"
    return f"{base}-{suffix}" if suffix else base


def statement_id(note_id: str, n: int) -> str:
    return f"s-{note_id.removeprefix('n-')}-{n:02d}"


def note_of_statement(statement: str) -> str:
    return "n-" + statement.removeprefix("s-").rsplit("-", 1)[0]


def note_dir(dt: datetime) -> str:
    u = dt.astimezone(UTC)
    return f"{NOTES}{u:%Y/%m/%d}/"


def note_key(dt: datetime, note_id: str, slug: str) -> str:
    return f"{note_dir(dt)}{note_id.removeprefix('n-')}-{slug}.md"


def slugify(text: str, *, max_words: int = 6, max_len: int = 48, default: str = "note") -> str:
    norm = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    words = re.findall(r"[a-z0-9]+", norm.lower())[:max_words]
    slug = "-".join(words)[:max_len].strip("-")
    return slug or default


def layer_key(note_id: str, layer: str) -> str:
    return f"{LAYERS_DIR}{note_id}/{layer}.json"


def entity_key(entity_id: str) -> str:
    return f"{ENTITIES}{entity_id}.json"


def page_key(entity_id: str) -> str:
    return f"{PAGES}{entity_id}.json"


def wiki_key(entity_type: str, entity_id: str) -> str:
    return f"{WIKI}{WIKI_DIRS[entity_type]}/{entity_id}.md"


def model_slug(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", model).strip("_") or "default"


def embedding_key(model: str, note_id: str) -> str:
    return f"{EMBEDDINGS}{model_slug(model)}/{note_id}.json"


def dream_run_key(run_id: str) -> str:
    return f"{DREAM_RUNS}{run_id}.json"


Role = Literal["note", "layer", "embedding", "entity", "page", "dream_run"]


@dataclass(frozen=True)
class KeyInfo:
    role: Role
    # Unit id derivable from the key alone; None for note files, whose id is
    # read from the file content.
    unit: str | None
    layer: str | None = None
    model_slug: str | None = None


def note_unit(note_id: str) -> str:
    return f"note:{note_id}"


def entity_unit(entity_id: str) -> str:
    return f"entity:{entity_id}"


def dream_unit(run_id: str) -> str:
    return f"dream:{run_id}"


def _stem(key: str, prefix: str, suffix: str) -> str | None:
    rest = key[len(prefix) :]
    if "/" in rest or not rest.endswith(suffix):
        return None
    return rest[: -len(suffix)] or None


_NOTE_KEY = re.compile(r"notes/\d{4}/\d{2}/\d{2}/\d{8}T\d{6}Z(-\d+)?-[^/]+\.md")


def is_bare_note(key: str) -> bool:
    """A markdown file saved into notes/ by hand: not at the path brain gives its own notes."""
    return key.startswith(NOTES) and key.endswith(".md") and not _NOTE_KEY.fullmatch(key)


def classify(key: str) -> KeyInfo | None:
    """Return what a key is, or None for keys the index ignores (the wiki, state, system files)."""
    if key.startswith(NOTES):
        return KeyInfo("note", None) if key.endswith(".md") else None
    if key.startswith(LAYERS_DIR):
        parts = key[len(LAYERS_DIR) :].split("/")
        if len(parts) == 2 and parts[0] and parts[1].endswith(".json") and parts[1][:-5] in LAYERS:
            return KeyInfo("layer", note_unit(parts[0]), layer=parts[1][:-5])
        return None
    if key.startswith(EMBEDDINGS):
        parts = key[len(EMBEDDINGS) :].split("/")
        if len(parts) == 2 and parts[1].endswith(".json") and parts[0]:
            return KeyInfo("embedding", note_unit(parts[1][: -len(".json")]), model_slug=parts[0])
        return None
    for prefix, role, unit in ((ENTITIES, "entity", entity_unit), (PAGES, "page", entity_unit)):
        if key.startswith(prefix):
            stem = _stem(key, prefix, ".json")
            return KeyInfo(role, unit(stem)) if stem else None  # type: ignore[arg-type]
    if key.startswith(DREAM_RUNS):
        stem = _stem(key, DREAM_RUNS, ".json")
        return KeyInfo("dream_run", dream_unit(stem)) if stem else None
    return None
