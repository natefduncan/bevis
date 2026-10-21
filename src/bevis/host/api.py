"""REST front end. Every route calls one `Service` method (SPEC §9)."""

from __future__ import annotations

import contextlib
from typing import Any

from fastapi import Body, Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response

from .auth import Principal
from .config import Settings
from .mcp_server import create_mcp
from .service import HostError, Service


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

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    # reports

    @app.post("/api/reports", status_code=201)
    def create_report(p: Principal = P, body: dict = Body(...)) -> dict:
        return service.create_report(p, body.get("title"), body.get("owner"))

    @app.get("/api/reports")
    def list_reports(p: Principal = P) -> list[dict]:
        return service.list_reports(p)

    @app.get("/api/reports/{rid}")
    def get_report(rid: str, version: int | None = None, p: Principal = P) -> dict:
        return service.get_report(p, rid, version)

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
        return service.add_comment(p, rid, body.get("body"), body.get("target"), body.get("version"))

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
