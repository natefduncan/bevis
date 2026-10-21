import io
import json
import zipfile

import pytest

from bevis import bundle
from bevis.validate import Subject, validate

from .conftest import EXAMPLE, Api, example_evidence


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


def test_create_only(agent):
    rid = small_report(agent)
    r = agent.post(f"/api/reports/{rid}/evidence", {**example_evidence()[2], "create_only": True}, expect=409)
    assert r["error"]["code"] == "evidence_exists"


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
    pinned.get(f"/api/reports/{rid}", params={"version": 2}, expect=403)
    assert latest.get(f"/api/reports/{rid}")["title"] == "Second"

    c = latest.post(f"/api/reports/{rid}/comments", {"body": "Is c1 net of refunds?", "target": {"claim": "c1"}},
                    expect=201)
    assert c["version"] == 2
    assert pinned.get(f"/api/reports/{rid}/comments") == []
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
