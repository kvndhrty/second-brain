"""The HTTP API, its local-only safeguards, the background worker, and serving the built UI."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from brain.core import runtime
from brain.core.shared import SharedBrain
from brain.core.worker import Worker
from brain.mcp.server import build_server
from brain.web.app import create_app
from tests.conftest import FAKE_LLM_SETTINGS, make_brain

ENTRY = (
    "Kicked off the [[GPU quota migration]] with Priya. We decided to move the eval cluster in December "
    "#capacity-planning. Does the vendor support MIG on the new nodes?"
)


@pytest.fixture
def brain(local_storage, state, tmp_path):
    b = make_brain(local_storage, state, tmp_path, fake_llm=True, llm=FAKE_LLM_SETTINGS)
    yield b
    b.close()


@pytest.fixture
def client(brain, tmp_path):
    app = create_app(SharedBrain(lambda: brain), static_dir=tmp_path / "no-static")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        yield c


def post(client, path, body):
    return client.post(path, json=body)


def processed(client, brain, text=ENTRY) -> str:
    entry_id = post(client, "/api/entries", {"text": text, "title": "Kickoff"}).json()["id"]
    brain.process(entry_id)  # no worker in these tests
    return entry_id


def test_create_entry_and_read_views(client, brain):
    eid = processed(client, brain)
    status = client.get("/api/status").json()
    assert status["entries"] == 1 and status["pending"] == 0 and status["review"] >= 1
    assert [t["id"] for t in client.get("/api/active").json()] == ["gpu-quota-migration"]
    entry = client.get(f"/api/entries/{eid}").json()
    assert entry["title"] == "Kickoff" and entry["processed"] and len(entry["fragments"]) == 3
    assert client.get("/api/entries").json()[0]["id"] == eid
    view = client.get("/api/entity/thread:gpu-quota-migration").json()
    assert view["name"] == "GPU quota migration" and view["summary"]
    timeline = client.get("/api/entity/thread:gpu-quota-migration/timeline").json()
    assert timeline[0]["fragment"]["entry_id"] == eid
    assert [e["name"] for e in client.get("/api/entities/theme").json()] == ["capacity planning"]
    graph = client.get("/api/graph").json()
    assert {n["id"] for n in graph["nodes"]} == {"thread:gpu-quota-migration", "theme:capacity-planning"}
    assert graph["edges"] == [] or all(e["type"] for e in graph["edges"])
    hits = client.get("/api/search", params={"q": "vendor MIG"}).json()
    assert hits and hits[0]["entry_id"] == eid


def test_edit_entry_marks_it_for_processing(client, brain):
    eid = processed(client, brain)
    r = client.put(f"/api/entries/{eid}", json={"text": "Rewritten completely.", "title": None})
    assert r.status_code == 200
    assert client.get(f"/api/entries/{eid}").json()["processed"] is False
    assert client.get("/api/status").json()["pending"] == 1


def test_fragment_patch_someday_and_review(client, brain):
    eid = processed(client, brain, "Someday we could build a self-serve portal. Met with Zed about it.")
    frag = client.get(f"/api/entries/{eid}").json()["fragments"][0]["id"]
    assert client.patch(f"/api/fragments/{frag}", json={"someday": True}).status_code == 200
    assert [f["id"] for f in client.get("/api/someday").json()] == [frag]
    item = next(i for i in client.get("/api/review").json() if i["payload"].get("surface") == "Zed")
    r = post(client, f"/api/review/{item['id']}", {"action": "create", "name": "Zed Shaw", "role": "SRE"})
    assert r.status_code == 200
    assert [p["name"] for p in client.get("/api/entities/person").json()] == ["Zed Shaw"]
    assert client.get("/api/choices/person", params={"q": "zed"}).json()[0]["entity_id"] == "zed-shaw"


def test_ask_and_brief(client, brain):
    eid = processed(client, brain)
    answer = post(client, "/api/ask", {"question": "does the vendor support MIG?"}).json()
    assert answer["claims"] and answer["sources"][0]["entry_id"] == eid
    brief = post(client, "/api/brief", {"target": "GPU quota migration"}).json()
    assert brief["type"] == "thread" and brief["sections"]["where_things_stand"]
    again = post(client, "/api/brief", {"target": "GPU quota migration"}).json()
    assert again["since"] is not None  # the first brief was recorded


def test_errors_map_to_status_codes(client, brain):
    assert client.get("/api/entries/e-19990101T000000Z").status_code == 404
    r = client.get("/api/entity/person:nobody")
    assert r.status_code == 404 and "nothing named" in r.json()["detail"]
    assert post(client, "/api/entries", {"text": "   "}).status_code == 400
    r = post(client, "/api/review/missing", {"action": "drop"})
    assert r.status_code == 400 and "no review item" in r.json()["detail"]
    assert post(client, "/api/review/x", {"action": "link"}).status_code in (400, 422)


def test_local_only_safeguards(client):
    assert client.get("/api/status", headers={"Host": "evil.example"}).status_code == 400
    r = client.post("/api/entries", content="text=hello", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 415
    r = client.post("/api/entries", content='{"text": "hi"}', headers={"Content-Type": "text/plain"})
    assert r.status_code == 415


def test_serves_built_ui_with_history_fallback(brain, tmp_path):
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<html>brain ui</html>")
    (static / "assets" / "app.js").write_text("console.log(1)")
    (static / "favicon.svg").write_text("<svg/>")
    with TestClient(create_app(SharedBrain(lambda: brain), static_dir=static), base_url="http://127.0.0.1") as c:
        assert c.get("/").text == "<html>brain ui</html>"
        assert c.get("/threads/anything").text == "<html>brain ui</html>"
        assert c.get("/assets/app.js").text == "console.log(1)"
        assert c.get("/favicon.svg").text == "<svg/>"
        assert c.get("/api/nope").status_code == 404
        assert c.get("/../../etc/passwd").text == "<html>brain ui</html>"


def test_without_built_ui_explains_how_to_build(client):
    assert "not built" in client.get("/").text


def test_mcp_is_mounted_and_checks_host(brain, tmp_path):
    shared = SharedBrain(lambda: brain)
    app = create_app(shared, mcp_server=build_server(shared), static_dir=tmp_path / "x", port=8765)
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
    }
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        r = c.post("/mcp", json=init, headers=headers)
        assert r.status_code == 200 and "brain" in r.text
        assert c.post("/mcp", json=init, headers={**headers, "Host": "evil.example"}).status_code == 400


def test_worker_processes_saved_entries(brain, local_storage, state, tmp_path):
    other = make_brain(
        local_storage, state, tmp_path, fake_llm=True, llm=FAKE_LLM_SETTINGS, index_name=brain.db.path.stem
    )
    worker = Worker(lambda: other, poll_seconds=30)
    worker.start()
    try:
        app = create_app(SharedBrain(lambda: brain), worker=worker, static_dir=tmp_path / "x")
        with TestClient(app, base_url="http://127.0.0.1") as c:
            eid = post(c, "/api/entries", {"text": ENTRY}).json()["id"]
            result = worker.process_now(eid, timeout=30)
            assert result is not None and result.error is None
            assert c.get(f"/api/entries/{eid}").json()["processed"]
            assert c.get("/api/status").json()["worker"]["processed"] >= 1
            assert worker.process_now(eid, timeout=5) is not None  # already done: returns at once
    finally:
        worker.stop()


def test_runtime_file_ignores_dead_or_other_servers(state):
    import os

    runtime.write(state, runtime.ServerInfo(os.getpid(), 8765, "local:/store"))
    assert runtime.read(state, "local:/store").port == 8765
    assert runtime.read(state, "local:/other") is None
    runtime.write(state, runtime.ServerInfo(2**22 + 12345, 8765, "local:/store"))  # no such pid
    assert runtime.read(state, "local:/store") is None
    runtime.clear(state, 2**22 + 12345)
    assert not (state.root / runtime.FILE).exists()


def test_thread_ops_and_sweep(client, brain):
    processed(client, brain, "Started the [[Cache rewrite]]. Need to benchmark the [[Cache rewrite]].")
    op = lambda body: post(client, "/api/threads/cache-rewrite", body)  # noqa: E731
    assert op({"op": "rename", "name": "Cache v2"}).status_code == 200
    assert op({"op": "pin"}).status_code == 200
    assert client.get("/api/entity/thread:cache-rewrite").json()["details"]["pinned"] is True
    frag = client.get("/api/entity/thread:cache-rewrite/timeline").json()[0]["fragment"]["id"]
    r = op({"op": "split", "name": "Benchmarks", "fragment_ids": [frag]})
    assert r.json()["id"] == "benchmarks"
    r = post(client, "/api/threads/benchmarks", {"op": "obe", "reason": "dropped", "cascade": True})
    assert r.json()["cascaded"] == 1
    assert op({"op": "merge"}).status_code == 400
    assert post(client, "/api/threads/nope", {"op": "pin"}).status_code == 400
    sweep = client.get("/api/sweep", params={"days": 0, "quiet_days": 0}).json()
    assert {i["kind"] for i in sweep} <= {"stale_open", "stale_speculative", "quiet_thread"}
