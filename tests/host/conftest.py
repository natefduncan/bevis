import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bevis.host.api import create_app
from bevis.host.config import Settings

EXAMPLE = Path(__file__).parents[2] / "examples" / "revenue-review"


class Api:
    """A TestClient bound to one credential."""

    def __init__(self, client: TestClient, secret: str):
        self.client = client
        self.secret = secret

    def _headers(self):
        return {"Authorization": f"Bearer {self.secret}"}

    def get(self, path, expect=200, **kw):
        r = self.client.get(path, headers=self._headers(), **kw)
        assert r.status_code == expect, r.text
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else r

    def post(self, path, body=None, expect=200):
        r = self.client.post(path, json=body, headers=self._headers())
        assert r.status_code == expect, r.text
        return r.json()

    def delete(self, path, expect=200):
        r = self.client.delete(path, headers=self._headers())
        assert r.status_code == expect, r.text
        return r.json()


@pytest.fixture
def host(tmp_path):
    app = create_app(Settings(tmp_path / "data", max_rows=1000, max_bytes=200_000))
    with TestClient(app) as client:
        client.service = app.state.service
        yield client


@pytest.fixture
def agent(host) -> Api:
    return Api(host, host.service.create_agent_key("claude-analyst")["secret"])


@pytest.fixture
def other_agent(host) -> Api:
    return Api(host, host.service.create_agent_key("another-agent")["secret"])


def example_evidence() -> list[dict]:
    """The example's evidence, as an agent submits it (the host derives the rest)."""
    out = []
    for path in sorted((EXAMPLE / "evidence").glob("*.json")):
        doc = json.loads(path.read_text())
        payload = {k: doc[k] for k in ("id", "source", "sql", "params", "assumptions", "key", "columns", "rows",
                                       "run_at")}
        for k in ("partial", "source_row_count"):
            if k in doc:
                payload[k] = doc[k]
        out.append(payload)
    return out


def example_ops() -> list[dict]:
    report = json.loads((EXAMPLE / "report.json").read_text())
    ops = [{"op": "upsert_block", "block": b} for b in report["blocks"]]
    for c in report["claims"]:
        claim = {k: v for k, v in c.items() if k != "status"}
        ops.append({"op": "link_claim", "claim": claim})
    return ops


@pytest.fixture
def report(agent) -> str:
    """A report rebuilt from the example through the API. Returns its id."""
    rid = agent.post("/api/reports", {"title": "Revenue Review: 2024 vs 2025", "owner": "Nate"}, expect=201)["id"]
    for payload in example_evidence():
        agent.post(f"/api/reports/{rid}/evidence", payload)
    agent.post(f"/api/reports/{rid}/edits", {"ops": example_ops()})
    return rid
