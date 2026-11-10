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

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from brain import __version__
from brain.core.answers import AmbiguousTarget
from brain.core.formats.yamlio import FormatError
from brain.core.llm.models import LLMError
from brain.core.services import Brain, Busy, JotFailed
from brain.core.shared import SharedBrain
from brain.core.storage.base import StorageError
from brain.core.storage.lease import LeaseError
from brain.core.views import NoSuchPage
from brain.core.worker import Worker

STATIC_DIR = Path(__file__).parent / "static"
LOCAL_HOSTS = ["127.0.0.1", "localhost"]
T = TypeVar("T")


class NoteIn(BaseModel):
    text: str
    title: str | None = None
    about: str | None = None  # id of the wiki page the note corrects ("This is wrong")


class AskIn(BaseModel):
    question: str


class BriefIn(BaseModel):
    request: str
    since: str | None = None
    mark_seen: bool = True


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
        ((KeyError, NoSuchPage), 404),
        (AmbiguousTarget, 409),
        ((ValueError, FormatError), 400),
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
                    body["matches"] = [{"id": m.id, "type": m.type, "name": m.name} for m in err.matches]
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

    @app.get("/api/pages")
    def pages(type: Literal["person", "org", "topic", "thread"] | None = None) -> Any:
        return _data(call(lambda b: b.pages(type)))

    @app.get("/api/pages/{entity_id}")
    def page(entity_id: str) -> Any:
        return _data(call(lambda b: b.page(entity_id)))

    @app.get("/api/notes")
    def notes(limit: int = 50, before: str | None = None) -> Any:
        return _data(call(lambda b: b.recent_notes(limit=min(max(limit, 1), 200), before=before)))

    @app.get("/api/notes/{note_id}")
    def note(note_id: str) -> Any:
        return _data(call(lambda b: b.note(note_id)))

    @app.get("/api/log")
    def log(limit: int = 100) -> Any:
        return _data(call(lambda b: b.log(limit=min(max(limit, 1), 500))))

    @app.get("/api/search")
    def search(q: str, limit: int = 20) -> Any:
        return _data(call(lambda b: b.search(q, limit=min(max(limit, 1), 50))))

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
        r = call(lambda b: b.brief(body.request, since=since, record=body.mark_seen))
        return {
            "request": r.request,
            "topics": _data(r.topics),
            "since": r.since.isoformat() if r.since else None,
            "sections": r.output.model_dump(),
            "sources": [dataclasses.asdict(s) for s in r.sources.values()],
        }

    # --- write -----------------------------------------------------------------

    @app.post("/api/notes")
    def create_note(body: NoteIn) -> Any:
        """Save a note. This is the only write: corrections to the wiki are notes too."""

        def run(b: Brain) -> Any:
            about = b.page(body.about).id if body.about else None
            return b.jot(body.text, title=body.title, about=about)

        saved = call(run)
        wake()
        return {"id": saved.note.id, "key": saved.key}

    @app.get("/api/context")
    def context() -> Any:
        def run(b):
            return {"doc": _data(b.context()), "text": b.context_text()}

        return call(run)

    @app.get("/api/dream")
    def dream_status() -> Any:
        def run(b):
            last = b.last_dream()
            return {
                "last": last.model_dump(mode="json") if last else None,
                "planned": dataclasses.asdict(b.dream_plan()),
                "max_calls": b.settings.dream.max_calls,
            }

        return call(run)

    @app.post("/api/dream")
    def dream_now() -> Any:
        """Ask the background worker for a pass; it runs there, not in this request."""
        if worker is None:
            raise HTTPException(409, "background processing is not running; use `brain dream`")
        worker.request_dream()
        return {"requested": True}

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
