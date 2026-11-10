"""Fakes for tests: an OpenAI-compatible HTTP endpoint, and a scripted LLM.

FakeOpenAI plugs into the real client as an httpx2 transport, so tests
exercise the same client, request building, and error handling as production.

FakeLLM runs the real PydanticAI agents against a FunctionModel that answers
each prompt from a responder: by default simple rules over the prompt's input
(sentences become statements, capitalized names become people, `#tags` become
topics, `[[Name]]` names a thread), or canned outputs a test supplies.

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
from brain.core.llm.schemas import BriefOutput
from brain.core.pipeline.text import SENTENCE_END

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
        models: tuple[str, ...] = ("small-model", "strong-model", "embed-model"),
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


def _blocks(user: str, tag: str) -> list[tuple[dict[str, str], str]]:
    """(attributes, inner text) of each <tag ...>...</tag> element."""
    return [(_attrs(a), body) for a, body in re.findall(rf"<{tag}\b([^>]*)>(.*?)</{tag}>", user, re.S)]


def _note_body(user: str) -> str:
    m = re.search(r"<note[^>]*>\n(.*?)\n</note>", user, re.S)
    return m.group(1) if m else ""


def _names_of(candidate_body: str) -> list[str]:
    m = re.search(r"^names: (.*)$", candidate_body, re.M)
    return [n.strip() for n in m.group(1).split(";")] if m else []


def _plain(text: str) -> str:
    return re.sub(r"\s*#([\w-]+)", r" \1", re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)).strip()


def heuristic_split(user: str) -> dict[str, Any]:
    out = []
    for sentence in SENTENCE_END.split(_note_body(user)):
        s = sentence.strip()
        if len(s) >= 3:
            out.append({"text": _plain(s), "quote": s})
    return {"statements": out}


_CORRECTION = re.compile(
    r"\b(same person|same as|are the same|is the same|different from|not the same|should be called|"
    r"rename|is finished|is closed|is cancelled|close the|reopen|ignore what|ignore the|is wrong|not part of)\b",
    re.I,
)


def heuristic_classify(user: str) -> dict[str, Any]:
    s = _blocks(user, "statement")[0][1]
    low = s.lower()
    if _CORRECTION.search(s):
        kind = "correction"
    elif "?" in s:
        kind = "question"
    elif re.search(r"\b(decided|agreed|decision)\b", low):
        kind = "decision"
    elif re.search(r"\b(need to|needs to|todo|will|must|action)\b", low):
        kind = "action"
    elif re.search(r"\b(idea|what if|someday|could|maybe)\b", low):
        kind = "idea"
    else:
        kind = "fact"
    return {"kind": kind}


# Capitalized words that begin a sentence in test notes without being names.
_OPENERS = {"Met", "Talked", "Called", "Started", "Kicked", "Confirmed", "Reopen", "Ignore", "Later", "More", "One"}


def _starts_name(tokens: list[str]) -> bool:
    """A sentence's first word is part of a name only when another capitalized word follows it."""
    if tokens[0].rstrip(".") in _OPENERS or len(tokens) < 2:
        return False
    return all(re.fullmatch(r"[A-Z][a-z]+\.?", t) and t.rstrip(".") not in _STOP for t in tokens[:2])


def heuristic_names(user: str) -> dict[str, Any]:
    """Capitalized words are people (orgs when they end in Team, Group, or Inc); capital-letter words and #tags are topics."""
    body = _note_body(user)
    found: dict[str, str] = {}
    for sentence in SENTENCE_END.split(body):
        for tag in re.findall(r"#([\w-]+)", sentence):
            found.setdefault(tag, "topic")
        tokens = re.findall(r"[A-Za-z][\w'-]*\.?", re.sub(r"\[\[[^\]]+\]\]|#[\w-]+", " ", sentence))
        i = 0
        while i < len(tokens):
            tok = tokens[i].rstrip(".") if len(tokens[i]) > 2 else tokens[i]
            bare = tok.rstrip(".")
            if re.fullmatch(r"[A-Z]{2,6}", bare) and bare not in _STOP:
                found.setdefault(bare, "topic")
            elif bare[0].isupper() and bare not in _STOP and (i > 0 or _starts_name(tokens)):
                name = tok
                while i + 1 < len(tokens) and tokens[i + 1][0].isupper() and tokens[i + 1].rstrip(".") not in _STOP:
                    name += " " + tokens[i + 1]
                    i += 1
                name = name.rstrip(".")
                if name in body:
                    found.setdefault(name, "org" if name.split()[-1] in ("Team", "Group", "Inc") else "person")
            i += 1
    return {"names": [{"name": n, "type": t} for n, t in found.items()]}


def heuristic_link(user: str) -> dict[str, Any]:
    """A candidate one of whose names contains every word of the name (so "Jane" matches "Jane Doe")."""
    name = _blocks(user, "name")[0][1]
    want = _words(name) or {name.lower()}
    for a, body in _blocks(user, "candidate"):
        if any(want <= (_words(n) or {n.lower()}) for n in _names_of(body)):
            return {"choice": a["id"]}
    return {"choice": "new"}


def heuristic_file(user: str) -> dict[str, Any]:
    """`[[Name]]` in the note names the thread; otherwise a candidate whose name appears in the statement."""
    statement = _words(_blocks(user, "statement")[0][1])
    cands = [(a["id"], _names_of(body)) for a, body in _blocks(user, "candidate")]

    def known(name: str) -> str | None:
        return next((cid for cid, names in cands if any(n.lower() == name.lower() for n in names)), None)

    marked = re.findall(r"\[\[([^\]]+)\]\]", _note_body(user))
    for name in marked:
        if _words(name) <= statement:
            return {"choice": known(name) or "new", "new_name": None if known(name) else name}
    for cid, names in cands:
        if any(_words(n) and _words(n) <= statement for n in names):
            return {"choice": cid}
    if marked:
        return {"choice": known(marked[0]) or "new", "new_name": None if known(marked[0]) else marked[0]}
    return {"choice": "none"}


_CHANGE = re.compile(r"\b(now|instead|moved|changed|no longer|actually|cancel+ed|postponed|pushed)\b", re.I)


def _overlap(a: str, b: str) -> float:
    x, y = _words(a), _words(b)
    return len(x & y) / len(x | y) if x and y else 0.0


def heuristic_relate(user: str) -> dict[str, Any]:
    (ea, earlier), (_, later) = _blocks(user, "earlier")[0], _blocks(user, "later")[0]
    overlap = _overlap(earlier, later)
    if overlap >= 0.9:
        return {"relation": "repeats"}
    if overlap >= 0.3 and ea.get("kind") in ("question", "action"):
        return {"relation": "answers"}
    if overlap >= 0.3 and _CHANGE.search(later) and (ea.get("kind") == "decision" or overlap >= 0.5):
        return {"relation": "supersedes"}
    return {"relation": "unrelated"}


def heuristic_correction_op(user: str) -> dict[str, Any]:
    s = _blocks(user, "instruction")[0][1].lower()
    rules = [
        (r"not part of", "not_part_of"),
        (r"different|not the same", "not_same_as"),
        (r"same person|same as|the same", "same_as"),
        (r"should be called|rename", "rename"),
        (r"reopen|active again", "reopen"),
        (r"finished|closed|cancelled|close the", "close"),
        (r"ignore|wrong|retract|disregard", "retract"),
    ]
    return {"op": next((op for pattern, op in rules if re.search(pattern, s)), "none")}


def heuristic_correction_targets(user: str) -> dict[str, Any]:
    op = _blocks(user, "operation")[0][1]
    instruction = _blocks(user, "instruction")[0][1]
    low = instruction.lower()
    cands = _blocks(user, "candidate")
    if op == "retract":
        hits = [a["id"] for a, body in cands if _overlap(body, instruction) >= 0.25]
        return {"targets": hits or [a["id"] for a, _ in cands[:1]]}
    # Candidates in the order their names appear in the instruction.
    placed = []
    for a, body in cands:
        at = [(low.find(n.lower()), -len(n)) for n in _names_of(body) if n.lower() in low]
        if at:
            placed.append((*min(at), a["id"]))
    named = [cid for *_, cid in sorted(placed)]
    page = re.search(r'<written_from_page id="([^"]+)"', user)
    if not named and page:
        named = [page.group(1)]  # "this is finished", written from that page
    if not named and op in ("close", "reopen") and cands:
        named = [cands[0][0]["id"]]  # the likeliest piece of work comes first
    if op in ("same_as", "not_same_as", "not_part_of"):
        return {"targets": named[:2]}
    new_name = None
    if op == "rename":
        m = re.search(r"(?:should be called|renamed? to|rename it to)\s+(.+?)[.!]?$", instruction, re.I)
        new_name = m.group(1).strip("\"' ") if m else None
    return {"targets": named[:1], "new_name": new_name}


def heuristic_now(user: str) -> dict[str, Any]:
    stmts = _blocks(user, "statement")
    if not stmts:
        return {"text": "No recent activity.", "citations": []}
    a, text = stmts[-1]
    return {"text": text[:200], "citations": [a["note"]]}


def heuristic_summary(user: str) -> dict[str, Any]:
    return {"sentences": [{"text": text, "citations": [a["note"]]} for a, text in _blocks(user, "statement")[-5:]]}


def heuristic_check(user: str) -> dict[str, Any]:
    return {"supported": "UNSUPPORTED" not in user}


def heuristic_same(user: str) -> dict[str, Any]:
    """Two records are the same when every word of one's name is in the other's (Jane, Jane Doe)."""
    a, b = [[_words(n) for n in _names_of(body)] for _, body in _blocks(user, "record")[:2]]
    return {"same": any(x and y and (x <= y or y <= x) for x in a for y in b)}


def heuristic_stale(user: str) -> dict[str, Any]:
    item = _blocks(user, "item")[0][1]
    best = max(
        ((a["id"], _overlap(item, text)) for a, text in _blocks(user, "later")), key=lambda x: x[1], default=None
    )
    if best and best[1] >= 0.3:
        return {"verdict": "answered", "by": best[0]}
    return {"verdict": "open", "by": None}


def heuristic_profile(user: str) -> dict[str, Any]:
    stmts = " ".join(text for _, text in _blocks(user, "statement"))
    m = re.search(r"\bis (?:a|an|the|our) ([a-z][a-z -]{2,40}?)(?: (?:on|at|in|for)\b|[.,]|$)", stmts)
    org = next((a["id"] for a, body in _blocks(user, "candidate") if any(n in stmts for n in _names_of(body))), None)
    return {"role": m.group(1) if m else None, "org": org}


def heuristic_brief(user: str) -> dict[str, Any]:
    out: dict[str, list] = {k: [] for k in BriefOutput.model_fields}
    stmts = _blocks(user, "statement")
    for a, text in stmts:
        claim = {"text": text, "citations": [a["note"]]}
        if a.get("status") == "superseded":
            out["no_longer_applies"].append(claim)
            continue
        if a.get("kind") == "decision":
            out["recent_decisions"].append(claim)
        if a.get("kind") in ("question", "action") and a.get("status") is None:
            out["open_items"].append(claim)
        if a.get("new") == "yes":
            out["changed_since"].append(claim)
    out["where_things_stand"] = [{"text": t, "citations": [a["note"]]} for a, t in stmts[-2:]]
    return out


def heuristic_brief_route(user: str) -> dict[str, Any]:
    """Candidates whose whole name appears in the request, threads first; a question when one
    name belongs to things of different types."""
    request = re.search(r"<request>(.*?)</request>", user, re.S)
    words = _words(request.group(1) if request else "")
    named = []
    for a, body in _blocks(user, "candidate"):
        for name in _names_of(body):
            if _words(name) and _words(name) <= words:
                named.append((a["type"] != "thread", a["type"], name.lower(), a["id"]))
                break
    named.sort()
    names = [n for _, _, n, _ in named]
    if len(set(names)) < len(names) and len({t for _, t, _, _ in named}) > 1:
        return {"targets": [], "clarify": f"Which {names[0]} do you mean?"}
    return {"targets": [i for *_, i in named][:4], "clarify": None}


def heuristic_ask(user: str) -> dict[str, Any]:
    stmts = [(a, t) for a, t in _blocks(user, "statement") if a.get("status") is None]
    if not stmts:
        return {"claims": [], "insufficient": True}
    return {
        "claims": [{"text": text.strip()[:200], "citations": [a["note"]]} for a, text in stmts[:2]],
        "insufficient": False,
    }


DEFAULT_RESPONDERS: dict[str, Responder] = {
    "split": heuristic_split,
    "classify": heuristic_classify,
    "names": heuristic_names,
    "link": heuristic_link,
    "file": heuristic_file,
    "relate": heuristic_relate,
    "correction_op": heuristic_correction_op,
    "correction_targets": heuristic_correction_targets,
    "now": heuristic_now,
    "summary": heuristic_summary,
    "check": heuristic_check,
    "dream_same": heuristic_same,
    "dream_stale": heuristic_stale,
    "dream_profile": heuristic_profile,
    "brief": heuristic_brief,
    "brief_route": heuristic_brief_route,
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
