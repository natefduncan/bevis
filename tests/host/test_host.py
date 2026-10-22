import io
import json
import random
import sys
import threading
import zipfile

import pytest

from bevis import bundle
from bevis.validate import Subject, validate

from .conftest import EXAMPLE, Api, example_evidence, example_ops


def admin_of(host, agent) -> tuple[str, Api]:
    created = agent.post("/api/reports", {"title": "T", "owner": "Nate"}, expect=201)
    return created["id"], Api(host, created["admin_link"])


TEXT = {"op": "upsert_block", "block": {"id": "b1", "type": "text", "text": "[Revenue fell 12.0%]{#c1}."}}
CLAIM = {"op": "link_claim", "claim": {
    "id": "c1", "block_id": "b1", "evidence": [{"id": "q-rev-yoy", "version": 1}],
    "numbers": [{"text": "12.0%", "tier": "computed",
                 "expression": "pct_change(cell(q-rev-yoy, 2024, revenue), cell(q-rev-yoy, 2025, revenue))"}],
}}


def small_report(agent) -> str:
    rid = agent.post("/api/reports", {"title": "Small"}, expect=201)["id"]
    agent.post(f"/api/reports/{rid}/evidence", example_evidence()[2])  # q-rev-yoy
    agent.post(f"/api/reports/{rid}/edits", {"ops": [TEXT, CLAIM]})
    return rid


# -- auth -------------------------------------------------------------------------------


def test_requires_credentials(host):
    assert host.get("/api/reports").status_code == 401
    r = host.get("/api/reports", headers={"Authorization": "Bearer bvk_nope"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthenticated"


def test_revoked_key(host, agent):
    key = host.service.list_agent_keys()[0]["id"]
    host.service.revoke_agent_key(key)
    agent.get("/api/reports", expect=401)


def test_only_agent_keys_create_reports(host, agent):
    _, admin = admin_of(host, agent)
    admin.post("/api/reports", {"title": "x"}, expect=403)


def test_unknown_report_is_404_not_403(host, agent):
    _, admin = admin_of(host, agent)
    other = agent.post("/api/reports", {"title": "Other"}, expect=201)["id"]
    admin.get(f"/api/reports/{other}", expect=404)


def test_status_order_is_404_then_403_then_422(host, agent):
    """§9: an unknown or invisible report is 404 whatever the role or payload; then 403; then 422/413."""
    rid = small_report(agent)
    agent.post(f"/api/reports/{rid}/publish")
    other = agent.post("/api/reports", {"title": "Other"}, expect=201)["id"]
    admin = Api(host, _admin_secret(host, agent, rid))
    reader = Api(host, admin.post(f"/api/reports/{rid}/links", {"role": "reader", "name": "r"}, expect=201)["secret"])
    commenter = Api(host, admin.post(f"/api/reports/{rid}/links", {"role": "commenter", "name": "c"},
                                     expect=201)["secret"])

    for target in (other, "rpt_madeup"):
        reader.post(f"/api/reports/{target}/edits", {"ops": []}, expect=404)
        reader.post(f"/api/reports/{target}/validate", expect=404)
        reader.post(f"/api/reports/{target}/publish", expect=404)
        reader.get(f"/api/reports/{target}/suggestions", expect=404)
        reader.get(f"/api/reports/{target}/activity", expect=404)
        reader.get(f"/api/reports/{target}/evidence/q-rev-yoy/diff", params={"old": 1, "new": 1}, expect=404)
        reader.post(f"/api/reports/{target}/suggestions/sug_x/accept", expect=404)
        # A payload that is malformed or too large still gets 404 first.
        reader.post(f"/api/reports/{target}/evidence", {"id": "BAD"}, expect=404)
        reader.post(f"/api/reports/{target}/evidence", {**example_evidence()[0], "rows": [[1]] * 5000}, expect=404)
        reader.post(f"/api/reports/{target}/comments", {"body": ""}, expect=404)
        reader.post(f"/api/reports/{target}/challenges", {"body": "", "target": {}}, expect=404)
        reader.post(f"/api/reports/{target}/links", {"role": "nope", "name": ""}, expect=404)

    # The report is visible, the role is too low: 403, before the payload is looked at.
    reader.post(f"/api/reports/{rid}/edits", {"ops": []}, expect=403)
    reader.get(f"/api/reports/{rid}/activity", expect=403)
    reader.post(f"/api/reports/{rid}/evidence", {"id": "BAD"}, expect=403)
    reader.post(f"/api/reports/{rid}/comments", {"body": ""}, expect=403)
    reader.post(f"/api/reports/{rid}/challenges", {"body": "", "target": {}}, expect=403)
    commenter.post(f"/api/reports/{rid}/links", {"role": "nope", "name": ""}, expect=403)
    # Allowed: now the payload is checked.
    agent.post(f"/api/reports/{rid}/evidence", {"id": "BAD"}, expect=422)
    agent.post(f"/api/reports/{rid}/evidence", {**example_evidence()[0], "rows": [[1]] * 5000}, expect=413)
    commenter.post(f"/api/reports/{rid}/comments", {"body": ""}, expect=422)
    commenter.post(f"/api/reports/{rid}/challenges", {"body": "", "target": {}}, expect=422)
    admin.post(f"/api/reports/{rid}/links", {"role": "nope", "name": "x"}, expect=422)
    commenter.post(f"/api/reports/{rid}/challenges/chl_x/status", {"status": "nope"}, expect=404)
    commenter.post(f"/api/reports/{rid}/challenges/chl_x/replies", {"body": ""}, expect=404)


def test_version_outside_scope_or_zero_is_404(host, agent):
    rid = small_report(agent)
    agent.post(f"/api/reports/{rid}/publish")
    admin = Api(host, _admin_secret(host, agent, rid))
    latest = Api(host, admin.post(f"/api/reports/{rid}/links", {"role": "commenter", "name": "c"},
                                  expect=201)["secret"])
    pinned = Api(host, admin.post(f"/api/reports/{rid}/links", {"role": "reader", "name": "r", "scope": 1},
                                  expect=201)["secret"])
    for link in (latest, pinned):
        link.get(f"/api/reports/{rid}", params={"version": 7}, expect=404)
        link.get(f"/api/reports/{rid}/export.zip", params={"version": 7}, expect=404)
    latest.post(f"/api/reports/{rid}/comments", {"body": "hi", "version": 7}, expect=404)
    latest.post(f"/api/reports/{rid}/challenges", {"body": "hi", "target": {"claim": "c1"}, "version": 7},
                expect=404)

    # version=0 is not "latest".
    assert agent.get(f"/api/reports/{rid}/evidence/q-rev-yoy")["evidence"]["version"] == 1
    agent.get(f"/api/reports/{rid}/evidence/q-rev-yoy", params={"version": 0}, expect=404)
    latest.get(f"/api/reports/{rid}/evidence/q-rev-yoy", params={"version": 0}, expect=404)


# -- the example, end to end ------------------------------------------------------------


def test_rebuilds_the_example(agent, report):
    draft = agent.get(f"/api/reports/{report}")
    assert draft["kind"] == "draft" and draft["role"] == "editor"
    assert draft["validation"]["counts"] == {"errors": 0, "warnings": 0, "info": 1}
    expected = json.loads((EXAMPLE / "report.json").read_text())
    assert {k: draft[k] for k in ("blocks", "claims", "non_data")} == expected
    assert draft["next_ids"] == {"block": "b9", "claim": "c8"}


def test_publish_and_export_matches_example(agent, report):
    published = agent.post(f"/api/reports/{report}/publish")
    assert published["version"] == 1 and published["override"] is None
    r = agent.get(f"/api/reports/{report}/export.zip")
    z = zipfile.ZipFile(io.BytesIO(r.content))
    prefix = f"{report}-v1-full/"
    for name in ["report.json", *(f"evidence/{p.name}" for p in (EXAMPLE / "evidence").iterdir())]:
        assert json.loads(z.read(prefix + name)) == json.loads((EXAMPLE / name).read_text()), name
    manifest = json.loads(z.read(prefix + "manifest.json"))
    assert manifest["version"]["number"] == 1
    assert manifest["version"]["published_by"] == {"kind": "agent", "name": "claude-analyst", "verified": False}
    assert manifest["validation"] == {"errors": 0, "warnings": 0, "info": 1}


def test_export_pdf(agent, report):
    pytest.importorskip("vl_convert")
    import shutil
    if shutil.which("xelatex") is None:
        pytest.skip("needs xelatex")
    agent.post(f"/api/reports/{report}/publish")
    r = agent.get(f"/api/reports/{report}/export.pdf")
    assert r.content[:5] == b"%PDF-"
    assert agent.get(f"/api/reports/{report}/export.pdf").content == r.content  # served from the cache


# -- evidence ---------------------------------------------------------------------------


def test_evidence_versions_and_dedup(agent):
    rid = small_report(agent)
    payload = example_evidence()[2]
    same = agent.post(f"/api/reports/{rid}/evidence", payload)
    assert (same["outcome"], same["version"], same["flags"]) == ("same_result", 1, [])

    edited_sql = {**payload, "sql": payload["sql"] + " -- reformatted"}
    same = agent.post(f"/api/reports/{rid}/evidence", edited_sql)
    assert (same["outcome"], same["flags"]) == ("same_result", ["query text changed"])

    changed = {**payload, "rows": [[2024, "4210000"], [2025, "3790000"]]}
    v2 = agent.post(f"/api/reports/{rid}/evidence", changed)
    assert (v2["outcome"], v2["version"]) == ("new_version", 2)
    assert v2["diff"]["cells_changed"] == [{"key": ["2025"], "column": "revenue", "before": "3705000",
                                           "after": "3790000"}]
    assert v2["stale"] == [{"claim": "c1", "version": 1}]

    ev = agent.get(f"/api/reports/{rid}/evidence/q-rev-yoy")
    assert ev["evidence"]["version"] == 2 and ev["versions"] == [1, 2]
    assert [r["outcome"] for r in ev["runs"]] == ["created", "same_result", "same_result", "new_version"]
    diff = agent.get(f"/api/reports/{rid}/evidence/q-rev-yoy/diff", params={"old": 1, "new": 2})
    assert diff["cells_changed_total"] == 1

    # The claim is stale until repinned; repinning re-verifies it against v2.
    draft = agent.get(f"/api/reports/{rid}")
    assert "pin.stale" in {f["code"] for f in draft["validation"]["findings"]}
    out = agent.post(f"/api/reports/{rid}/edits", {"ops": [{"op": "repin", "evidence_id": "q-rev-yoy",
                                                             "version": 2}]})
    [change] = [c for c in out["changes"] if c.get("number") == 0]
    assert change["before"] == {"status": "ok", "value": "-0.1199524940617577197149643705463183"}
    assert change["after"]["status"] == "error"  # 12.0% no longer matches -0.0997...
    assert change["after"]["value"].startswith("-0.0997")


def test_dedup_flags_metadata_changes(agent):
    # Same rows, different metadata: a "same result" run, flagged (SPEC §3.4).
    rid = small_report(agent)
    payload = example_evidence()[2]
    runs = [
        ({**payload, "assumptions": ["refunds included"]}, ["assumptions changed"]),
        ({**payload, "partial": True, "source_row_count": 48211}, ["partial changed", "source row count changed"]),
        ({**payload, "key": ["year", "revenue"]}, ["key changed"]),
    ]
    for body, flags in runs:
        same = agent.post(f"/api/reports/{rid}/evidence", body)
        assert (same["outcome"], same["version"], same["flags"]) == ("same_result", 1, flags)
        assert all(f in same["note"] for f in flags) and "v1 keeps its own" in same["note"]
    ev = agent.get(f"/api/reports/{rid}/evidence/q-rev-yoy")
    assert ev["versions"] == [1]
    assert ev["evidence"]["assumptions"] == payload["assumptions"] and "partial" not in ev["evidence"]
    assert [r["flags"] for r in ev["runs"]] == [[], *[flags for _, flags in runs]]

    # A flag-free rerun keeps the plain note.
    same = agent.post(f"/api/reports/{rid}/evidence", payload)
    assert (same["flags"], same["note"]) == ([], "the data matches v1; no new version was created")


def test_create_only(agent):
    rid = small_report(agent)
    r = agent.post(f"/api/reports/{rid}/evidence", {**example_evidence()[2], "create_only": True}, expect=409)
    assert r["error"]["code"] == "evidence_exists"


def test_rolled_back_evidence_is_not_cached(host, agent, monkeypatch):
    """A version read inside a write that rolls back must not be served later (#10)."""
    rid = small_report(agent)
    claim = {**CLAIM["claim"], "evidence": [{"id": "q-rev-yoy", "version": 2}]}
    agent.post(f"/api/reports/{rid}/edits", {"ops": [{"op": "link_claim", "claim": claim}]})  # pin.unknown for now
    payload = example_evidence()[2]
    revalidate = host.service._revalidate

    def fail_after_revalidate(*args):
        revalidate(*args)  # reads the uncommitted v2 for the claim that pins it
        raise RuntimeError("a later step fails")

    monkeypatch.setattr(host.service, "_revalidate", fail_after_revalidate)
    with pytest.raises(RuntimeError):
        agent.post(f"/api/reports/{rid}/evidence", {**payload, "rows": [[2024, "4210000"], [2025, "3790000"]]})
    monkeypatch.undo()

    v2 = agent.post(f"/api/reports/{rid}/evidence", {**payload, "rows": [[2024, "4210000"], [2025, "3704000"]]})
    assert (v2["outcome"], v2["version"]) == ("new_version", 2)
    [c1] = agent.get(f"/api/reports/{rid}")["claims"]
    assert c1["status"] == "ok"  # 12.0% matches the committed v2, not the rolled-back one (-9.97%)


def test_evidence_cache_is_thread_safe(host, agent, monkeypatch):
    """Concurrent lookups and evictions in the shared cache must not raise (#10)."""
    rid = small_report(agent)
    with host.service.db.read() as conn:
        doc = conn.execute("SELECT doc FROM evidence WHERE report_id = ?", (rid,)).fetchone()["doc"]

    class Conn:  # every version resolves, so lookups keep filling and evicting the cache
        in_transaction = False

        def execute(self, *args):
            return self

        def fetchone(self):
            return {"doc": doc}

    monkeypatch.setattr(host.service, "record_cache_size", 4)  # small, so lookups often race evictions

    errors = []

    def hammer(seed):
        versions = random.Random(seed)
        try:
            for _ in range(5000):
                host.service._record(Conn(), rid, "q-rev-yoy", versions.randrange(6))
        except Exception as e:  # noqa: BLE001 - any error is the failure
            errors.append(e)

    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        threads = [threading.Thread(target=hammer, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.setswitchinterval(interval)
    assert errors == []


@pytest.mark.parametrize(
    "change, code, status",
    [
        (lambda p: p.update(id="Bad Slug"), "invalid_evidence", 422),
        (lambda p: p.update(rows=[[2024, 4210000.5]]), "invalid_evidence", 422),
        (lambda p: p.update(rows=[[2024, "1"], [2024, "2"]]), "invalid_evidence", 422),
        (lambda p: p.update(surprise=1), "invalid_evidence", 422),
        (lambda p: p.update(partial=True), "invalid_evidence", 422),
        (lambda p: p.update(rows=[[y, "1"] for y in range(2000)]), "evidence_too_large", 413),
    ],
)
def test_bad_evidence(agent, change, code, status):
    rid = agent.post("/api/reports", {"title": "x"}, expect=201)["id"]
    payload = example_evidence()[2]
    change(payload)
    assert agent.post(f"/api/reports/{rid}/evidence", payload, expect=status)["error"]["code"] == code


# -- editing ----------------------------------------------------------------------------------


def test_edit_validates_and_logs(agent):
    rid = small_report(agent)
    out = agent.post(f"/api/reports/{rid}/edits", {"ops": [
        {"op": "upsert_block", "block": {"id": "b1", "type": "text", "text": "[Revenue rose 12.0%]{#c1}."}}]})
    assert out["revision"] == 2
    assert out["validation"]["counts"]["errors"] == 1
    assert out["changes"] == [
        {"claim": "c1", "status": {"before": "ok", "after": "error"}},
        {"claim": "c1", "number": 0, "text": "12.0%",
         "before": {"status": "ok", "value": "-0.1199524940617577197149643705463183"},
         "after": {"status": "error", "value": "-0.1199524940617577197149643705463183"}},
    ]
    actions = [a["action"] for a in agent.get(f"/api/reports/{rid}/activity")]
    assert actions == ["create_report", "add_evidence", "add_block", "link_claim", "update_block"]


def test_dropping_a_live_span_is_rejected(agent):
    rid = small_report(agent)
    plain = {"op": "upsert_block", "block": {"id": "b1", "type": "text", "text": "Revenue fell 12.0%."}}
    r = agent.post(f"/api/reports/{rid}/edits", {"ops": [plain]}, expect=422)
    assert r["error"]["code"] == "span_dropped" and r["error"]["details"] == {"claims": ["c1"]}
    # Unlinking the claim in the same edit is fine, and then the number is merely uncited.
    out = agent.post(f"/api/reports/{rid}/edits", {"ops": [plain, {"op": "unlink_claim", "id": "c1"}]})
    assert [f["code"] for f in out["validation"]["findings"]] == ["text.uncited_number"]


def test_delete_block_cascades(agent):
    rid = small_report(agent)
    agent.post(f"/api/reports/{rid}/edits", {"ops": [{"op": "delete_block", "id": "b1"}]})
    draft = agent.get(f"/api/reports/{rid}")
    assert draft["blocks"] == [] and draft["claims"] == []


def test_stale_revision(agent):
    rid = small_report(agent)
    r = agent.post(f"/api/reports/{rid}/edits", {"ops": [TEXT], "base_revision": 0}, expect=409)
    assert r["error"]["code"] == "stale_revision"


@pytest.mark.parametrize("base", ["1", True, 1.0, [1]])
@pytest.mark.parametrize("path", ["edits", "publish"])
def test_base_revision_must_be_an_integer(agent, path, base):
    rid = small_report(agent)
    body = {"ops": [TEXT], "base_revision": base} if path == "edits" else {"base_revision": base}
    r = agent.post(f"/api/reports/{rid}/{path}", body, expect=422)
    assert r["error"]["code"] == "malformed" and "base_revision" in r["error"]["message"]


@pytest.mark.parametrize("method, path, body", [
    ("get", "/api/reports/{rid}?version=abc", None),
    ("get", "/api/reports/{rid}/evidence/q?version=abc", None),
    ("post", "/api/reports/{rid}/edits", [1]),
    ("post", "/api/reports", "a title"),
])
def test_request_validation_errors_use_the_error_shape(agent, method, path, body):
    rid = small_report(agent)
    kw = {} if body is None else {"json": body}
    r = getattr(agent.client, method)(path.format(rid=rid), headers=agent._headers(), **kw)
    assert r.status_code == 422, r.text
    error = r.json()["error"]
    assert set(r.json()) == {"error"} and error["code"] == "malformed" and error["message"]
    assert isinstance(error["details"], list) and error["details"][0]["loc"]


def test_request_validation_comes_after_authentication(host):
    r = host.get("/api/reports/rpt_x?version=abc", headers={"Authorization": "Bearer bvk_nope"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthenticated"


@pytest.mark.parametrize(
    "op, message",
    [
        ({"op": "explode"}, "unknown op"),
        ({"op": "upsert_block", "block": {"type": "image", "src": "x"}}, "image blocks are not supported"),
        ({"op": "upsert_block", "block": {"type": "text"}}, "invalid_draft"),
        ({"op": "upsert_block", "block": {"type": "text", "text": "x", "colour": 1}}, "has no field(s) colour"),
        ({"op": "link_claim", "claim": {"id": "claim-1", "block_id": "b1"}}, "claim id must look like"),
        ({"op": "link_claim", "claim": {"id": "c2", "block_id": "b9"}}, "no block 'b9'"),
        ({"op": "move_block", "id": "b1", "position": {"after": "b1"}}, "no block 'b1'"),
    ],
)
def test_bad_ops(agent, op, message):
    rid = small_report(agent)
    r = agent.post(f"/api/reports/{rid}/edits", {"ops": [op]}, expect=422)
    assert message in json.dumps(r)


def test_chart_with_unhashable_evidence_id_is_a_finding(agent):
    rid = small_report(agent)
    chart = {"op": "upsert_block", "block": {
        "id": "b2", "type": "chart", "evidence": [{"id": "q-rev-yoy", "version": 1}],
        "spec": {"mark": "bar", "data": {"evidence": {"id": ["x"], "version": 1}}}}}
    out = agent.post(f"/api/reports/{rid}/validate", {"ops": [chart]})
    assert "chart.data" in json.dumps(out)


@pytest.mark.parametrize(
    "mark, refused",
    [("boxplot", True), ({"type": "errorband"}, True), ("point", False), (["boxplot"], False), ({"type": ["x"]}, False)],
)
def test_composite_chart_mark_is_a_finding(agent, mark, refused):
    rid = small_report(agent)
    pin = {"id": "q-rev-yoy", "version": 1}
    chart = {"op": "upsert_block", "block": {
        "id": "b2", "type": "chart", "evidence": [pin],
        "spec": {"data": {"evidence": pin}, "layer": [{"mark": "point"}, {"mark": mark}]}}}
    out = agent.post(f"/api/reports/{rid}/validate", {"ops": [chart]})
    assert ("chart.mark" in json.dumps(out)) == refused


def test_preview_stores_nothing(agent):
    rid = small_report(agent)
    out = agent.post(f"/api/reports/{rid}/validate", {"ops": [{"op": "set_title", "title": "New"}]})
    assert out["diff"] == {"title": {"before": "Small", "after": "New"}}
    assert agent.get(f"/api/reports/{rid}")["title"] == "Small"


def test_non_data_marking(agent):
    rid = small_report(agent)
    agent.post(f"/api/reports/{rid}/edits", {"ops": [
        {"op": "upsert_block", "block": {"id": "b2", "type": "text", "text": "We looked at 3 regions."}}]})
    out = agent.post(f"/api/reports/{rid}/edits", {"ops": [
        {"op": "mark_non_data", "block_id": "b2", "text": "3", "reason": "scope, not a finding"}]})
    assert out["validation"]["counts"]["warnings"] == 0


# -- roles and suggestions -----------------------------------------------------------------------


def test_other_agents_suggest(agent, other_agent):
    rid = small_report(agent)
    assert other_agent.get(f"/api/reports/{rid}")["role"] == "suggester"
    title = {"op": "set_title", "title": "Revenue, revised"}
    r = other_agent.post(f"/api/reports/{rid}/edits", {"ops": [title]}, expect=403)
    assert "suggest_edit" in r["error"]["message"]
    other_agent.post(f"/api/reports/{rid}/evidence", example_evidence()[0])  # evidence needs no approval

    sug = other_agent.post(f"/api/reports/{rid}/suggestions", {"ops": [title], "message": "clearer"}, expect=201)
    assert sug["status"] == "open" and sug["diff"]["title"]["after"] == "Revenue, revised"
    other_agent.post(f"/api/reports/{rid}/suggestions/{sug['id']}/accept", expect=403)

    listed = agent.get(f"/api/reports/{rid}/suggestions")
    assert [s["id"] for s in listed] == [sug["id"]]
    assert agent.get(f"/api/reports/{rid}/suggestions/{sug['id']}")["preview"]["diff"]["title"]
    out = agent.post(f"/api/reports/{rid}/suggestions/{sug['id']}/accept", {"note": "thanks"})
    assert out["status"] == "accepted"
    assert agent.get(f"/api/reports/{rid}")["title"] == "Revenue, revised"
    agent.post(f"/api/reports/{rid}/suggestions/{sug['id']}/reject", expect=409)

    log = agent.get(f"/api/reports/{rid}/activity")
    by = {(a["action"], a["author"]["name"]) for a in log}
    assert ("set_title", "another-agent") in by  # the suggester authored the change
    assert ("accept_suggestion", "claude-analyst") in by


def test_rejected_suggestion_changes_nothing(agent, other_agent):
    rid = small_report(agent)
    sug = other_agent.post(f"/api/reports/{rid}/suggestions",
                           {"ops": [{"op": "set_title", "title": "No"}]}, expect=201)
    agent.post(f"/api/reports/{rid}/suggestions/{sug['id']}/reject", {"note": "keep it"})
    assert agent.get(f"/api/reports/{rid}")["title"] == "Small"
    assert agent.get(f"/api/reports/{rid}/suggestions", params={"status": "rejected"})[0]["note"] == "keep it"


def test_invalid_suggestion_is_refused(agent, other_agent):
    rid = small_report(agent)
    other_agent.post(f"/api/reports/{rid}/suggestions", {"ops": [{"op": "delete_block", "id": "zz"}]},
                     expect=422)


def test_suggestion_base_revision(agent, other_agent):
    """A suggestion may carry base_revision, checked like an edit's (SPEC §6, #11)."""
    rid = small_report(agent)
    revision = agent.get(f"/api/reports/{rid}")["revision"]
    title = {"op": "set_title", "title": "Revenue, revised"}
    r = other_agent.post(f"/api/reports/{rid}/suggestions", {"ops": [title], "base_revision": revision - 1},
                         expect=409)
    assert r["error"]["code"] == "stale_revision"
    for bad in (True, "1", 1.5):
        r = other_agent.post(f"/api/reports/{rid}/suggestions", {"ops": [title], "base_revision": bad},
                             expect=422)
        assert r["error"]["code"] == "malformed"
    assert agent.get(f"/api/reports/{rid}/suggestions") == []
    sug = other_agent.post(f"/api/reports/{rid}/suggestions", {"ops": [title], "base_revision": revision},
                           expect=201)
    assert sug["revision"] == revision
    assert agent.get(f"/api/reports/{rid}/suggestions")[0]["base_revision"] == revision


# -- publishing ----------------------------------------------------------------------------------


def test_publish_blocked_by_errors(agent):
    rid = small_report(agent)
    agent.post(f"/api/reports/{rid}/edits", {"ops": [
        {"op": "upsert_block", "block": {"id": "b1", "type": "text", "text": "[Revenue rose 12.0%]{#c1}."}}]})
    r = agent.post(f"/api/reports/{rid}/publish", expect=409)
    assert r["error"]["code"] == "validation_errors"
    assert r["error"]["details"][0]["code"] == "direction.contradicts"
    out = agent.post(f"/api/reports/{rid}/publish", {"override_reason": "the lexicon misreads this"})
    assert out["override"] == {"reason": "the lexicon misreads this", "errors_overridden": 1}
    v = agent.get(f"/api/reports/{rid}", params={"version": 1})
    assert v["override"]["reason"] == "the lexicon misreads this"


def test_versions_are_immutable(agent):
    rid = small_report(agent)
    agent.post(f"/api/reports/{rid}/publish")
    agent.post(f"/api/reports/{rid}/edits", {"ops": [{"op": "set_title", "title": "Changed"},
                                                      {"op": "delete_block", "id": "b1"}]})
    v1 = agent.get(f"/api/reports/{rid}", params={"version": 1})
    assert v1["title"] == "Small" and v1["blocks"][0]["id"] == "b1"
    assert agent.post(f"/api/reports/{rid}/publish")["version"] == 2


# -- share links and disclosure ----------------------------------------------------------------------


def test_reader_link(host, agent, report):
    agent.post(f"/api/reports/{report}/publish")
    admin = Api(host, _admin_secret(host, agent, report))
    link = admin.post(f"/api/reports/{report}/links",
                      {"role": "reader", "name": "CFO", "disclosure": "cited"}, expect=201)
    reader = Api(host, link["secret"])

    v = reader.get(f"/api/reports/{report}")
    assert v["kind"] == "version" and v["version"] == 1 and v["role"] == "reader"
    reader.get(f"/api/reports/{report}/activity", expect=403)
    reader.post(f"/api/reports/{report}/comments", {"body": "hi"}, expect=403)
    reader.post(f"/api/reports/{report}/evidence", example_evidence()[0], expect=403)

    ev = reader.get(f"/api/reports/{report}/evidence/q-top-accounts")["evidence"]
    assert ev["disclosure"]["level"] == "cited" and len(ev["rows"]) == 3
    assert "account_name" in ev["disclosure"]["withheld_columns"]

    z = zipfile.ZipFile(io.BytesIO(reader.get(f"/api/reports/{report}/export.zip").content))
    manifest = json.loads(z.read(f"{report}-v1-cited/manifest.json"))
    assert manifest["disclosure"] == "cited"

    admin.delete(f"/api/reports/{report}/links/{link['id']}")
    reader.get(f"/api/reports/{report}", expect=401)


def _admin_secret(host, agent, rid) -> str:
    # The admin link is returned once at creation; tests mint another through the service.
    from bevis.host.auth import Principal

    creator = host.service.list_agent_keys()[0]["id"]
    p = Principal(creator, "link", "Nate", "human", rid, "admin", "full", "draft")
    return host.service.create_link(p, rid, "admin", "Nate (test)")["secret"]


def test_comments_are_for_commenters_and_targets_must_exist(host, agent):
    rid = small_report(agent)
    agent.post(f"/api/reports/{rid}/publish")
    admin = Api(host, _admin_secret(host, agent, rid))
    reader = Api(host, admin.post(f"/api/reports/{rid}/links", {"role": "reader", "name": "r", "disclosure": "metadata"},
                                  expect=201)["secret"])
    commenter = Api(host, admin.post(f"/api/reports/{rid}/links", {"role": "commenter", "name": "c"},
                                     expect=201)["secret"])
    agent.post(f"/api/reports/{rid}/comments", {"body": "Revenue was 4,210 in Q3", "target": {"claim": "c1"}, "version": 1},
               expect=201)

    r = reader.get(f"/api/reports/{rid}/comments", expect=403)
    assert r["error"]["code"] == "forbidden"
    assert [c["body"] for c in commenter.get(f"/api/reports/{rid}/comments")] == ["Revenue was 4,210 in Q3"]

    # An unknown claim or block is 404, for draft viewers and published-version viewers alike.
    for who in (agent, commenter):
        who.post(f"/api/reports/{rid}/comments", {"body": "hi", "target": {"claim": "c99"}}, expect=404)
        who.post(f"/api/reports/{rid}/comments", {"body": "hi", "target": {"block": "b99"}}, expect=404)
    agent.post(f"/api/reports/{rid}/comments", {"body": "hi", "target": {"claim": "c99"}, "version": 1}, expect=404)
    commenter.post(f"/api/reports/{rid}/comments", {"body": "hi", "target": "c1"}, expect=422)
    commenter.post(f"/api/reports/{rid}/comments", {"body": "hi", "target": {"block": "b1"}}, expect=201)
    commenter.post(f"/api/reports/{rid}/comments", {"body": "hi"}, expect=201)

    # A block only in the draft can be commented on from the draft, not from the published version.
    agent.post(f"/api/reports/{rid}/edits", {"ops": [
        {"op": "upsert_block", "block": {"id": "b9", "type": "text", "text": "New."}}]})
    agent.post(f"/api/reports/{rid}/comments", {"body": "hi", "target": {"block": "b9"}}, expect=201)
    commenter.post(f"/api/reports/{rid}/comments", {"body": "hi", "target": {"block": "b9"}}, expect=404)


def test_link_scopes(host, agent):
    rid = small_report(agent)
    admin = Api(host, _admin_secret(host, agent, rid))
    admin.post(f"/api/reports/{rid}/links", {"role": "reader", "name": "x", "scope": 1}, expect=404)
    agent.post(f"/api/reports/{rid}/publish")
    agent.post(f"/api/reports/{rid}/edits", {"ops": [{"op": "set_title", "title": "Second"}]})
    agent.post(f"/api/reports/{rid}/publish")

    pinned = Api(host, admin.post(f"/api/reports/{rid}/links", {"role": "reader", "name": "x", "scope": 1},
                                  expect=201)["secret"])
    latest = Api(host, admin.post(f"/api/reports/{rid}/links", {"role": "commenter", "name": "y"},
                                  expect=201)["secret"])
    assert pinned.get(f"/api/reports/{rid}")["title"] == "Small"
    pinned.get(f"/api/reports/{rid}", params={"version": 2}, expect=404)  # outside its scope (§9)
    assert latest.get(f"/api/reports/{rid}")["title"] == "Second"

    c = latest.post(f"/api/reports/{rid}/comments", {"body": "Is c1 net of refunds?", "target": {"claim": "c1"}},
                    expect=201)
    assert c["version"] == 2
    pinned.get(f"/api/reports/{rid}/comments", expect=403)  # readers don't see comments (§8)
    pinned_commenter = Api(host, admin.post(f"/api/reports/{rid}/links",
                                            {"role": "commenter", "name": "p", "scope": 1}, expect=201)["secret"])
    assert pinned_commenter.get(f"/api/reports/{rid}/comments") == []
    assert agent.get(f"/api/reports/{rid}/comments")[0]["author"]["name"] == "y"

    r = admin.post(f"/api/reports/{rid}/links", {"role": "reader", "name": "z", "scope": "draft"}, expect=422)
    assert "only suggester" in r["error"]["message"]
    editor_link = admin.post(f"/api/reports/{rid}/links", {"role": "editor", "name": "Nate"}, expect=201)
    assert editor_link["scope"] == "draft"


def test_admin_manages_links_editor_cannot(host, agent):
    rid = small_report(agent)
    agent.post(f"/api/reports/{rid}/links", {"role": "reader", "name": "x"}, expect=403)
    admin = Api(host, _admin_secret(host, agent, rid))
    assert {link["role"] for link in admin.get(f"/api/reports/{rid}/links")} == {"admin"}


def test_exported_bundle_validates_like_the_host(agent, report, tmp_path):
    agent.post(f"/api/reports/{report}/publish")
    z = zipfile.ZipFile(io.BytesIO(agent.get(f"/api/reports/{report}/export.zip").content))
    z.extractall(tmp_path)
    b = bundle.load(tmp_path / f"{report}-v1-full")
    result = validate(Subject.from_bundle(b))
    assert result.counts() == b.manifest["validation"]
    assert result.apply(b.report) == b.report


def test_link_expiry(host, agent):
    rid = small_report(agent)
    agent.post(f"/api/reports/{rid}/publish")
    admin = Api(host, _admin_secret(host, agent, rid))
    r = admin.post(f"/api/reports/{rid}/links", {"role": "reader", "name": "x", "expires_at": "tomorrow"},
                   expect=422)
    assert "expires_at" in r["error"]["message"]
    expired = admin.post(f"/api/reports/{rid}/links",
                         {"role": "reader", "name": "x", "expires_at": "2000-01-01T00:00:00Z"}, expect=201)
    Api(host, expired["secret"]).get(f"/api/reports/{rid}", expect=401)
    live = admin.post(f"/api/reports/{rid}/links",
                      {"role": "reader", "name": "x", "expires_at": "2999-01-01T00:00:00Z"}, expect=201)
    Api(host, live["secret"]).get(f"/api/reports/{rid}")


def test_render_failure_is_a_structured_error(agent, monkeypatch):
    from bevis.host import service as service_mod
    from bevis.pdf import RenderError

    def broken(bundle, out):
        raise RenderError("! Font \\XeTeXLink@font=pzdr not loadable")

    monkeypatch.setattr(service_mod, "render_pdf", broken)
    rid = small_report(agent)
    agent.post(f"/api/reports/{rid}/publish")
    r = agent.get(f"/api/reports/{rid}/export.pdf", expect=500)
    assert r["error"]["code"] == "render_failed" and "pzdr" in r["error"]["details"]


def test_metadata_link_sees_no_values(host, agent, report):
    agent.post(f"/api/reports/{report}/publish")
    admin = Api(host, _admin_secret(host, agent, report))
    link = admin.post(f"/api/reports/{report}/links", {"role": "reader", "name": "x", "disclosure": "metadata"},
                      expect=201)
    reader = Api(host, link["secret"])
    view = reader.get(f"/api/reports/{report}", params={"render": True})
    c1 = next(c for c in view["claims"] if c["id"] == "c1")
    assert c1["numbers"][0]["value"] is None and c1["numbers"][0]["expression"].startswith("pct_change")
    ev = reader.get(f"/api/reports/{report}/evidence/q-rev-yoy")
    assert "rows" not in ev["evidence"] and ev["row_keys"] is None


# -- the web view --------------------------------------------------------------------------------


def test_view_page_and_headers(host):
    r = host.get("/view")
    assert r.status_code == 200 and "<title>Bevis</title>" in r.text
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "unsafe-eval" not in csp and "frame-ancestors 'none'" in csp
    assert "script-src 'self' https://cdn.jsdelivr.net" in csp
    assert r.headers["referrer-policy"] == "no-referrer"
    for path in ("/static/view.js", "/static/view.css", "/static/icon.svg"):
        assert host.get(path).status_code == 200
    assert host.get("/favicon.ico", follow_redirects=False).status_code == 308


def test_rendered_report(agent, report):
    agent.post(f"/api/reports/{report}/publish")
    view = agent.get(f"/api/reports/{report}", params={"version": 1, "render": True})
    b2 = next(b for b in view["blocks"] if b["id"] == "b2")
    assert '<span class="claim" data-claim="c1">' in b2["html"]
    c2 = next(c for c in view["claims"] if c["id"] == "c2")
    assert c2["numbers"][0]["cells"] == [
        {"evidence": "q-seg-delta", "version": 1, "key": ["Mid-market", "2025"], "column": "revenue"},
        {"evidence": "q-seg-delta", "version": 1, "key": ["Mid-market", "2024"], "column": "revenue"},
    ]
    assert "html" not in agent.get(f"/api/reports/{report}", params={"version": 1})["blocks"][1]


def test_row_keys_align_with_rows(agent, report):
    ev = agent.get(f"/api/reports/{report}/evidence/q-seg-delta")
    assert ev["row_keys"][0] == ["Enterprise", "2024"]
    assert len(ev["row_keys"]) == len(ev["evidence"]["rows"])


def test_bundle_zip_has_derived_files(agent, report):
    agent.post(f"/api/reports/{report}/publish")
    z = zipfile.ZipFile(io.BytesIO(agent.get(f"/api/reports/{report}/export.zip").content))
    names = set(z.namelist())
    prefix = f"{report}-v1-full/"
    assert {prefix + n for n in ("report.md", "report.html", "charts/b5.svg", "charts/b8.svg")} <= names
    assert z.read(prefix + "report.md").startswith(b"# Revenue Review")


def test_export_html(agent, report):
    agent.post(f"/api/reports/{report}/publish")
    r = agent.get(f"/api/reports/{report}/export.html")
    assert r.content.startswith(b"<!DOCTYPE html>")
    assert "sandbox" in r.headers["content-security-policy"]
    assert r.headers["content-disposition"].startswith("attachment")


def test_chart_href_does_not_break_exports(agent):
    rid = agent.post("/api/reports", {"title": "Revenue Review: 2024 vs 2025", "owner": "Nate"}, expect=201)["id"]
    for payload in example_evidence():
        agent.post(f"/api/reports/{rid}/evidence", payload)
    ops = example_ops()
    for op in ops:
        if op["op"] == "upsert_block" and op["block"]["type"] == "chart":
            op["block"]["spec"]["encoding"]["href"] = {"value": "javascript:alert(1)"}
    agent.post(f"/api/reports/{rid}/edits", {"ops": ops})
    agent.post(f"/api/reports/{rid}/publish")
    for fmt in ("zip", "html", "pdf"):
        agent.get(f"/api/reports/{rid}/export.{fmt}")


def test_oversized_body_is_refused_unread(host, agent):
    rid = agent.post("/api/reports", {"title": "x"}, expect=201)["id"]
    big = b"{" + b" " * (host.service.settings.max_bytes * 3 + 2 * 1024 * 1024) + b"}"
    r = host.post(f"/api/reports/{rid}/evidence", content=big,
                  headers={"Authorization": f"Bearer {agent.secret}", "Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["error"]["code"] == "too_large"


def test_backup_while_running(host, agent, tmp_path):
    from bevis.host.db import Database

    rid = small_report(agent)
    dest = tmp_path / "backups" / "bevis.db"
    host.service.db.backup(dest)
    copy = Database(dest)
    with copy.read() as conn:
        assert conn.execute("SELECT id FROM reports").fetchone()[0] == rid
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_failed_render_is_not_cached(host, agent, monkeypatch):
    from bevis.host import service as service_mod
    from bevis.pdf import RenderError

    def partial(bundle, out):
        out.write_bytes(b"%PDF-partial")
        raise RenderError("disk full")

    monkeypatch.setattr(service_mod, "render_pdf", partial)
    rid = small_report(agent)
    agent.post(f"/api/reports/{rid}/publish")
    agent.get(f"/api/reports/{rid}/export.pdf", expect=500)
    monkeypatch.setattr(service_mod, "render_pdf", lambda bundle, out: out.write_bytes(b"%PDF-whole"))
    assert agent.get(f"/api/reports/{rid}/export.pdf").content == b"%PDF-whole"
    assert [p.name for p in host.service.settings.export_dir.iterdir()] == [f"{rid}-v1-full.pdf"]  # no temp files


def test_concurrent_export_never_serves_a_partial_file(agent, monkeypatch):
    from bevis.host import service as service_mod

    started, release = threading.Event(), threading.Event()
    calls = []

    def slow(bundle, out):
        calls.append(out)
        out.write_text("<!DOCTYPE html><p>first half")
        if len(calls) == 1:
            started.set()
            release.wait(10)
        with out.open("a") as f:
            f.write(" and second half</p>")

    monkeypatch.setattr(service_mod, "render_html", slow)
    rid = small_report(agent)
    agent.post(f"/api/reports/{rid}/publish")
    results = {}

    def get(name):
        results[name] = agent.get(f"/api/reports/{rid}/export.html").content

    first = threading.Thread(target=get, args=("first",))
    first.start()
    assert started.wait(10)
    second = threading.Thread(target=get, args=("second",))
    second.start()
    second.join(1)  # the second request may wait for the first render, but must not read its partial file
    release.set()
    first.join(10)
    second.join(10)
    whole = b"<!DOCTYPE html><p>first half and second half</p>"
    assert results == {"first": whole, "second": whole}
    assert len(calls) == 1  # one render per export, not one per request
