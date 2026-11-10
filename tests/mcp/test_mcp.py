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
from brain.core.llm.models import LLMError
from brain.core.shared import SharedBrain
from brain.core.storage.lease import Lease
from brain.mcp.server import build_server

pytestmark = pytest.mark.anyio

NOTE = (
    "Kicked off the [[GPU quota migration]] with Priya Raman. We decided to move the eval cluster in December. "
    "Does the vendor support MIG on the new nodes?"
)
FOLLOW_UP = "Priya Raman needs to file the ticket for the [[GPU quota migration]]."
TOOLS = {"search", "ask", "brief", "get_page", "get_note", "list_active", "jot", "get_context"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


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
        assert set(tools) == TOOLS
        for name, t in tools.items():
            assert t.description and t.output_schema, name
            assert t.annotations.read_only_hint is (name != "jot"), name
        instructions = (c.instructions or "").lower()
        assert "cite" in instructions and "only way to change the" in instructions


async def test_jot_then_read_it_back(server, brain):
    async with Client(server) as c:
        added, err = await call(c, "jot", text=NOTE, title="Quota kickoff")
        assert err is None and added["processed"] and added["error"] is None
        assert [s["kind"] for s in added["statements"]] == ["fact", "decision", "question"]
        assert any("New person: Priya Raman" in line for line in added["changes"])
        await call(c, "jot", text=FOLLOW_UP)
        note, _ = await call(c, "get_note", note_id=added["note_id"])
        assert note["text"].strip() == NOTE and note["title"] == "Quota kickoff" and note["processed"]
        assert note["statements"][0]["age"] == "0d" and note["today"]

        hits, _ = await call(c, "search", query="vendor MIG nodes")
        assert any(h["note_id"] == added["note_id"] for h in hits["hits"])
        threads, _ = await call(c, "search", query="GPU quota migration", types=["thread"])
        assert [h["id"] for h in threads["hits"]] == ["gpu-quota-migration"]

        answer, _ = await call(c, "ask", question="does the vendor support MIG?")
        cited = {x for claim in answer["claims"] for x in claim["citations"]}
        assert cited and cited <= {s["note_id"] for s in answer["sources"]}

        active, _ = await call(c, "list_active")
        assert [t["id"] for t in active["threads"]] == ["gpu-quota-migration"]
        assert active["threads"][0]["now"] and active["threads"][0]["open_items"] == 2

        page, _ = await call(c, "get_page", name_or_id="GPU quota migration")
        assert page["id"] == "gpu-quota-migration" and page["type"] == "thread" and page["now"]["citations"]
        assert [s["kind"] for s in page["open"]] == ["question", "action"]
        person, _ = await call(c, "get_page", name_or_id="priya-raman")
        assert person["name"] == "Priya Raman" and person["related"][0]["id"] == "gpu-quota-migration"


async def test_a_correction_is_a_note(server, brain):
    async with Client(server) as c:
        await call(c, "jot", text=NOTE)
        await call(c, "jot", text=FOLLOW_UP)
        fixed, err = await call(c, "jot", text="This is finished.", about="GPU quota migration")
        assert err is None and any("closed" in line for line in fixed["changes"])
        page, _ = await call(c, "get_page", name_or_id="gpu-quota-migration")
        assert page["status"] == "closed" and page["open"] == []
        _, err = await call(c, "jot", text="x", about="no such page")
        assert "no page for" in err


async def test_brief_marks_seen_only_when_asked(server, brain):
    async with Client(server) as c:
        await call(c, "jot", text=NOTE)
        await call(c, "jot", text=FOLLOW_UP)
        result, err = await call(c, "brief", request="GPU quota migration")
        assert err is None and result["topics"][0]["type"] == "thread" and result["where_things_stand"]
        assert not brain.storage.exists(keys.BRIEF_STATE)
        await call(c, "brief", request="GPU quota migration", mark_seen=True)
        assert "gpu-quota-migration" in json.loads(brain.storage.get(keys.BRIEF_STATE))["last"]
        again, _ = await call(c, "brief", request="gpu-quota-migration")
        assert again["last_looked"] is not None


async def test_errors_carry_useful_messages(server, brain, monkeypatch):
    async with Client(server) as c:
        _, err = await call(c, "get_page", name_or_id="nobody at all")
        assert "no page for" in err
        _, err = await call(c, "get_note", note_id="n-19990101T000000Z")
        assert "no note" in err

        def down(*a, **k):
            raise LLMError("ask: HTTP 503 from fake-small: overloaded")

        await call(c, "jot", text=NOTE)
        monkeypatch.setattr(brain.llm, "run", down)
        _, err = await call(c, "ask", question="anything about MIG?")
        assert "HTTP 503" in err


async def test_jot_reports_lease_held_elsewhere(server, brain, local_storage):
    local_storage.supports_conditional_writes = True
    Lease(local_storage, machine_id="laptop", ttl_seconds=900, command="brain start").acquire()
    async with Client(server) as c:
        _, err = await call(c, "jot", text="hello")
        assert "Another machine holds the write lease" in err


async def test_stdio_subprocess(tmp_path: Path):
    """`brain mcp` as Claude Code runs it: nothing but protocol on stdout."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[storage]\nbackend = "local"\npath = "{tmp_path / "store"}"\n'
        '[llm]\nbase_url = "https://fake/v1"\nsmall_model = "fake-small"\nembed_model = "fake-embed"\n'
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
        assert len((await c.list_tools()).tools) == len(TOOLS)
        assert "Current state of the journal" in (c.instructions or "")  # injected at session start
        added, err = await call(c, "jot", text=NOTE)
        assert err is None and added["processed"]
        assert added["today"] and added["statements"][0]["age"] == "0d"
        answer, _ = await call(c, "ask", question="when does the eval cluster move?")
        assert answer["claims"]


async def test_context_resource_and_tool(server, brain):
    async with Client(server) as c:
        await call(c, "jot", text=NOTE)
        added, err = await call(c, "jot", text=FOLLOW_UP)
        assert err is None and added["processed"]
        assert "Current state" not in (c.instructions or "")  # only `brain mcp` puts it in the instructions
        r = await c.read_resource("brain://context")
        text = r.contents[0].text
        assert text.startswith("# brain: ") and "GPU quota migration [gpu-quota-migration]" in text
        tool = await c.call_tool("get_context", {})
        assert not tool.is_error and tool.content[0].text == text
