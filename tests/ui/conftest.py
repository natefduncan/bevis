"""A real host on a local port and a real browser, for the pages in src/bevis/host/web.

The host is shared by the whole session; each test seeds its own report and people, so tests do not
touch one another. The browser is Chromium through Playwright. The tests skip when Playwright or a
Chromium it can launch is missing (set BEVIS_CHROMIUM to a binary to use one).
"""

import os
import shutil
import socket
import threading
import time
from dataclasses import dataclass

import httpx
import pytest
import uvicorn

from bevis.host.api import create_app
from bevis.host.auth import Principal
from bevis.host.config import Settings

from ..host.conftest import example_evidence, example_ops

playwright_api = pytest.importorskip("playwright.sync_api")


@dataclass
class Host:
    url: str
    service: object

    def call(self, secret: str, method: str, path: str, body=None, expect=200):
        r = httpx.request(method, f"{self.url}{path}", json=body, headers={"Authorization": f"Bearer {secret}"})
        assert r.status_code == expect, r.text
        return r.json()


@pytest.fixture(scope="session")
def host(tmp_path_factory):
    app = create_app(Settings(tmp_path_factory.mktemp("data"), max_rows=1000, max_bytes=200_000))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "the host did not start"
    yield Host(f"http://127.0.0.1:{port}", app.state.service)
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="session")
def browser():
    with playwright_api.sync_playwright() as p:
        attempts = [{}]
        exe = os.environ.get("BEVIS_CHROMIUM") or shutil.which("chromium") or shutil.which("chromium-browser")
        if exe:
            attempts.append({"executable_path": exe, "args": ["--no-sandbox"]})
        last = None
        for kw in attempts:
            try:
                b = p.chromium.launch(**kw)
                break
            except Exception as e:  # noqa: BLE001
                last = e
        else:
            pytest.skip(f"no Chromium to drive: {str(last).splitlines()[0]}")
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context(viewport={"width": 1200, "height": 900})
    pg = ctx.new_page()
    pg.set_default_timeout(8000)
    yield pg
    ctx.close()


@dataclass
class Seed:
    host: Host
    rid: str
    agent: str
    names: dict  # person name -> token

    def view(self, who: str, **extra) -> str:
        frag = "&".join(f"{k}={v}" for k, v in {"key": self.names[who], "report": self.rid, **extra}.items())
        return f"{self.host.url}/view#{frag}"

    def index(self, who: str) -> str:
        return f"{self.host.url}/view#key={self.names[who]}"

    def call(self, who: str, method: str, path: str, body=None, expect=200):
        secret = self.agent if who == "agent" else self.names[who]
        return self.host.call(secret, method, path, body, expect)


_counter = iter(range(10_000))


@pytest.fixture
def seed(host):
    """A published report with an admin (`ada`), an editor (`eli`) and a suggester (`sue`), all granted
    their role directly. Returns the Seed."""
    n = next(_counter)
    svc = host.service
    agent = svc.create_agent_key(f"analyst{n}")["secret"]
    rid = host.call(agent, "POST", "/api/reports", {"title": f"Revenue Review {n}", "owner": "Nate"}, 201)["id"]
    for e in example_evidence():
        host.call(agent, "POST", f"/api/reports/{rid}/evidence", e)
    host.call(agent, "POST", f"/api/reports/{rid}/edits", {"ops": example_ops()})
    host.call(agent, "POST", f"/api/reports/{rid}/publish")
    creator = next(k["id"] for k in svc.list_agent_keys() if k["name"] == f"analyst{n}")
    owner = Principal(creator, "link", "Nate", "human", rid, "admin", "full", "draft")
    names = {}
    for who, role in (("ada", "admin"), ("eli", "editor"), ("sue", "suggester")):
        name = f"{who}{n}"
        names[who] = svc.create_person(name)["secret"]
        svc.create_link(owner, rid, role, f"{who} {role}", person=name)
    return Seed(host, rid, agent, names)
