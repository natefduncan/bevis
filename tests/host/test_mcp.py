"""The MCP front end, over real streamable HTTP with a bearer header."""

import json
import socket
import threading
import time

import anyio
import httpx2
import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from bevis.host.api import create_app
from bevis.host.config import Settings

from .conftest import example_evidence

SPEC_TOOLS = {"create_report", "get_report", "add_evidence", "upsert_block", "link_claim", "validate_report",
              "suggest_edit", "accept_edit", "publish", "add_comment"}


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    app = create_app(Settings(tmp_path_factory.mktemp("mcp") / "data"))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp/", app.state.service
    srv.should_exit = True
    thread.join(timeout=5)


def session(url: str, secret: str):
    """Run `fn(session)` against the server as the holder of `secret`."""

    def run(fn):
        async def main():
            async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {secret}"}, timeout=30) as http:
                async with streamable_http_client(url, http_client=http) as (read, write, *_):
                    async with ClientSession(read, write) as s:
                        await s.initialize()
                        return await fn(s)

        return anyio.run(main)

    return run


async def call(s: ClientSession, tool: str, **args):
    result = await s.call_tool(tool, args)
    text = result.content[0].text
    return result.is_error, (json.loads(text) if not result.is_error else text)


def test_tools_are_listed(server):
    url, service = server
    run = session(url, service.create_agent_key("lister")["secret"])
    names = run(lambda s: s.list_tools())
    assert SPEC_TOOLS <= {t.name for t in names.tools}


def test_agent_workflow(server):
    url, service = server
    run = session(url, service.create_agent_key("mcp-agent")["secret"])

    async def flow(s):
        err, created = await call(s, "create_report", title="Via MCP", owner="Nate")
        assert not err and created["role"] == "editor"
        rid = created["id"]
        err, ev = await call(s, "add_evidence", report_id=rid, evidence=example_evidence()[2])
        assert not err and ev["outcome"] == "created"
        err, out = await call(s, "upsert_block", report_id=rid,
                              block={"type": "text", "text": "[Revenue fell 12.0%]{#c1}."})
        assert not err and out["validation"]["counts"]["errors"] == 1  # span without claim, for now
        err, out = await call(s, "link_claim", report_id=rid, claim={
            "id": "c1", "block_id": "b1", "evidence": [{"id": "q-rev-yoy", "version": 1}],
            "numbers": [{"text": "12.0%", "tier": "computed",
                         "expression": "pct_change(cell(q-rev-yoy, 2024, revenue), cell(q-rev-yoy, 2025, revenue))"}],
        })
        assert not err and out["validation"]["counts"] == {"errors": 0, "warnings": 0, "info": 0}
        err, pub = await call(s, "publish", report_id=rid)
        assert not err and pub["version"] == 1
        err, report = await call(s, "get_report", report_id=rid, version=1)
        assert not err and report["claims"][0]["status"] == "ok"
        return rid

    run(flow)


def test_errors_are_tool_errors(server):
    url, service = server
    run = session(url, "bvk_not-a-key")

    async def flow(s):
        err, text = await call(s, "list_reports")
        assert err and '"unauthenticated"' in text

    run(flow)


def test_suggester_gets_a_hint(server):
    url, service = server
    owner = session(url, service.create_agent_key("owner")["secret"])
    other = session(url, service.create_agent_key("other")["secret"])
    rid = owner(lambda s: call(s, "create_report", title="Owned"))[1]["id"]

    async def flow(s):
        err, text = await call(s, "upsert_block", report_id=rid, block={"type": "text", "text": "Hi"})
        assert err and "suggest_edit" in text
        err, sug = await call(s, "suggest_edit", report_id=rid,
                              ops=[{"op": "upsert_block", "block": {"type": "text", "text": "Hi"}}], message="intro")
        assert not err and sug["status"] == "open"

    other(flow)
