import threading

import pytest

from .conftest import Api
from .test_host import _admin_secret


@pytest.fixture
def shared(host, agent, report):
    """The example, published, with an admin, a commenter link, a reader link, and a cited commenter."""
    agent.post(f"/api/reports/{report}/publish")
    admin = Api(host, _admin_secret(host, agent, report))

    def link(**body):
        return Api(host, admin.post(f"/api/reports/{report}/links", {"name": "link", **body}, expect=201)["secret"])

    return {
        "rid": report,
        "agent": agent,
        "admin": admin,
        "commenter": link(role="commenter", name="Finance team"),
        "other_commenter": link(role="commenter", name="Board"),
        "reader": link(role="reader"),
        "cited": link(role="commenter", disclosure="cited"),
    }


def file(api, rid, **body):
    body = {"target": {"claim": "c1"}, "body": "Is this net of refunds?", **body}
    return api.post(f"/api/reports/{rid}/challenges", body, expect=201)


def test_lifecycle(shared):
    rid, commenter, agent = shared["rid"], shared["commenter"], shared["agent"]
    cells = [{"evidence": "q-rev-yoy", "version": 1, "key": ["2025"], "column": "revenue"}]
    ch = file(commenter, rid, cells=cells, display_name="Dana")
    assert ch["status"] == "open" and ch["version"] == 1 and ch["mine"]
    assert ch["author"] == {"kind": "human", "name": "Dana", "verified": False, "link": "Finance team"}
    assert ch["cells"] == cells

    # The agent pulls it, across reports.
    open_ = agent.get("/api/challenges/open")
    assert [(c["report_id"], c["id"]) for c in open_] == [(rid, ch["id"])]

    # A reply from someone else answers it; an agent can't close it.
    out = agent.post(f"/api/reports/{rid}/challenges/{ch['id']}/replies",
                     {"body": "Yes: refunded = false in the query."}, expect=201)
    assert out["status"] == "answered" and not out["can_resolve"]
    r = agent.post(f"/api/reports/{rid}/challenges/{ch['id']}/status", {"status": "resolved"}, expect=403)
    assert "only a person" in r["error"]["message"]
    assert agent.get("/api/challenges/open") == []

    # The filer pushes back, which reopens it; then a person resolves it.
    out = commenter.post(f"/api/reports/{rid}/challenges/{ch['id']}/replies",
                         {"body": "And partial refunds?", "display_name": "Dana"}, expect=201)
    assert out["status"] == "open"
    out = shared["other_commenter"].post(f"/api/reports/{rid}/challenges/{ch['id']}/status",
                                         {"status": "resolved", "note": "covered", "display_name": "Lee"})
    assert out["status"] == "resolved" and not out["can_reopen"]  # not the filer, not an editor
    shared["other_commenter"].post(f"/api/reports/{rid}/challenges/{ch['id']}/status", {"status": "open"},
                                   expect=403)
    commenter.post(f"/api/reports/{rid}/challenges/{ch['id']}/replies", {"body": "x"}, expect=409)

    # The filer can reopen.
    out = commenter.post(f"/api/reports/{rid}/challenges/{ch['id']}/status", {"status": "open"})
    assert out["status"] == "open"
    assert [(e["kind"], e["to_status"]) for e in out["thread"]] == [
        ("reply", "answered"), ("reply", "open"), ("status", "resolved"), ("status", "open")]
    actions = {a["action"] for a in agent.get(f"/api/reports/{rid}/activity")}
    assert {"file_challenge", "reply_to_challenge", "resolve_challenge", "reopen_challenge"} <= actions


def test_reply_with_a_suggestion(shared, other_agent):
    rid = shared["rid"]
    ch = file(shared["commenter"], rid)
    op = {"op": "upsert_block", "block": {"id": "b2", "type": "text", "text":
          "[Revenue fell 12.0%, from $4.21M to $3.71M]{#c1}, net of refunds."}}
    out = other_agent.post(f"/api/reports/{rid}/challenges/{ch['id']}/replies",
                           {"body": "Clarified in the text.", "ops": [op]}, expect=201)
    sid = out["thread"][0]["suggestion_id"]
    assert sid and out["status"] == "answered"
    sug = shared["agent"].get(f"/api/reports/{rid}/suggestions/{sid}")
    assert sug["status"] == "open" and "challenge" in sug["message"]
    shared["commenter"].post(f"/api/reports/{rid}/challenges/{ch['id']}/replies",
                             {"body": "x", "ops": [op]}, expect=403)  # commenters can't suggest


def test_reply_checks_status_in_its_own_transaction(host, shared, other_agent, monkeypatch):
    """A challenge resolved while a reply's ops are validated is not replied to as closed (#11)."""
    rid = shared["rid"]
    ch = file(shared["commenter"], rid)
    op = {"op": "set_title", "title": "Revenue, net of refunds"}
    validate = host.service._validate
    racer = []

    def resolve_meanwhile(*args):
        if not racer:
            racer.append(threading.Thread(target=shared["admin"].post, args=(
                f"/api/reports/{rid}/challenges/{ch['id']}/status", {"status": "resolved"})))
            racer[0].start()
            racer[0].join(timeout=0.5)  # finishes here unless the reply holds the write lock
        return validate(*args)

    monkeypatch.setattr(host.service, "_validate", resolve_meanwhile)
    other_agent.post(f"/api/reports/{rid}/challenges/{ch['id']}/replies",
                     {"body": "Clarified.", "ops": [op]}, expect=201)
    racer[0].join()
    out = shared["commenter"].get(f"/api/reports/{rid}/challenges/{ch['id']}")
    assert [(e["kind"], e["from_status"], e["to_status"]) for e in out["thread"]] == [
        ("reply", "open", "answered"), ("status", "answered", "resolved")]
    assert out["status"] == "resolved"


def test_failed_reply_leaves_no_suggestion(host, shared, other_agent, monkeypatch):
    """A reply that fails after its suggestion is stored rolls the suggestion back too (#11)."""
    rid = shared["rid"]
    ch = file(shared["commenter"], rid)
    log = host.service._log

    def fail_on_reply(conn, rid_, author, action, *args):
        if action == "reply_to_challenge":
            raise RuntimeError("a later step fails")
        return log(conn, rid_, author, action, *args)

    monkeypatch.setattr(host.service, "_log", fail_on_reply)
    with pytest.raises(RuntimeError):
        other_agent.post(f"/api/reports/{rid}/challenges/{ch['id']}/replies",
                         {"body": "Clarified.", "ops": [{"op": "set_title", "title": "Renamed"}]})
    monkeypatch.undo()
    assert shared["agent"].get(f"/api/reports/{rid}/suggestions", params={"status": "all"}) == []
    assert shared["commenter"].get(f"/api/reports/{rid}/challenges/{ch['id']}")["thread"] == []


def test_who_can_see_and_file(shared):
    rid = shared["rid"]
    shared["reader"].post(f"/api/reports/{rid}/challenges",
                          {"target": {"claim": "c1"}, "body": "x"}, expect=403)
    shared["reader"].get(f"/api/reports/{rid}/challenges", expect=403)
    file(shared["commenter"], rid)
    assert len(shared["other_commenter"].get(f"/api/reports/{rid}/challenges")) == 1


@pytest.mark.parametrize(
    "body, message",
    [
        ({"target": {"claim": "c99"}}, "claim c99"),
        ({"target": {"paragraph": "b2"}}, "target is"),
        ({"body": "  "}, "needs text"),
        ({"cells": [{"evidence": "q-rev-yoy", "version": 1, "key": ["2023"], "column": "revenue"}]}, "no visible"),
        ({"cells": [{"evidence": "q-rev-yoy", "version": 1, "key": [2025], "column": "revenue"}]}, "canonical"),
    ],
)
def test_bad_challenges(shared, body, message):
    rid = shared["rid"]
    full = {"target": {"claim": "c1"}, "body": "doubt", **body}
    r = shared["commenter"].client.post(f"/api/reports/{rid}/challenges", json=full,
                                        headers=shared["commenter"]._headers())
    assert r.status_code in (404, 422) and message in r.json()["error"]["message"]


def test_cells_respect_disclosure(shared):
    rid, cited = shared["rid"], shared["cited"]
    hidden = {"evidence": "q-top-accounts", "version": 1, "key": ["A-1153"], "column": "change"}
    shown = {"evidence": "q-top-accounts", "version": 1, "key": ["A-1042"], "column": "change"}
    # A cited-level commenter cannot point at rows it cannot see (no probing for hidden keys)...
    r = cited.client.post(f"/api/reports/{rid}/challenges", headers=cited._headers(),
                          json={"target": {"claim": "c6"}, "body": "?", "cells": [hidden]})
    assert r.status_code == 422
    # ...and sees only the visible cells of a full-level commenter's challenge.
    ch = file(shared["commenter"], rid, target={"claim": "c6"}, cells=[hidden, shown])
    assert ch["cells"] == [hidden, shown]
    assert cited.get(f"/api/reports/{rid}/challenges/{ch['id']}")["cells"] == [shown]


def test_carried_over(shared):
    rid, agent, commenter = shared["rid"], shared["agent"], shared["commenter"]
    ch = file(commenter, rid)
    file(commenter, rid, target={"block": "b7"}, body="Accounts paragraph is vague")
    agent.post(f"/api/reports/{rid}/edits", {"ops": [
        {"op": "upsert_block", "block": {"id": "b2", "type": "text",
                                          "text": "[Revenue fell 12.0%, from $4.21M to $3.71M]{#c1}."}},
        {"op": "delete_block", "id": "b7"}]})
    agent.post(f"/api/reports/{rid}/publish")
    listed = commenter.get(f"/api/reports/{rid}/challenges")  # the commenter's link sees version 2 now
    assert [(c["id"], c.get("carried_over"), c.get("target_changed")) for c in listed] == [
        (ch["id"], True, False)]  # c1 is unchanged; b7 is gone from version 2
    v1 = agent.get(f"/api/reports/{rid}/challenges", params={"version": 1})
    assert len(v1) == 2 and not any(c.get("carried_over") for c in v1)


def test_dismiss_needs_an_editor(shared):
    rid = shared["rid"]
    ch = file(shared["commenter"], rid)
    shared["other_commenter"].post(f"/api/reports/{rid}/challenges/{ch['id']}/status",
                                   {"status": "dismissed"}, expect=403)
    out = shared["admin"].post(f"/api/reports/{rid}/challenges/{ch['id']}/status",
                               {"status": "dismissed", "note": "out of scope"})
    assert out["status"] == "dismissed" and out["can_reopen"]


def test_me(shared):
    me = shared["commenter"].get("/api/me")
    assert me == {"kind": "link", "name": "Finance team", "author_kind": "human", "disclosure": "full",
                  "report_id": shared["rid"], "role": "commenter", "scope": "latest"}
    assert "secret" not in shared["agent"].get("/api/me")


def test_comment_display_name(shared):
    rid = shared["rid"]
    shared["commenter"].post(f"/api/reports/{rid}/comments", {"body": "Nice", "display_name": "  Dana  K "},
                             expect=201)
    [c] = shared["agent"].get(f"/api/reports/{rid}/comments")
    assert c["author"]["name"] == "Dana K" and c["author"]["link"] == "Finance team"


def test_admin_cannot_revoke_own_link(shared):
    rid, admin = shared["rid"], shared["admin"]
    own = next(link for link in admin.get(f"/api/reports/{rid}/links") if link["current"])
    admin.delete(f"/api/reports/{rid}/links/{own['id']}", expect=409)


def test_override_over_an_unknown_pin(host, agent, report):
    """A version published over pin.unknown stays usable: the unknown pin is skipped (#1)."""
    claim = {k: v for k, v in agent.get(f"/api/reports/{report}")["claims"][0].items() if k != "status"}
    claim["evidence"] = [*claim["evidence"], {"id": "q-missing", "version": 1}]
    out = agent.post(f"/api/reports/{report}/edits", {"ops": [{"op": "link_claim", "claim": claim}]})
    assert "pin.unknown" in {f["code"] for f in out["validation"]["findings"]}
    agent.post(f"/api/reports/{report}/publish", {"override_reason": "q-missing lands next week"})
    admin = Api(host, _admin_secret(host, agent, report))

    def link(**body):
        return Api(host, admin.post(f"/api/reports/{report}/links", {"name": "link", **body}, expect=201)["secret"])

    commenter, cited = link(role="commenter"), link(role="commenter", disclosure="cited")
    cells = [{"evidence": "q-rev-yoy", "version": 1, "key": ["2025"], "column": "revenue"}]
    ch = file(commenter, report, cells=cells)
    file(commenter, report)
    gone = {**cells[0], "evidence": "q-missing"}
    r = commenter.post(f"/api/reports/{report}/challenges", {"target": {"claim": "c1"}, "body": "?", "cells": [gone]},
                       expect=422)
    assert "no visible cell" in r["error"]["message"]
    assert cited.get(f"/api/reports/{report}/challenges/{ch['id']}")["cells"] == cells
    assert commenter.get(f"/api/reports/{report}/evidence/q-rev-yoy")["evidence"]["id"] == "q-rev-yoy"
    commenter.get(f"/api/reports/{report}/evidence/q-missing", expect=404)
    # A bundle must list every version its report pins (spec/bundle rule 2), so exports still refuse.
    for fmt in ("zip", "html", "pdf"):
        r = commenter.get(f"/api/reports/{report}/export.{fmt}", expect=409)
        assert r["error"]["code"] == "missing_evidence"
