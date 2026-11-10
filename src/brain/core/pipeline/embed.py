"""Vectors for the note and its statements. Unchanged texts keep their vectors."""

from __future__ import annotations

from brain.core.formats.note import Note, content_hash
from brain.core.formats.records import EmbeddingFile, EmbeddingItem, Statement, encode_vector
from brain.core.llm.models import LLMError
from brain.core.pipeline.context import PipelineContext


def needed_texts(note: Note, statements: list[Statement]) -> dict[str, str]:
    return {note.id: note.body, **{s.id: s.text for s in statements}}


def is_current(previous: EmbeddingFile | None, texts: dict[str, str]) -> bool:
    have = {i.id: i.hash for i in previous.items} if previous else {}
    return previous is not None and all(have.get(i) == content_hash(t) for i, t in texts.items())


def embed(ctx: PipelineContext, note: Note, texts: dict[str, str], previous: EmbeddingFile | None) -> EmbeddingFile:
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
    return EmbeddingFile(note_id=note.id, model=ctx.llm.embed_model, dim=dim, items=[items[i] for i in texts])
