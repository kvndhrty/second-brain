"""MCP tools over the real protocol: in memory, and `brain mcp` over stdio in a subprocess."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from brain.core import keys
from brain.core.formats.entity import Person, Thread
from brain.core.llm.models import LLMError
from brain.core.shared import SharedBrain
from brain.core.storage.lease import Lease
from brain.mcp.server import build_server
from tests.conftest import FAKE_LLM_SETTINGS, make_brain

pytestmark = pytest.mark.anyio

ENTRY = (
    "Kicked off the [[GPU quota migration]] with Priya. We decided to move the eval cluster in December. "
    "Does the vendor support MIG on the new nodes?"
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def brain(local_storage, state, tmp_path):
    b = make_brain(local_storage, state, tmp_path, fake_llm=True, llm=FAKE_LLM_SETTINGS)
    yield b
    b.close()


@pytest.fixture
def server(brain):
    return build_server(SharedBrain(lambda: brain))


async def call(client: Client, tool: str, **args):
    r = await client.call_tool(tool, args)
    if r.is_error:
        return None, r.content[0].text
    return r.structured_content, None


async def test_tools_are_listed_with_schemas_and_hints(server):
    async with Client(server) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        assert set(tools) == {"search", "ask", "brief", "get_entity", "get_entry", "list_active", "add_entry"}
        for name, t in tools.items():
            assert t.description and t.output_schema, name
            assert t.annotations.read_only_hint is (name != "add_entry"), name
        assert "cite" in (c.instructions or "").lower()


async def test_add_entry_then_read_it_back(server, brain):
    async with Client(server) as c:
        added, err = await call(c, "add_entry", text=ENTRY, title="Quota kickoff")
        assert err is None and added["processed"] and added["error"] is None
        assert [f["kind"] for f in added["fragments"]] == ["observation", "decision", "question"]
        assert added["names_to_review"] == 2  # Priya, and "MIG" (the fake treats all-caps words as orgs)
        entry, _ = await call(c, "get_entry", entry_id=added["entry_id"])
        assert entry["text"].strip() == ENTRY and entry["title"] == "Quota kickoff" and entry["processed"]

        hits, _ = await call(c, "search", query="vendor MIG nodes")
        assert hits["hits"][0]["type"] in ("fragment", "entry")
        assert any(h["entry_id"] == added["entry_id"] for h in hits["hits"])
        threads, _ = await call(c, "search", query="GPU quota", types=["thread"])
        assert [h["id"] for h in threads["hits"]] == ["thread:gpu-quota-migration"]

        answer, _ = await call(c, "ask", question="does the vendor support MIG?")
        cited = {x for claim in answer["claims"] for x in claim["citations"]}
        assert cited and cited <= {s["entry_id"] for s in answer["sources"]}

        active, _ = await call(c, "list_active")
        assert [t["id"] for t in active["threads"]] == ["gpu-quota-migration"]
        assert active["threads"][0]["summary"]

        entity, _ = await call(c, "get_entity", name_or_id="GPU quota migration")
        assert entity["id"] == "thread:gpu-quota-migration" and entity["fragment_count"] == 1


async def test_brief_marks_seen_only_when_asked(server, brain):
    async with Client(server) as c:
        await call(c, "add_entry", text=ENTRY)
        result, err = await call(c, "brief", target="GPU quota migration")
        assert err is None and result["type"] == "thread" and result["where_things_stand"]
        assert not brain.storage.exists(keys.BRIEF_STATE)
        await call(c, "brief", target="GPU quota migration", mark_seen=True)
        assert "thread:gpu-quota-migration" in json.loads(brain.storage.get(keys.BRIEF_STATE))["last"]
        again, _ = await call(c, "brief", target="gpu-quota-migration")
        assert again["last_looked"] is not None


async def test_errors_carry_useful_messages(server, brain, monkeypatch):
    brain.vault.put_entity(Person(id="jane", name="Jane Doe", aliases=["Jane"]))
    brain.vault.put_entity(Thread(id="jane", name="Jane"))
    async with Client(server) as c:
        _, err = await call(c, "get_entity", name_or_id="Jane")
        assert "person:jane" in err and "thread:jane" in err
        _, err = await call(c, "get_entity", name_or_id="nobody at all")
        assert "nothing named" in err
        _, err = await call(c, "get_entry", entry_id="e-19990101T000000Z")
        assert "no entry" in err

        def down(*a, **k):
            raise LLMError("ask: HTTP 503 from fake-chat: overloaded")

        await call(c, "add_entry", text=ENTRY)
        monkeypatch.setattr(brain.llm, "run", down)
        _, err = await call(c, "ask", question="anything about MIG?")
        assert "HTTP 503" in err


async def test_add_entry_reports_lease_held_elsewhere(server, brain, local_storage):
    local_storage.supports_conditional_writes = True
    Lease(local_storage, machine_id="laptop", ttl_seconds=900, command="brain start").acquire()
    async with Client(server) as c:
        _, err = await call(c, "add_entry", text="hello")
        assert "Another machine holds the write lease" in err


async def test_stdio_subprocess(tmp_path: Path):
    """`brain mcp` as Claude Code runs it: nothing but protocol on stdout."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[storage]\nbackend = "local"\npath = "{tmp_path / "store"}"\n'
        '[llm]\nbase_url = "https://fake/v1"\nchat_model = "fake-chat"\nembed_model = "fake-embed"\n'
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("BRAIN_")}
    env.update(
        {"BRAIN_FAKE_LLM": "1", "XDG_STATE_HOME": str(tmp_path / "state"), "XDG_CACHE_HOME": str(tmp_path / "cache")}
    )
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from brain.cli.app import main; main()", "--config", str(cfg), "mcp"],
        env=env,
    )
    async with Client(params, read_timeout_seconds=60) as c:
        assert len((await c.list_tools()).tools) == 7
        added, err = await call(c, "add_entry", text=ENTRY)
        assert err is None and added["processed"]
        answer, _ = await call(c, "ask", question="when does the eval cluster move?")
        assert answer["claims"]
