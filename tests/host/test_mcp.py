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
              "suggest_edit", "accept_edit", "publish", "add_comment", "list_open_challenges", "reply_to_challenge"}


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
    if result.is_error:
        return True, result.content[0].text
    # A list result arrives as one content block per item; the structured result holds it whole.
    structured = result.structured_content
    if isinstance(structured, dict) and set(structured) == {"result"}:
        return False, structured["result"]
    return False, structured if structured is not None else json.loads(result.content[0].text)


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


def test_challenge_tools(server):
    url, service = server
    key = service.create_agent_key("answerer")
    run = session(url, key["secret"])
    rid = run(lambda s: call(s, "create_report", title="Challenged"))[1]["id"]

    from bevis.host.auth import Principal

    agent = service.authenticate(key["secret"])
    service.add_evidence(agent, rid, example_evidence()[2])
    service.edit(agent, rid, [
        {"op": "upsert_block", "block": {"type": "text", "text": "[Revenue fell 12.0%]{#c1}."}},
        {"op": "link_claim", "claim": {"id": "c1", "block_id": "b1", "evidence": [{"id": "q-rev-yoy", "version": 1}],
                                       "numbers": [{"text": "12.0%", "tier": "computed", "expression":
                                                    "pct_change(cell(q-rev-yoy, 2024, revenue), "
                                                    "cell(q-rev-yoy, 2025, revenue))"}]}}])
    service.publish(agent, rid)
    admin = Principal("x", "link", "Owner", "human", rid, "admin", "full", "draft")
    commenter = service.authenticate(service.create_link(admin, rid, "commenter", "Reader")["secret"])
    filed = service.file_challenge(commenter, rid, {"claim": "c1"}, "Net of refunds?")

    async def flow(s):
        err, open_ = await call(s, "list_open_challenges")
        assert not err and [c["id"] for c in open_] == [filed["id"]]
        err, out = await call(s, "reply_to_challenge", report_id=rid, challenge_id=filed["id"],
                              body="Yes, refunded = false.")
        assert not err and out["status"] == "answered"
        err, open_ = await call(s, "list_open_challenges", report_id=rid)
        assert not err and open_ == []

    run(flow)


def test_tool_errors_are_the_error_object(server):
    url, service = server
    run = session(url, service.create_agent_key("shape")["secret"])
    rid = run(lambda s: call(s, "create_report", title="Shapes"))[1]["id"]

    async def flow(s):
        err, text = await call(s, "get_report", report_id="rpt_nope")
        assert err and json.loads(text)["error"]["code"] == "not_found"
        err, text = await call(s, "get_report", report_id=rid, version="abc")
        assert err
        error = json.loads(text)["error"]
        assert error["code"] == "malformed" and error["details"][0]["loc"] == ["version"]
        for base in (True, "0"):
            err, text = await call(s, "edit_draft", report_id=rid, ops=[], base_revision=base)
            assert err and json.loads(text)["error"]["code"] == "malformed"
        err, draft = await call(s, "get_report", report_id=rid)
        err, out = await call(s, "edit_draft", report_id=rid, base_revision=draft["revision"],
                              ops=[{"op": "upsert_block", "block": {"type": "text", "text": "Hi"}}])
        assert not err, out

    run(flow)
