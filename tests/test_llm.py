"""The real PydanticAI + OpenAI client path, against the fake gateway over an httpx2 transport."""

from __future__ import annotations

import json
from importlib import resources

import pydantic_ai
import pytest

from brain.core.config import LoadedSettings, Settings
from brain.core.doctor import Doctor
from brain.core.llm.fake import FakeOpenAI
from brain.core.llm.models import EMBED_BATCH, LLM, LLMError
from brain.core.llm.prompts import load_prompt
from brain.core.llm.schemas import Probe

LLM_CFG = {
    "base_url": "https://gateway.test/v1",
    "chat_model": "chat-model",
    "extract_model": "extract-model",
    "embed_model": "embed-model",
}


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("BRAIN_API_KEY", "test-key")


def make(fake: FakeOpenAI, **llm) -> LLM:
    return LLM(Settings(llm=LLM_CFG | llm), transport=fake.transport())


def sent_bodies(fake: FakeOpenAI) -> list[dict]:
    return [json.loads(r.content) for r in fake.requests if r.url.path.endswith("/chat/completions")]


def test_tool_mode_through_openai_client():
    fake = FakeOpenAI()
    out = make(fake, structured_output="tool").run("probe", "go", Probe)
    assert out.output == Probe(ok=True, word="ok")
    assert out.model == "extract-model" and out.prompt.name == "probe"
    body = sent_bodies(fake)[0]
    assert body["model"] == "extract-model" and body["tools"]


def test_auto_falls_back_to_prompted_and_remembers():
    fake = FakeOpenAI(supports_tools=False)
    llm = make(fake)
    assert llm.run("probe", "go", Probe).output.ok
    n = len(sent_bodies(fake))
    assert llm.run("probe", "again", Probe).output.ok
    later = sent_bodies(fake)[n:]
    assert later and not any(b.get("tools") for b in later)  # went straight to prompted


def test_native_mode_rejected_is_a_clear_error():
    fake = FakeOpenAI(supports_json_schema=False)
    with pytest.raises(LLMError, match="HTTP 400"):
        make(fake, structured_output="native").run("probe", "go", Probe)


def test_missing_key_and_model(monkeypatch):
    monkeypatch.delenv("BRAIN_API_KEY")
    with pytest.raises(LLMError, match="BRAIN_API_KEY"):
        make(FakeOpenAI()).run("probe", "go", Probe)
    monkeypatch.setenv("BRAIN_API_KEY", "test-key")
    with pytest.raises(LLMError, match="extract_model"):
        LLM(Settings(llm={"base_url": "https://g/v1"}), transport=FakeOpenAI().transport()).run("probe", "go", Probe)


def test_embeddings_are_batched():
    fake = FakeOpenAI(dim=8)
    vectors = make(fake).embed([f"text {i}" for i in range(EMBED_BATCH + 6)])
    assert len(vectors) == EMBED_BATCH + 6 and len(vectors[0]) == 8
    assert sum(r.url.path.endswith("/embeddings") for r in fake.requests) == 2


def test_every_prompt_file_loads_with_a_stamp():
    names = [p.name.removesuffix(".md") for p in resources.files("brain.prompts").iterdir() if p.name.endswith(".md")]
    assert set(names) >= {"extract", "resolve", "answers", "thread_summary", "brief", "ask", "probe"}
    for name in names:
        p = load_prompt(name)
        assert p.version >= 1 and len(p.sha256) == 16 and p.role in ("chat", "extract") and p.instructions


def test_pydantic_ai_banner_is_disabled():
    assert pydantic_ai.BANNER_ENABLED is False


@pytest.mark.parametrize(
    "fake_kw, configured, status, fix",
    [
        ({}, "auto", "ok", None),
        ({"supports_tools": False}, "auto", "ok", None),
        ({"supports_tools": False}, "tool", "fail", "Set llm.structured_output = 'native'."),
    ],
)
def test_doctor_structured_output(local_storage, state, fake_kw, configured, status, fix):
    settings = Settings(llm=LLM_CFG | {"structured_output": configured})
    results = Doctor(
        storage=local_storage,
        state=state,
        llm_transport=FakeOpenAI(**fake_kw).transport(),
        loaded=LoadedSettings(settings=settings, path=None, env_overrides=[]),
    ).run()
    (r,) = [r for r in results if r.name == "structured output"]
    assert r.status == status and r.fix == fix
