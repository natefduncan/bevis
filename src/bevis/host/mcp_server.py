"""MCP front end: the same operations as REST, as tools (SPEC §9).

The caller authenticates with `Authorization: Bearer <agent key or link>`
on the MCP connection.
"""

from __future__ import annotations

import json
from typing import Any, Callable

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp_types import CallToolResult, TextContent
from pydantic import StrictInt, ValidationError

from .service import HostError, Service, malformed_error

INSTRUCTIONS = """\
Bevis hosts auditable reports: prose whose claims link to the evidence behind them.

Workflow: run your query yourself, then add_evidence with its rows. Write prose with
claim spans like [Revenue fell 12.0%]{#c1}, then link_claim c1 to its numbers:
"direct" for a formatted cell, "computed" for an expression such as
pct_change(cell(q-rev, 2024, revenue), cell(q-rev, 2025, revenue)), or "asserted".
Every write is validated; read the findings and fix errors before publishing.
On a report you did not create you are a suggester: use suggest_edit.
Readers challenge published claims; check list_open_challenges and answer with reply_to_challenge.
"""


class _Server(MCPServer):
    """Tool errors are the §9 error object itself: `{"error": {"code", "message", "details"}}`.

    The SDK prefixes a tool's error text with "Error executing tool <name>: ", and reports
    arguments that fail the input schema as pydantic text; both are replaced here.
    """

    async def call_tool(self, name: str, arguments: dict[str, Any], context: Context | None = None) -> Any:
        try:
            return await super().call_tool(name, arguments, context)
        except ToolError as exc:
            if isinstance(exc, UnexpectedToolError):
                raise
            error = _error_object(exc)
            if error is None:
                raise
            return CallToolResult(content=[TextContent(type="text", text=json.dumps({"error": error},
                                                                                    ensure_ascii=False))],
                                  is_error=True)


def _error_object(exc: BaseException) -> dict | None:
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, HostError):
            return seen.to_json()
        if isinstance(seen, ValidationError):
            return malformed_error(seen.errors())
        seen = seen.__cause__
    return None


def create_mcp(service: Service) -> MCPServer:
    mcp = _Server(name="bevis", instructions=INSTRUCTIONS)

    async def call(ctx: Context, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        headers = ctx.headers or {}
        auth = headers.get("authorization") or headers.get("Authorization")
        secret = auth[7:].strip() if auth and auth.lower().startswith("bearer ") else None

        def run() -> Any:
            return fn(service.authenticate(secret), *args, **kwargs)

        try:
            return await anyio.to_thread.run_sync(run)
        except HostError as e:
            raise ToolError(json.dumps({"error": e.to_json()}, ensure_ascii=False)) from e

    @mcp.tool()
    async def create_report(title: str, ctx: Context, owner: str | None = None) -> dict:
        """Create a report. You become its editor. The result holds a one-time admin link for its human owner."""
        return await call(ctx, service.create_report, title, owner)

    @mcp.tool()
    async def list_reports(ctx: Context) -> list[dict]:
        """Reports you can see, with your role on each."""
        return await call(ctx, service.list_reports)

    @mcp.tool()
    async def get_report(report_id: str, ctx: Context, version: int | None = None) -> dict:
        """The draft with its validation findings, or a published version when `version` is given."""
        return await call(ctx, service.get_report, report_id, version)

    @mcp.tool()
    async def add_evidence(report_id: str, evidence: dict, ctx: Context) -> dict:
        """Submit a query result. Fields: id (slug), source, sql, params, assumptions, key (columns that
        identify a row), columns [{name, type}], rows (typed values; decimals as strings), run_at,
        partial + source_row_count for samples, create_only. Resubmitting an id adds a version when the
        data changed."""
        return await call(ctx, service.add_evidence, report_id, evidence)

    @mcp.tool()
    async def get_evidence(report_id: str, evidence_id: str, ctx: Context, version: int | None = None) -> dict:
        """An evidence record (latest version unless `version` is given), with its runs."""
        return await call(ctx, service.get_evidence, report_id, evidence_id, version)

    @mcp.tool()
    async def upsert_block(report_id: str, block: dict, ctx: Context, position: dict | None = None,
                           base_revision: StrictInt | None = None) -> dict:
        """Add or replace a block (editor). block: {id?, type: heading|text|chart, ...}. heading: level,
        text. text: Markdown with claim spans [..]{#cN}. chart: Vega-Lite spec whose data is
        {"evidence": {"id", "version"}}, evidence pins, caption. position: {after|before: block id} or
        {index: n}."""
        op = {"op": "upsert_block", "block": block, **({"position": position} if position else {})}
        return await call(ctx, service.edit, report_id, [op], base_revision)

    @mcp.tool()
    async def link_claim(report_id: str, claim: dict, ctx: Context, base_revision: StrictInt | None = None) -> dict:
        """Create or replace a claim (editor). claim: {id: "cN" matching its span, block_id,
        evidence: [{id, version}], numbers: [{text, tier: direct|computed|asserted, cell | expression |
        derivation}], derivation?}. direct cell: {evidence, version, key: [...], column}."""
        return await call(ctx, service.edit, report_id, [{"op": "link_claim", "claim": claim}], base_revision)

    @mcp.tool()
    async def edit_draft(report_id: str, ops: list[dict], ctx: Context, base_revision: StrictInt | None = None) -> dict:
        """Apply several ops atomically (editor). Ops: set_title, upsert_block, move_block, delete_block,
        link_claim, unlink_claim, repin {evidence_id, version, claims?, blocks?}, mark_non_data
        {block_id, text, reason}, unmark_non_data."""
        return await call(ctx, service.edit, report_id, ops, base_revision)

    @mcp.tool()
    async def validate_report(report_id: str, ctx: Context, ops: list[dict] | None = None) -> dict:
        """Validate the draft, or preview the draft with `ops` applied. Nothing is stored."""
        return await call(ctx, service.preview, report_id, ops)

    @mcp.tool()
    async def suggest_edit(report_id: str, ops: list[dict], ctx: Context, message: str = "",
                           base_revision: StrictInt | None = None) -> dict:
        """Propose ops as one suggestion for an editor to accept (suggester and up)."""
        return await call(ctx, service.suggest, report_id, ops, message, base_revision)

    @mcp.tool()
    async def list_suggestions(report_id: str, ctx: Context, status: str = "open") -> list[dict]:
        """Suggestions on a report: open, accepted, rejected, or all."""
        return await call(ctx, service.list_suggestions, report_id, None if status == "all" else status)

    @mcp.tool()
    async def accept_edit(report_id: str, suggestion_id: str, ctx: Context, note: str = "") -> dict:
        """Accept a suggestion (editor). It is validated again against the current draft."""
        return await call(ctx, service.resolve_suggestion, report_id, suggestion_id, True, note)

    @mcp.tool()
    async def reject_edit(report_id: str, suggestion_id: str, ctx: Context, note: str = "") -> dict:
        """Reject a suggestion (editor)."""
        return await call(ctx, service.resolve_suggestion, report_id, suggestion_id, False, note)

    @mcp.tool()
    async def publish(report_id: str, ctx: Context, override_reason: str | None = None,
                      base_revision: StrictInt | None = None) -> dict:
        """Publish the draft as an immutable version (editor). Blocked by validation errors unless
        override_reason is given; the override is recorded on the version."""
        return await call(ctx, service.publish, report_id, override_reason, base_revision)

    @mcp.tool()
    async def add_comment(report_id: str, body: str, ctx: Context, target: dict | None = None,
                          version: int | None = None) -> dict:
        """Comment on the report, or on {"block": id} or {"claim": id}."""
        return await call(ctx, service.add_comment, report_id, body, target, version)

    @mcp.tool()
    async def list_comments(report_id: str, ctx: Context) -> list[dict]:
        """Comments on the report."""
        return await call(ctx, service.list_comments, report_id)

    @mcp.tool()
    async def list_open_challenges(ctx: Context, report_id: str | None = None) -> list[dict]:
        """Open challenges readers filed against published claims, on one report or every report you can
        see. Each has its target, the reader's text, highlighted cells, and the thread so far."""
        return await call(ctx, service.list_open_challenges, report_id)

    @mcp.tool()
    async def get_challenge(report_id: str, challenge_id: str, ctx: Context) -> dict:
        """One challenge with its thread."""
        return await call(ctx, service.get_challenge, report_id, challenge_id)

    @mcp.tool()
    async def reply_to_challenge(report_id: str, challenge_id: str, body: str, ctx: Context,
                                 ops: list[dict] | None = None, message: str = "") -> dict:
        """Answer a challenge: explain with `body`, and optionally propose a fix as `ops`, which becomes a
        suggestion for an editor to review. A person resolves the challenge; you cannot close it."""
        return await call(ctx, service.reply_to_challenge, report_id, challenge_id, body, ops, message)

    return mcp
