"""REST front end. Every route calls one `Service` method (SPEC §9)."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from .auth import Principal
from .config import Settings
from .mcp_server import create_mcp
from .service import HostError, Service


WEB_DIR = Path(__file__).parent / "web"

# The view runs no inline script and no eval: Vega charts use the expression
# interpreter. Vega-Embed injects its own styles, hence style 'unsafe-inline'.
VIEW_CSP = "; ".join([
    "default-src 'none'",
    "script-src 'self' https://cdn.jsdelivr.net",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "connect-src 'self'",
    "base-uri 'none'",
    "form-action 'none'",
    "frame-ancestors 'none'",
])


def bearer(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def create_app(settings: Settings | None = None) -> FastAPI:
    service = Service(settings or Settings.from_env())
    mcp = create_mcp(service)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title="Bevis", version="0.1.0", lifespan=lifespan)
    app.state.service = service
    app.mount("/mcp", mcp.streamable_http_app(streamable_http_path="/"))

    @app.exception_handler(HostError)
    async def host_error(request: Request, exc: HostError) -> JSONResponse:
        return JSONResponse({"error": exc.to_json()}, status_code=exc.status)

    def principal(authorization: str | None = Header(default=None)) -> Principal:
        return service.authenticate(bearer(authorization))

    P = Depends(principal)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.url.path == "/view" or request.url.path.startswith("/static/"):
            response.headers["Content-Security-Policy"] = VIEW_CSP
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Cache-Control"] = "no-cache"  # revalidate, so a new release shows at once
        return response

    @app.get("/view", include_in_schema=False)
    def view() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> RedirectResponse:
        return RedirectResponse("/static/icon.svg", status_code=308)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/api/me")
    def me(p: Principal = P) -> dict:
        return service.me(p)

    # reports

    @app.post("/api/reports", status_code=201)
    def create_report(p: Principal = P, body: dict = Body(...)) -> dict:
        return service.create_report(p, body.get("title"), body.get("owner"))

    @app.get("/api/reports")
    def list_reports(p: Principal = P) -> list[dict]:
        return service.list_reports(p)

    @app.get("/api/reports/{rid}")
    def get_report(rid: str, version: int | None = None, render: bool = False, p: Principal = P) -> dict:
        return service.get_report(p, rid, version, render)

    # evidence

    @app.post("/api/reports/{rid}/evidence")
    def add_evidence(rid: str, p: Principal = P, body: dict = Body(...)) -> dict:
        return service.add_evidence(p, rid, body)

    @app.get("/api/reports/{rid}/evidence/{eid}")
    def get_evidence(rid: str, eid: str, version: int | None = None, p: Principal = P) -> dict:
        return service.get_evidence(p, rid, eid, version)

    @app.get("/api/reports/{rid}/evidence/{eid}/diff")
    def diff_evidence(rid: str, eid: str, old: int, new: int, p: Principal = P) -> dict:
        return service.diff_evidence(p, rid, eid, old, new)

    # draft

    @app.post("/api/reports/{rid}/edits")
    def edit(rid: str, p: Principal = P, body: dict = Body(...)) -> dict:
        return service.edit(p, rid, body.get("ops"), body.get("base_revision"))

    @app.post("/api/reports/{rid}/validate")
    def validate_report(rid: str, p: Principal = P, body: dict[str, Any] | None = Body(default=None)) -> dict:
        return service.preview(p, rid, (body or {}).get("ops"))

    @app.post("/api/reports/{rid}/suggestions", status_code=201)
    def suggest(rid: str, p: Principal = P, body: dict = Body(...)) -> dict:
        return service.suggest(p, rid, body.get("ops"), body.get("message", ""))

    @app.get("/api/reports/{rid}/suggestions")
    def list_suggestions(rid: str, status: str | None = "open", p: Principal = P) -> list[dict]:
        return service.list_suggestions(p, rid, None if status == "all" else status)

    @app.get("/api/reports/{rid}/suggestions/{sid}")
    def get_suggestion(rid: str, sid: str, p: Principal = P) -> dict:
        return service.get_suggestion(p, rid, sid)

    @app.post("/api/reports/{rid}/suggestions/{sid}/accept")
    def accept(rid: str, sid: str, p: Principal = P, body: dict[str, Any] | None = Body(default=None)) -> dict:
        return service.resolve_suggestion(p, rid, sid, True, (body or {}).get("note", ""))

    @app.post("/api/reports/{rid}/suggestions/{sid}/reject")
    def reject(rid: str, sid: str, p: Principal = P, body: dict[str, Any] | None = Body(default=None)) -> dict:
        return service.resolve_suggestion(p, rid, sid, False, (body or {}).get("note", ""))

    # publishing and export

    @app.post("/api/reports/{rid}/publish")
    def publish(rid: str, p: Principal = P, body: dict[str, Any] | None = Body(default=None)) -> dict:
        body = body or {}
        return service.publish(p, rid, body.get("override_reason"), body.get("base_revision"))

    @app.get("/api/reports/{rid}/export.zip")
    def export_zip(rid: str, version: int | None = None, p: Principal = P) -> Response:
        name, data = service.export_bundle(p, rid, version)
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/api/reports/{rid}/export.pdf")
    def export_pdf(rid: str, version: int | None = None, p: Principal = P) -> Response:
        name, data = service.export_pdf(p, rid, version)
        return Response(data, media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{name}"'})

    # comments, activity, links

    @app.post("/api/reports/{rid}/comments", status_code=201)
    def add_comment(rid: str, p: Principal = P, body: dict = Body(...)) -> dict:
        return service.add_comment(p, rid, body.get("body"), body.get("target"), body.get("version"),
                                   body.get("display_name"))

    # challenges

    @app.post("/api/reports/{rid}/challenges", status_code=201)
    def file_challenge(rid: str, p: Principal = P, body: dict = Body(...)) -> dict:
        return service.file_challenge(p, rid, body.get("target"), body.get("body"), body.get("cells"),
                                      body.get("version"), body.get("display_name"))

    @app.get("/api/reports/{rid}/challenges")
    def list_challenges(rid: str, status: str | None = None, version: int | None = None,
                        p: Principal = P) -> list[dict]:
        return service.list_challenges(p, rid, status, version)

    @app.get("/api/reports/{rid}/challenges/{cid}")
    def get_challenge(rid: str, cid: str, p: Principal = P) -> dict:
        return service.get_challenge(p, rid, cid)

    @app.post("/api/reports/{rid}/challenges/{cid}/replies", status_code=201)
    def reply_to_challenge(rid: str, cid: str, p: Principal = P, body: dict = Body(...)) -> dict:
        return service.reply_to_challenge(p, rid, cid, body.get("body"), body.get("ops"), body.get("message", ""),
                                          body.get("display_name"))

    @app.post("/api/reports/{rid}/challenges/{cid}/status")
    def set_challenge_status(rid: str, cid: str, p: Principal = P, body: dict = Body(...)) -> dict:
        return service.set_challenge_status(p, rid, cid, body.get("status"), body.get("note", ""),
                                            body.get("display_name"))

    @app.get("/api/challenges/open")
    def list_open_challenges(report_id: str | None = None, p: Principal = P) -> list[dict]:
        return service.list_open_challenges(p, report_id)

    @app.get("/api/reports/{rid}/comments")
    def list_comments(rid: str, p: Principal = P) -> list[dict]:
        return service.list_comments(p, rid)

    @app.get("/api/reports/{rid}/activity")
    def activity(rid: str, after: int = 0, limit: int = 100, p: Principal = P) -> list[dict]:
        return service.activity(p, rid, after, limit)

    @app.post("/api/reports/{rid}/links", status_code=201)
    def create_link(rid: str, p: Principal = P, body: dict = Body(...)) -> dict:
        return service.create_link(
            p, rid, body.get("role"), body.get("name"),
            disclosure=body.get("disclosure", "full"),
            scope=body.get("scope", "latest"),
            author_kind=body.get("author_kind", "human"),
            expires_at=body.get("expires_at"),
        )

    @app.get("/api/reports/{rid}/links")
    def list_links(rid: str, p: Principal = P) -> list[dict]:
        return service.list_links(p, rid)

    @app.delete("/api/reports/{rid}/links/{lid}")
    def revoke_link(rid: str, lid: str, p: Principal = P) -> dict:
        return service.revoke_link(p, rid, lid)

    return app
