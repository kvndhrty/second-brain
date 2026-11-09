"""Step 2: vectors for the entry and its fragments. Unchanged texts keep their vectors."""

from __future__ import annotations

from brain.core.formats.decisions import Decisions
from brain.core.formats.entry import Entry, content_hash
from brain.core.formats.sidecars import EmbeddingFile, EmbeddingItem, Extraction, encode_vector
from brain.core.llm.models import LLMError
from brain.core.pipeline.context import PipelineContext


def needed_texts(entry: Entry, extraction: Extraction, decisions: Decisions) -> dict[str, str]:
    texts = {entry.id: entry.body}
    for f in extraction.fragments:
        if not decisions.is_deleted(f.id):
            texts[f.id] = f.text
    return texts


def embed(
    ctx: PipelineContext, entry: Entry, extraction: Extraction, decisions: Decisions, previous: EmbeddingFile | None
) -> EmbeddingFile:
    texts = needed_texts(entry, extraction, decisions)
    old = {}
    if previous is not None and previous.model == ctx.llm.embed_model:
        old = {i.id: i for i in previous.items}
    items: dict[str, EmbeddingItem] = {}
    todo: list[tuple[str, str]] = []
    for item_id, text in texts.items():
        h = content_hash(text)
        if item_id in old and old[item_id].hash == h:
            items[item_id] = old[item_id]
        else:
            todo.append((item_id, text))
    dim = previous.dim if previous is not None and old else 0
    if todo:
        vectors = ctx.llm.embed([t for _, t in todo])
        dim = len(vectors[0])
        expected = ctx.settings.llm.embed_dim
        if expected and dim != expected:
            raise LLMError(f"{ctx.llm.embed_model} returned {dim} dimensions but llm.embed_dim = {expected}")
        if any(len(v) != dim for v in vectors) or (old and previous and previous.dim != dim):
            raise LLMError(f"{ctx.llm.embed_model} returned vectors of inconsistent dimensions")
        for (item_id, text), vec in zip(todo, vectors, strict=True):
            items[item_id] = EmbeddingItem(id=item_id, hash=content_hash(text), vec=encode_vector(vec))
    return EmbeddingFile(
        entry_id=entry.id,
        model=ctx.llm.embed_model,
        dim=dim,
        items=[items[i] for i in texts],
    )
