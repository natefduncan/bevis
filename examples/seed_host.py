"""Load the example report into a running host and print view links.

    BEVIS_URL=http://localhost:8000 BEVIS_KEY=bvk_... uv run python examples/seed_host.py

It submits the example's evidence and prose through the API, as an agent
would, publishes version 1, and makes share links at each disclosure level.
"""

import json
import os
from pathlib import Path

import httpx

EXAMPLE = Path(__file__).parent / "revenue-review"


def main() -> None:
    url = os.environ.get("BEVIS_URL", "http://localhost:8000").rstrip("/")
    agent = httpx.Client(base_url=url, headers={"Authorization": f"Bearer {os.environ['BEVIS_KEY']}"}, timeout=120)

    def ok(r: httpx.Response) -> dict:
        if r.is_error:
            raise SystemExit(f"{r.request.method} {r.request.url.path}: {r.status_code} {r.text}")
        return r.json()

    created = ok(agent.post("/api/reports", json={"title": "Revenue Review: 2024 vs 2025", "owner": "owner"}))
    rid = created["id"]
    for path in sorted((EXAMPLE / "evidence").glob("*.json")):
        doc = json.loads(path.read_text())
        fields = ("id", "source", "sql", "params", "assumptions", "key", "columns", "rows", "run_at", "partial",
                  "source_row_count")
        ok(agent.post(f"/api/reports/{rid}/evidence", json={k: doc[k] for k in fields if k in doc}))
    report = json.loads((EXAMPLE / "report.json").read_text())
    ops = [{"op": "upsert_block", "block": b} for b in report["blocks"]]
    ops += [{"op": "link_claim", "claim": {k: v for k, v in c.items() if k != "status"}} for c in report["claims"]]
    ok(agent.post(f"/api/reports/{rid}/edits", json={"ops": ops}))
    ok(agent.post(f"/api/reports/{rid}/publish"))

    admin = httpx.Client(base_url=url, headers={"Authorization": f"Bearer {created['admin_link']}"}, timeout=60)
    links = {"admin (draft)": created["admin_link"]}
    for name, body in {
        "reader, full": {"role": "reader", "name": "Reader", "disclosure": "full"},
        "reader, cited": {"role": "reader", "name": "Reader", "disclosure": "cited"},
        "reader, metadata": {"role": "reader", "name": "Reader", "disclosure": "metadata"},
    }.items():
        r = admin.post(f"/api/reports/{rid}/links", json=body)
        links[name] = ok(r)["secret"]
    print(f"report {rid}")
    for name, secret in links.items():
        print(f"  {name:18} {url}/view#key={secret}")


if __name__ == "__main__":
    main()
