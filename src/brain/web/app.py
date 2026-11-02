"""FastAPI app for `brain start`: JSON API, the built web UI, and MCP over HTTP.

It binds to 127.0.0.1 only and has no login. Two checks stop other websites
in the user's browser from using it: the Host header must be localhost (this
blocks DNS rebinding), and every write must be a JSON request (a cross-site
form or simple request cannot send that without a CORS preflight, which this
app never approves).
"""

from __future__ import annotations

import contextlib
import dataclasses
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, TypeVar

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from brain import __version__
from brain.core import review as rv
from brain.core.answers import AmbiguousTarget
from brain.core.formats.yamlio import FormatError
from brain.core.llm.models import LLMError
from brain.core.services import Brain, Busy, FragmentChange, JotFailed
from brain.core.shared import SharedBrain
from brain.core.storage.base import StorageError
from brain.core.storage.lease import LeaseError
from brain.core.views import fragment_view
from brain.core.worker import Worker

STATIC_DIR = Path(__file__).parent / "static"
LOCAL_HOSTS = ["127.0.0.1", "localhost"]
T = TypeVar("T")


class EntryIn(BaseModel):
    text: str
    title: str | None = None


class FragmentPatch(BaseModel):
    status: Literal["confirmed", "speculative", "overcome_by_events"] | None = None
    obe_reason: str | None = None
    state: Literal["open", "closed"] | None = None
    closed_note: str | None = None
    dream: bool | None = None


class AskIn(BaseModel):
    question: str


class BriefIn(BaseModel):
    target: str
    since: str | None = None
    mark_seen: bool = True


class ReviewIn(BaseModel):
    action: Literal["link", "create", "ignore", "accept", "rename", "merge", "yes", "no", "drop"]
    entity_id: str | None = None
    name: str | None = None
    role: str | None = None
    org: str | None = None
    kind: Literal["domain", "technique"] = "domain"
    into: str | None = None
    apply_to_all: bool = True


def _decision(body: ReviewIn) -> rv.ReviewDecision:
    def need(value: str | None, field: str) -> str:
        if not value:
            raise ValueError(f"'{body.action}' needs '{field}'")
        return value

    match body.action:
        case "link":
            return rv.LinkMention(need(body.entity_id, "entity_id"), apply_to_all=body.apply_to_all)
        case "create":
            return rv.CreateEntity(
                need(body.name, "name"), role=body.role, org=body.org, kind=body.kind, apply_to_all=body.apply_to_all
            )
        case "ignore":
            return rv.IgnoreMention(apply_to_all=body.apply_to_all)
        case "accept":
            return rv.AcceptProposed()
        case "rename":
            return rv.AcceptProposed(rename=need(body.name, "name"))
        case "merge":
            return rv.MergeProposed(need(body.into, "into"))
        case "yes" | "no":
            return rv.AnswerDecision(accept=body.action == "yes")
        case _:
            return rv.DropDecision()


def _data(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if isinstance(obj, list):
        return [_data(x) for x in obj]
    return obj


def create_app(
    shared: SharedBrain,
    *,
    worker: Worker | None = None,
    mcp_server: Any = None,
    static_dir: Path = STATIC_DIR,
    port: int = 8765,
    on_shutdown: Callable[[], None] | None = None,
) -> FastAPI:
    """on_shutdown runs during graceful shutdown. uvicorn re-raises SIGTERM/SIGINT after it
    shuts down, which ends the process before code after `uvicorn.run` would run."""
    mcp_app = None
    if mcp_server is not None:
        from mcp.server.transport_security import TransportSecuritySettings

        hosts = [*LOCAL_HOSTS, *(f"{h}:{port}" for h in LOCAL_HOSTS)]
        mcp_app = mcp_server.streamable_http_app(
            streamable_http_path="/mcp",
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=hosts,
                allowed_origins=[f"http://{h}" for h in hosts],
            ),
        )

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            if mcp_server is not None:
                async with mcp_server.session_manager.run():
                    yield
            else:
                yield
        finally:
            if on_shutdown is not None:
                on_shutdown()

    app = FastAPI(
        title="brain", version=__version__, lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json"
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=LOCAL_HOSTS)

    @app.middleware("http")
    async def json_writes_only(request: Request, call_next):
        if (
            request.url.path.startswith("/api/")
            and request.method in ("POST", "PUT", "PATCH", "DELETE")
            and request.headers.get("content-type", "").split(";")[0].strip() != "application/json"
        ):
            return JSONResponse({"detail": "writes must be JSON (Content-Type: application/json)"}, status_code=415)
        return await call_next(request)

    errors: list[tuple[type[BaseException] | tuple[type[BaseException], ...], int]] = [
        (KeyError, 404),
        (AmbiguousTarget, 409),
        ((ValueError, rv.ReviewError, FormatError), 400),
        ((LeaseError, Busy), 409),
        (LLMError, 502),
        ((StorageError, JotFailed), 503),
    ]
    for exc, code in errors:
        for e in exc if isinstance(exc, tuple) else (exc,):

            def handler(_: Request, err: Exception, code: int = code) -> JSONResponse:
                detail = err.args[0] if isinstance(err, KeyError) and err.args else str(err)
                body: dict[str, Any] = {"detail": str(detail)}
                if isinstance(err, AmbiguousTarget):
                    body["matches"] = [dataclasses.asdict(m) for m in err.matches]
                return JSONResponse(body, status_code=code)

            app.add_exception_handler(e, handler)  # type: ignore[arg-type]

    def call(fn: Callable[[Brain], T]) -> T:
        return shared.call(fn)

    def wake() -> None:
        if worker is not None:
            worker.wake()

    # --- read ------------------------------------------------------------------

    @app.get("/api/status")
    def status() -> dict:
        info = call(lambda b: b.status())
        info["version"] = __version__
        info["worker"] = dataclasses.asdict(worker.status) if worker else None
        return info

    @app.get("/api/active")
    def active() -> Any:
        return _data(call(lambda b: b.active_threads()))

    @app.get("/api/entities/{etype}")
    def entities(etype: Literal["person", "org", "theme", "thread"]) -> Any:
        return _data(call(lambda b: b.entities(etype)))

    @app.get("/api/entity/{node_id}")
    def entity(node_id: str) -> Any:
        return _data(call(lambda b: b.entity(node_id)))

    @app.get("/api/entity/{node_id}/timeline")
    def timeline(node_id: str) -> Any:
        return _data(call(lambda b: b.timeline(node_id)))

    @app.get("/api/entries")
    def entries(limit: int = 50, before: str | None = None) -> Any:
        return _data(call(lambda b: b.recent_entries(limit=min(max(limit, 1), 200), before=before)))

    @app.get("/api/entries/{entry_id}")
    def entry(entry_id: str) -> Any:
        return _data(call(lambda b: b.entry(entry_id)))

    @app.get("/api/dreams")
    def dreams() -> Any:
        return _data(call(lambda b: b.dreams()))

    @app.get("/api/review")
    def review_items() -> Any:
        return _data(call(lambda b: b.review_items()))

    @app.get("/api/choices/{etype}")
    def choices(etype: Literal["person", "org", "theme", "thread"], q: str) -> Any:
        return _data(call(lambda b: b.entity_choices(etype, q)))

    @app.get("/api/search")
    def search(q: str, limit: int = 20) -> Any:
        return _data(call(lambda b: b.search(q, limit=min(max(limit, 1), 50))))

    @app.get("/api/graph")
    def graph(center: str | None = None, depth: int = 2, limit: int = 250) -> Any:
        return _data(call(lambda b: b.graph(center=center, depth=min(max(depth, 1), 4), limit=min(limit, 1000))))

    @app.post("/api/ask")
    def ask(body: AskIn) -> Any:
        a = call(lambda b: b.ask(body.question))
        return {
            "question": a.question,
            "claims": _data(a.claims),
            "insufficient": a.insufficient,
            "sources": [dataclasses.asdict(s) for s in a.sources.values()],
        }

    @app.post("/api/brief")
    def brief(body: BriefIn) -> Any:
        since = datetime.fromisoformat(body.since).astimezone() if body.since else None
        r = call(lambda b: b.brief(body.target, since=since, record=body.mark_seen))
        return {
            "target": r.target,
            "label": r.label,
            "type": r.type,
            "since": r.since.isoformat() if r.since else None,
            "summary": r.summary,
            "sections": r.output.model_dump(),
            "open_items": [dataclasses.asdict(fragment_view(f)) for f in r.open_items],
            "sources": [dataclasses.asdict(s) for s in r.sources.values()],
            "notes": r.notes,
            "fragment_count": r.fragment_count,
        }

    # --- write -----------------------------------------------------------------

    @app.post("/api/entries")
    def create_entry(body: EntryIn) -> Any:
        saved = call(lambda b: b.jot(body.text, title=body.title))
        wake()
        return {"id": saved.entry.id, "key": saved.key}

    @app.put("/api/entries/{entry_id}")
    def update_entry(entry_id: str, body: EntryIn) -> Any:
        call(lambda b: b.update_entry(entry_id, body.text, title=body.title))
        wake()
        return {"id": entry_id}

    @app.patch("/api/fragments/{fragment_id}")
    def patch_fragment(fragment_id: str, body: FragmentPatch) -> Any:
        call(lambda b: b.set_fragment(fragment_id, FragmentChange(**body.model_dump())))
        return {"id": fragment_id}

    @app.post("/api/review/{item_id}")
    def resolve(item_id: str, body: ReviewIn) -> Any:
        decision = _decision(body)
        call(lambda b: b.resolve_review(item_id, decision))
        return {"id": item_id}

    @app.post("/api/worker/wake")
    def wake_worker() -> Any:
        wake()
        return {"woken": worker is not None}

    # --- MCP and the web UI ----------------------------------------------------

    if mcp_app is not None:
        app.router.routes.extend(mcp_app.routes)

    index = static_dir / "index.html"
    if index.exists():
        if (static_dir / "assets").is_dir():
            app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> Any:
            if path.startswith("api/"):
                return JSONResponse({"detail": "not found"}, status_code=404)
            candidate = static_dir / path
            if path and candidate.is_file() and static_dir in candidate.resolve().parents:
                return FileResponse(candidate)
            return FileResponse(index)
    else:

        @app.get("/", include_in_schema=False)
        def not_built() -> HTMLResponse:
            return HTMLResponse(
                "<h1>brain</h1><p>The web UI is not built. Run <code>npm ci && npm run build</code> in "
                "<code>frontend/</code>, or use the Vite dev server (<code>npm run dev</code>).</p>"
                "<p>The API is available at <a href='/api/docs'>/api/docs</a>.</p>"
            )

    return app
