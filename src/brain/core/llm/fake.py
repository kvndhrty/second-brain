"""Fakes for tests: an OpenAI-compatible HTTP endpoint, and a scripted LLM.

FakeOpenAI plugs into the real client as an httpx2 transport, so tests
exercise the same client, request building, and error handling as production.

FakeLLM runs the real PydanticAI agents against a FunctionModel that answers
each prompt from a responder: by default simple rules over the prompt's input
(sentences become fragments, capitalized names become people, `#tags` become
themes, `[[Name]]` assigns a thread), or canned outputs a test supplies.

Embeddings are deterministic bag-of-words vectors (feature hashing), so equal
texts have similarity 1 and texts sharing words are close.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable
from typing import Any

import httpx2
from pydantic_ai import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from brain.core.config import Settings
from brain.core.llm.models import LLM
from brain.core.llm.prompts import Prompt, load_prompt

_WORD = re.compile(r"[a-z0-9]{3,}")


def fake_embedding(text: str, dim: int) -> list[float]:
    v = [0.0] * dim
    for word in _WORD.findall(text.lower()):
        h = int.from_bytes(hashlib.sha256(word.encode()).digest()[:8], "big")
        v[h % dim] += 1.0 if (h >> 32) & 1 else -1.0
    if not any(v):
        v[int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], "big") % dim] = 1.0
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


class FakeOpenAI:
    def __init__(
        self,
        *,
        models: tuple[str, ...] = ("chat-model", "extract-model", "embed-model"),
        dim: int = 16,
        api_key: str = "test-key",
        chat_reply: str = "ok",
        structured_reply: dict | None = None,
        supports_tools: bool = True,
        supports_json_schema: bool = True,
    ) -> None:
        self.models = models
        self.dim = dim
        self.api_key = api_key
        self.chat_reply = chat_reply
        self.structured_reply = structured_reply or {"ok": True, "word": "ok"}
        self.supports_tools = supports_tools
        self.supports_json_schema = supports_json_schema
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
            return self._chat(body, model)
        return httpx2.Response(404, json={"error": {"message": f"no fake handler for {path}"}})

    def _chat(self, body: dict, model: str | None) -> httpx2.Response:
        def reply(message: dict, finish: str = "stop") -> httpx2.Response:
            return httpx2.Response(
                200,
                json={
                    "id": "chatcmpl-fake",
                    "object": "chat.completion",
                    "created": 0,
                    "model": model,
                    "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", **message}}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 1, "total_tokens": 1},
                },
            )

        if body.get("tools"):
            if not self.supports_tools:
                return httpx2.Response(400, json={"error": {"message": "tools are not supported by this model"}})
            name = body["tools"][0]["function"]["name"]
            call = {
                "id": "call_1",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(self.structured_reply)},
            }
            return reply({"content": None, "tool_calls": [call]}, "tool_calls")
        if body.get("response_format"):
            if not self.supports_json_schema and body["response_format"].get("type") == "json_schema":
                return httpx2.Response(400, json={"error": {"message": "response_format is not supported"}})
            return reply({"content": json.dumps(self.structured_reply)})
        text = json.dumps(body.get("messages", []))
        if "schema" in text.lower() or "json" in text.lower():
            return reply({"content": json.dumps(self.structured_reply)})
        return reply({"content": self.chat_reply})


# --- scripted LLM ---------------------------------------------------------------

Responder = Callable[[str], dict[str, Any]] | dict[str, Any]

_STOP = {
    "I",
    "The",
    "We",
    "A",
    "An",
    "It",
    "This",
    "That",
    "They",
    "He",
    "She",
    "Our",
    "My",
    "If",
    "And",
    "But",
    "So",
    "Then",
    "Open",
    "Someday",
    "Maybe",
    "Today",
    "Yesterday",
    "Tomorrow",
    "Also",
    "Next",
    "TODO",
    "Idea",
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
}


def _attrs(tag: str) -> dict[str, str]:
    return dict(re.findall(r'(\w+)="([^"]*)"', tag))


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def heuristic_extract(user: str) -> dict[str, Any]:
    body = re.search(r"<entry[^>]*>\n(.*?)\n</entry>", user, re.S)
    text = body.group(1) if body else ""
    threads = re.findall(r"^- id: (\S+) \| name: ([^|\n]+)", user, re.M)
    fragments = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        s = sentence.strip()
        if len(s) < 3:
            continue
        low = s.lower()
        if "?" in s:
            kind = "question"
        elif re.search(r"\b(decided|agreed|decision)\b", low):
            kind = "decision"
        elif re.search(r"\b(need to|todo|will|must|action)\b", low):
            kind = "action_item"
        elif re.search(r"\b(idea|what if|someday|could)\b", low):
            kind = "idea"
        elif re.search(r"\b(claims|says|said)\b", low):
            kind = "claim"
        else:
            kind = "observation"
        status = "speculative" if re.search(r"\b(maybe|might|probably|i think|rumou?r|perhaps)\b", low) else "confirmed"
        clean = re.sub(r"\[\[([^\]]+)\]\]", r"\1", s)
        tokens = re.findall(r"[A-Za-z][\w'-]*", re.sub(r"\[\[[^\]]+\]\]|#[\w-]+", " ", s))
        people, orgs = [], []
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if re.fullmatch(r"[A-Z]{2,6}", tok) and tok not in _STOP:
                orgs.append(tok)
            elif tok[0].isupper() and tok not in _STOP and i > 0:
                name = tok
                if i + 1 < len(tokens) and tokens[i + 1][0].isupper() and tokens[i + 1] not in _STOP:
                    name += " " + tokens[i + 1]
                    i += 1
                people.append(name)
            i += 1
        themes = [
            {
                "name": t.removeprefix("tech-").replace("-", " "),
                "kind": "technique" if t.startswith("tech-") else "domain",
            }
            for t in re.findall(r"#([\w-]+)", s)
        ]
        refs = []
        for name in re.findall(r"\[\[([^\]]+)\]\]", s):
            match = next((tid for tid, tname in threads if tname.strip().lower() == name.lower()), None)
            refs.append(
                {"thread_id": match, "weight": 0.9}
                if match
                else {"new_name": name, "new_description": f"Work on {name}", "weight": 0.9}
            )
        if not refs:
            for tid, tname in threads:
                if _words(tname) and _words(tname) <= _words(s):
                    refs.append({"thread_id": tid, "weight": 0.7})
        fragments.append(
            {
                "kind": kind,
                "text": re.sub(r"\s*#[\w-]+", "", clean).strip(),
                "quote": s,
                "status": status,
                "someday": "someday" in low,
                "mentions": [{"name": p, "type": "person"} for p in people]
                + [{"name": o, "type": "org"} for o in orgs],
                "themes": themes,
                "threads": refs,
            }
        )
    return {"fragments": fragments}


def heuristic_resolve(user: str) -> dict[str, Any]:
    choices = []
    for block in re.findall(r"<mention [^>]*>.*?</mention>", user, re.S):
        key = _attrs(block.split(">", 1)[0])["key"]
        cands = [_attrs(c) for c in re.findall(r"<candidate [^>]*>", block)]
        if cands and float(cands[0]["score"]) >= 0.85:
            choices.append({"key": key, "choice": cands[0]["id"], "confidence": 0.9, "reason": "close match"})
        else:
            choices.append({"key": key, "choice": "unsure", "confidence": 0.3, "reason": "not enough context"})
    return {"choices": choices}


def heuristic_answers(user: str) -> dict[str, Any]:
    new = re.findall(r'<fragment id="([^"]+)"[^>]*>(.*?)</fragment>', user)
    items = re.findall(r'<item id="([^"]+)"[^>]*>(.*?)</item>', user)
    links = []
    for fid, ftext in new:
        for iid, itext in items:
            a, b = _words(ftext), _words(itext)
            if a and b and len(a & b) / len(a | b) >= 0.3:
                links.append({"fragment": fid, "answers": iid, "confidence": 0.8})
    return {"links": links}


def heuristic_summary(user: str) -> dict[str, Any]:
    prev = re.search(r"<previous_summary[^>]*>\n(.*?)\n</previous_summary>", user, re.S)
    frags = re.findall(r'<fragment entry="([^"]+)"[^>]*>(.*?)</fragment>', user)
    lines = [prev.group(1)] if prev else []
    lines += [f"- {text} [{entry}]" for entry, text in frags]
    return {"summary_md": "\n".join(lines), "citations": sorted({e for e, _ in frags})}


def heuristic_brief(user: str) -> dict[str, Any]:
    out: dict[str, list] = {
        k: []
        for k in (
            "where_things_stand",
            "recent_decisions",
            "open_items",
            "people",
            "speculative",
            "overcome_by_events",
            "changed_since",
        )
    }
    frags = [(_attrs(tag), text) for tag, text in re.findall(r"<fragment ([^>]*)>(.*?)</fragment>", user)]
    for a, text in frags:
        claim = {"text": text, "citations": [a["entry"]]}
        if a.get("status") == "overcome_by_events":
            out["overcome_by_events"].append(claim)
            continue
        if a.get("kind") == "decision":
            out["recent_decisions"].append(claim)
        if a.get("state") == "open":
            out["open_items"].append(claim)
        if a.get("status") == "speculative":
            out["speculative"].append(claim)
        if a.get("new") == "yes":
            out["changed_since"].append(claim)
    out["where_things_stand"] = [{"text": t, "citations": [a["entry"]]} for a, t in frags[-2:]]
    return out


def heuristic_ask(user: str) -> dict[str, Any]:
    excerpts = re.findall(r'<excerpt entry="([^"]+)"[^>]*>(.*?)</excerpt>', user, re.S)
    if not excerpts:
        return {"claims": [], "insufficient": True}
    return {
        "claims": [{"text": text.strip()[:200], "citations": [entry]} for entry, text in excerpts[:2]],
        "insufficient": False,
    }


DEFAULT_RESPONDERS: dict[str, Responder] = {
    "extract": heuristic_extract,
    "resolve": heuristic_resolve,
    "answers": heuristic_answers,
    "thread_summary": heuristic_summary,
    "brief": heuristic_brief,
    "ask": heuristic_ask,
}


def _user_text(messages: list) -> str:
    for m in messages:
        if isinstance(m, ModelRequest):
            for part in m.parts:
                if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                    return part.content
    return ""


class FakeLLM(LLM):
    """Scripted LLM for tests and offline demos. No network."""

    def __init__(self, settings: Settings, *, responders: dict[str, Responder] | None = None, dim: int = 64) -> None:
        super().__init__(
            settings, model_factory=self._fake_model, embedder=lambda ts: [fake_embedding(t, dim) for t in ts]
        )
        self.responders = {**DEFAULT_RESPONDERS, **(responders or {})}
        self.calls: list[tuple[str, str]] = []
        self.embed_calls: list[list[str]] = []
        self._dim = dim

    @property
    def configured(self) -> bool:
        return True

    def model_name(self, role: str) -> str:  # type: ignore[override]
        return super().model_name(role) or f"fake-{role}"  # type: ignore[arg-type]

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.embed_calls.append(list(texts))
        return super().embed(texts)

    def _fake_model(self, prompt: Prompt, name: str) -> FunctionModel:
        # One model serves every prompt with the same role, so identify the
        # prompt from the instructions the agent actually sent.
        by_instructions = {load_prompt(n).instructions: n for n in DEFAULT_RESPONDERS}

        def respond(messages: list, info: AgentInfo) -> ModelResponse:
            user = _user_text(messages)
            sent = info.instructions or ""
            prompt_name = next((n for text, n in by_instructions.items() if text in sent), prompt.name)
            self.calls.append((prompt_name, user))
            responder = self.responders.get(prompt_name, {})
            data = responder(user) if callable(responder) else responder
            if info.output_tools:
                return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, json.dumps(data))])
            return ModelResponse(parts=[TextPart(json.dumps(data))])

        return FunctionModel(respond, model_name=name)

    def prompts_called(self) -> list[str]:
        return [name for name, _ in self.calls]
