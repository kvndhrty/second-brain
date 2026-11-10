"""The HTTP API, its local-only safeguards, the background worker, and serving the built UI."""

from __future__ import annotations

import time

import pytest
from starlette.testclient import TestClient

from brain.core import runtime
from brain.core.llm.models import LLMError
from brain.core.shared import SharedBrain
from brain.core.worker import Worker
from brain.mcp.server import build_server
from brain.web.app import create_app
from tests.conftest import FAKE_LLM_SETTINGS, make_brain

NOTE = (
    "Kicked off the [[GPU quota migration]] with Priya Raman. We decided to move the eval cluster in December. "
    "Does the vendor support MIG on the new nodes?"
)
FOLLOW_UP = "Priya Raman needs to file the ticket for the [[GPU quota migration]]."


@pytest.fixture
def client(brain, tmp_path):
    app = create_app(SharedBrain(lambda: brain), static_dir=tmp_path / "no-static")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        yield c


def post(client, path, body):
    return client.post(path, json=body)


def saved(client, brain, text=NOTE, **extra) -> str:
    note_id = post(client, "/api/notes", {"text": text, **extra}).json()["id"]
    brain.process(note_id)  # no worker in these tests
    return note_id


def test_save_notes_and_read_the_wiki(client, brain):
    nid = saved(client, brain)
    saved(client, brain, FOLLOW_UP)
    status = client.get("/api/status").json()
    assert status["notes"] == 2 and status["pending"] == 0 and status["pages"] >= 2
    (thread,) = client.get("/api/active").json()
    assert thread["id"] == "gpu-quota-migration" and thread["open_items"] == 2 and thread["now"]
    note = client.get(f"/api/notes/{nid}").json()
    assert note["done"] and [s["kind"] for s in note["statements"]] == ["fact", "decision", "question"]
    assert {e["id"] for e in note["entities"]} >= {"gpu-quota-migration", "priya-raman"}
    assert client.get("/api/notes").json()[-1]["id"] == nid
    page = client.get("/api/pages/gpu-quota-migration").json()
    assert page["name"] == "GPU quota migration" and page["type"] == "thread" and page["now"]["citations"]
    assert [s["kind"] for s in page["open"]] == ["question", "action"]
    assert [r["id"] for r in page["related"]][:1] == ["priya-raman"]
    assert client.get("/api/pages/GPU quota migration").json()["id"] == "gpu-quota-migration"  # by name too
    assert [p["id"] for p in client.get("/api/pages", params={"type": "person"}).json()] == ["priya-raman"]
    assert any("New person: Priya Raman" in e["text"] for e in client.get("/api/log").json())
    hits = client.get("/api/search", params={"q": "vendor MIG"}).json()
    assert hits and hits[0]["note_id"] == nid


def test_this_is_wrong_is_a_note_about_the_page(client, brain):
    saved(client, brain)
    saved(client, brain, FOLLOW_UP)
    nid = saved(client, brain, "This is finished.", about="gpu-quota-migration")
    assert client.get(f"/api/notes/{nid}").json()["about"] == "gpu-quota-migration"
    page = client.get("/api/pages/gpu-quota-migration").json()
    assert page["status"] == "closed" and page["open"] == [] and page["now"] is None
    assert client.get("/api/active").json() == []
    assert post(client, "/api/notes", {"text": "x", "about": "no-such-page"}).status_code == 404


def test_notes_are_the_only_write(client):
    """Nothing in the API changes the wiki directly."""
    writes = sorted(
        (r.path, m)
        for r in client.app.routes
        for m in getattr(r, "methods", ())
        if m in ("POST", "PUT", "PATCH", "DELETE")
    )
    assert writes == [
        ("/api/ask", "POST"),
        ("/api/brief", "POST"),
        ("/api/dream", "POST"),
        ("/api/notes", "POST"),
        ("/api/worker/wake", "POST"),
    ]


def test_ask_and_brief(client, brain):
    nid = saved(client, brain)
    saved(client, brain, FOLLOW_UP)
    answer = post(client, "/api/ask", {"question": "does the vendor support MIG?"}).json()
    assert answer["claims"] and answer["sources"][0]["note_id"] == nid
    brief = post(client, "/api/brief", {"request": "GPU quota migration"}).json()
    assert brief["topics"][0]["type"] == "thread" and brief["sections"]["where_things_stand"]
    again = post(client, "/api/brief", {"request": "GPU quota migration"}).json()
    assert again["since"] is not None  # the first brief was recorded


def test_errors_map_to_status_codes(client, brain):
    assert client.get("/api/notes/n-19990101T000000Z").status_code == 404
    r = client.get("/api/pages/nobody")
    assert r.status_code == 404 and "no page for" in r.json()["detail"]
    assert post(client, "/api/notes", {"text": "   "}).status_code == 400
    brain.llm.responders["ask"] = lambda _user: (_ for _ in ()).throw(LLMError("gateway is down"))
    saved(client, brain)
    assert post(client, "/api/ask", {"question": "vendor MIG?"}).status_code == 502


def test_local_only_safeguards(client):
    assert client.get("/api/status", headers={"Host": "evil.example"}).status_code == 400
    r = client.post("/api/notes", content="text=hello", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 415
    r = client.post("/api/notes", content='{"text": "hi"}', headers={"Content-Type": "text/plain"})
    assert r.status_code == 415


def test_serves_built_ui_with_history_fallback(brain, tmp_path):
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<html>brain ui</html>")
    (static / "assets" / "app.js").write_text("console.log(1)")
    (static / "favicon.svg").write_text("<svg/>")
    with TestClient(create_app(SharedBrain(lambda: brain), static_dir=static), base_url="http://127.0.0.1") as c:
        assert c.get("/").text == "<html>brain ui</html>"
        assert c.get("/p/anything").text == "<html>brain ui</html>"
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


def _worker(brain, local_storage, state, tmp_path, **kw) -> tuple[Worker, object]:
    other = make_brain(
        local_storage, state, tmp_path, fake_llm=True, llm=FAKE_LLM_SETTINGS, index_name=brain.db.path.stem, **kw
    )
    return Worker(lambda: other, poll_seconds=30), other


def test_worker_processes_saved_notes(brain, local_storage, state, tmp_path):
    worker, _ = _worker(brain, local_storage, state, tmp_path)
    worker.start()
    try:
        app = create_app(SharedBrain(lambda: brain), worker=worker, static_dir=tmp_path / "x")
        with TestClient(app, base_url="http://127.0.0.1") as c:
            nid = post(c, "/api/notes", {"text": NOTE}).json()["id"]
            result = worker.process_now(nid, timeout=30)
            assert result is not None and result.error is None
            assert c.get(f"/api/notes/{nid}").json()["done"]
            assert c.get("/api/status").json()["worker"]["processed"] >= 1
            assert worker.process_now(nid, timeout=5) is not None  # already done: returns at once
    finally:
        worker.stop()


def test_worker_adopts_files_saved_into_notes(brain, local_storage, state, tmp_path):
    local_storage.put("notes/from my editor.md", b"Priya Raman is out this week.\n")
    worker, _ = _worker(brain, local_storage, state, tmp_path)
    worker.start()
    try:
        for _ in range(100):
            if worker.status.processed:
                break
            time.sleep(0.1)
        brain.sync(full=True)
        (note,) = brain.recent_notes()
        assert note.done and note.label == "from my editor"
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


def test_context_and_dream_endpoints(client, brain):
    saved(client, brain)
    saved(client, brain, FOLLOW_UP)
    ctx = client.get("/api/context").json()
    assert ctx["text"].startswith("# brain: ") and "GPU quota migration [gpu-quota-migration]" in ctx["text"]
    assert ctx["doc"]["threads"][0]["now"]
    status = client.get("/api/dream").json()
    assert status["last"] is None and set(status["planned"]) >= {"duplicates", "stale", "close", "profiles"}
    assert client.post("/api/dream", json={}).status_code == 409  # no worker in this app
    brain.dream()
    assert client.get("/api/dream").json()["last"]["run_id"]


def _wait_for_dream(worker: Worker) -> None:
    for _ in range(100):
        if worker.status.last_dream:
            break
        time.sleep(0.1)
    assert worker.status.last_dream


def test_worker_dreams_when_asked(brain, local_storage, state, tmp_path):
    worker, other = _worker(brain, local_storage, state, tmp_path)
    other.settings.dream.enabled = False  # nothing scheduled; only the request makes it dream
    worker.start()
    try:
        app = create_app(SharedBrain(lambda: brain), worker=worker, static_dir=tmp_path / "x")
        with TestClient(app, base_url="http://127.0.0.1") as c:
            nid = post(c, "/api/notes", {"text": NOTE}).json()["id"]
            assert worker.process_now(nid, timeout=30) is not None
            assert c.post("/api/dream", json={}).json() == {"requested": True}
            _wait_for_dream(worker)
            assert c.get("/api/dream").json()["last"]["run_id"] == worker.status.last_dream
    finally:
        worker.stop()


def test_a_failing_note_does_not_stop_dreaming(brain, local_storage, state, tmp_path):
    def down(_user: str) -> dict:
        raise LLMError("gateway is down")

    worker, bad = _worker(brain, local_storage, state, tmp_path, responders={"split": down})
    bad.settings.dream.enabled = False
    worker.start()
    try:
        app = create_app(SharedBrain(lambda: brain), worker=worker, static_dir=tmp_path / "x")
        with TestClient(app, base_url="http://127.0.0.1") as c:
            nid = post(c, "/api/notes", {"text": NOTE}).json()["id"]
            result = worker.process_now(nid, timeout=30)
            assert result is not None and result.error  # it stays pending and fails every round
            c.post("/api/dream", json={})
            _wait_for_dream(worker)
    finally:
        worker.stop()
