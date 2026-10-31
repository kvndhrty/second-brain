"""Fake OpenAI-compatible endpoint for tests.

It plugs into the real client as an httpx2 transport, so tests exercise the
same client, request building, and error handling as production, with no
network and no model. Embeddings are deterministic: the same text always
gives the same unit vector. Milestone 2 adds canned structured outputs.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections.abc import Callable

import httpx2


def fake_embedding(text: str, dim: int) -> list[float]:
    seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")
    rng = random.Random(seed)
    v = [rng.gauss(0.0, 1.0) for _ in range(dim)]
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


class FakeOpenAI:
    def __init__(
        self,
        *,
        models: tuple[str, ...] = ("chat-model", "extract-model", "embed-model"),
        dim: int = 16,
        api_key: str = "test-key",
        chat_reply: str = "ok",
    ) -> None:
        self.models = models
        self.dim = dim
        self.api_key = api_key
        self.chat_reply = chat_reply
        self.requests: list[httpx2.Request] = []
        self.handlers: dict[str, Callable[[httpx2.Request], httpx2.Response]] = {}

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._handle)

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != f"Bearer {self.api_key}":
            return httpx2.Response(401, json={"error": {"message": "invalid api key", "type": "auth"}})
        path = request.url.path.rstrip("/")
        for suffix, handler in self.handlers.items():
            if path.endswith(suffix):
                return handler(request)
        if path.endswith("/models"):
            return httpx2.Response(
                200,
                json={
                    "object": "list",
                    "data": [{"id": m, "object": "model", "created": 0, "owned_by": "fake"} for m in self.models],
                },
            )
        body = json.loads(request.content or b"{}")
        model = body.get("model")
        if model is not None and model not in self.models:
            return httpx2.Response(404, json={"error": {"message": f"model {model} not found"}})
        if path.endswith("/embeddings"):
            inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
            return httpx2.Response(
                200,
                json={
                    "object": "list",
                    "model": model,
                    "data": [
                        {"object": "embedding", "index": i, "embedding": fake_embedding(t, self.dim)}
                        for i, t in enumerate(inputs)
                    ],
                    "usage": {"prompt_tokens": 0, "total_tokens": 0},
                },
            )
        if path.endswith("/chat/completions"):
            return httpx2.Response(
                200,
                json={
                    "id": "chatcmpl-fake",
                    "object": "chat.completion",
                    "created": 0,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": self.chat_reply},
                        }
                    ],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 1, "total_tokens": 1},
                },
            )
        return httpx2.Response(404, json={"error": {"message": f"no fake handler for {path}"}})
