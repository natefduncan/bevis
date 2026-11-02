"""People and memberships (SPEC §7.2)."""

import sqlite3

import pytest

from bevis.host.auth import hash_secret
from bevis.host.db import SCHEMA_VERSION, Database

from .conftest import Api
from .test_host import _admin_secret

HEADING = {"op": "upsert_block", "block": {"type": "heading", "level": 2, "text": "A new section"}}


@pytest.fixture
def nate(host) -> Api:
    return Api(host, host.service.create_person("nate")["secret"])


@pytest.fixture
def dana(host) -> Api:
    return Api(host, host.service.create_person("dana")["secret"])


@pytest.fixture
def published(agent, report) -> str:
    agent.post(f"/api/reports/{report}/publish")
    return report


def admin_secret(host, agent, rid) -> str:
    return _admin_secret(host, agent, rid)


def make_link(host, agent, rid, **body) -> str:
    admin = Api(host, admin_secret(host, agent, rid))
    return admin.post(f"/api/reports/{rid}/links", {"name": "link", **body}, expect=201)["secret"]


def claim(person: Api, secret: str, expect=200) -> dict:
    return person.post("/api/me/claims", {"link": secret}, expect=expect)


# -- the schema ---------------------------------------------------------------------------


def test_schema_has_people(tmp_path):
    Database(tmp_path / "bevis.db")
    conn = sqlite3.connect(tmp_path / "bevis.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 3
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"persons", "memberships"} <= tables
    assert "bound_person_id" in {r[1] for r in conn.execute("PRAGMA table_info(credentials)")}


def test_a_version_2_database_migrates(tmp_path):
    from bevis.host import db as db_mod

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(db_mod.SCHEMA)
    conn.executescript(f"BEGIN; {db_mod.MIGRATIONS[0]} PRAGMA user_version=2; COMMIT;")
    conn.close()
    Database(path)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM memberships").fetchone()[0] == 0


# -- the person token ---------------------------------------------------------------------


def test_a_person_authenticates_and_has_no_reports_yet(nate):
    me = nate.get("/api/me")
    assert me["kind"] == "person" and me["name"] == "nate" and me["author_kind"] == "human"
    assert nate.get("/api/reports") == []
    assert nate.get("/api/me/reports") == []
    assert nate.get("/api/me/inbox") == []


def test_names_are_unique_and_secrets_are_hashed(host):
    host.service.create_person("nate")
    with pytest.raises(Exception, match="already exists"):
        host.service.create_person("nate")
    token = host.service.create_person("lee")["secret"]
    conn = sqlite3.connect(host.service.settings.db_path)
    assert token not in repr(conn.execute("SELECT * FROM persons").fetchall())


def test_a_person_cannot_create_reports(nate):
    nate.post("/api/reports", {"title": "Mine"}, expect=403)


def test_only_a_person_claims_or_has_an_index(agent):
    agent.post("/api/me/claims", {"link": "bvl_x"}, expect=403)
    agent.get("/api/me/reports", expect=403)
    agent.get("/api/me/inbox", expect=403)


# -- claiming ------------------------------------------------------------------------------


def test_claiming_a_link_adds_the_report(host, agent, nate, published):
    secret = admin_secret(host, agent, published)
    out = claim(nate, secret)
    assert out["report_id"] == published and out["role"] == "admin" and out["created"] is True
    assert [r["id"] for r in nate.get("/api/reports")] == [published]
    got = nate.get(f"/api/reports/{published}")
    assert got["role"] == "admin" and got["kind"] == "draft"
    # The link itself still works for whoever holds it.
    assert Api(host, secret).get("/api/me")["report_id"] == published


def test_claiming_twice_is_a_no_op(host, agent, nate, published):
    secret = admin_secret(host, agent, published)
    claim(nate, secret)
    assert claim(nate, secret)["created"] is False
    assert len(nate.get("/api/me/reports")) == 1
    log = Api(host, secret).get(f"/api/reports/{published}/activity")
    assert [e["action"] for e in log].count("claim_link") == 1


def test_a_link_that_cannot_be_claimed_is_404(host, agent, nate, dana, published):
    claim(nate, "bvl_nonsense", expect=404)
    secret = make_link(host, agent, published, role="reader")
    admin = Api(host, admin_secret(host, agent, published))
    lid = next(l["id"] for l in admin.get(f"/api/reports/{published}/links") if l["role"] == "reader")
    admin.delete(f"/api/reports/{published}/links/{lid}")
    claim(nate, secret, expect=404)
    claim(nate, "", expect=422)

    # A link bound to someone else is 404 for everyone else, and unusable bare.
    other = make_link(host, agent, published, role="reader")
    conn = sqlite3.connect(host.service.settings.db_path)
    conn.execute("UPDATE credentials SET bound_person_id = ? WHERE secret_hash = ?",
                 (dana.get("/api/me")["person_id"], hash_secret(other)))
    conn.commit()
    claim(nate, other, expect=404)
    Api(host, other).get("/api/me", expect=401)
    assert claim(dana, other)["created"] is True


def test_a_revoked_link_stops_granting_through_a_membership(host, agent, nate, published):
    reader = make_link(host, agent, published, role="reader")
    claim(nate, reader)
    nate.get(f"/api/reports/{published}")
    admin = Api(host, admin_secret(host, agent, published))
    lid = next(l["id"] for l in admin.get(f"/api/reports/{published}/links") if l["role"] == "reader")
    admin.delete(f"/api/reports/{published}/links/{lid}")
    nate.get(f"/api/reports/{published}", expect=404)
    assert nate.get("/api/me/reports") == []


def test_an_expired_link_stops_granting(host, agent, nate, published):
    link = make_link(host, agent, published, role="reader", expires_at="2099-01-01T00:00:00Z")
    claim(nate, link)
    conn = sqlite3.connect(host.service.settings.db_path)
    conn.execute("UPDATE credentials SET expires_at = '2000-01-01T00:00:00Z' WHERE role = 'reader'")
    conn.commit()
    nate.get(f"/api/reports/{published}", expect=404)


# -- effective access ---------------------------------------------------------------------


def test_a_report_with_no_membership_is_404(nate, published):
    nate.get(f"/api/reports/{published}", expect=404)
    nate.get(f"/api/reports/{published}/evidence/q-rev-yoy", expect=404)


def test_the_most_capable_link_wins_without_mixing(host, agent, nate, published):
    claim(nate, make_link(host, agent, published, role="reader", disclosure="full"))
    assert nate.get(f"/api/reports/{published}")["role"] == "reader"
    claim(nate, make_link(host, agent, published, role="editor", disclosure="cited"))
    got = nate.get(f"/api/reports/{published}")
    assert got["role"] == "editor" and got["kind"] == "draft"
    # The editor link's cited disclosure applies, not the reader link's full.
    assert host.service.authenticate(nate.secret, published).disclosure == "cited"
    assert nate.get("/api/reports")[0]["role"] == "editor"


def test_a_reader_membership_sees_published_only(host, agent, nate, published):
    claim(nate, make_link(host, agent, published, role="reader"))
    assert nate.get(f"/api/reports/{published}")["kind"] == "version"
    nate.post(f"/api/reports/{published}/edits", {"ops": [HEADING]}, expect=403)
    nate.post(f"/api/reports/{published}/links", {"role": "reader", "name": "x"}, expect=403)


def test_a_person_edits_as_themselves(host, agent, nate, published):
    claim(nate, admin_secret(host, agent, published))
    out = nate.post(f"/api/reports/{published}/edits", {"ops": [HEADING]})
    assert out["revision"] > 0
    log = nate.get(f"/api/reports/{published}/activity")
    edit = [e for e in log if e["action"] == "add_block"][-1]
    assert edit["author"]["name"] == "nate" and edit["author"]["person_id"] == nate.get("/api/me")["person_id"]
    assert edit["author"]["kind"] == "human"


def test_a_challenge_is_theirs_across_links(host, agent, nate, published):
    claim(nate, make_link(host, agent, published, role="commenter"))
    ch = nate.post(f"/api/reports/{published}/challenges", {"target": {"claim": "c1"}, "body": "Net of refunds?"},
                   expect=201)
    assert ch["mine"] and ch["author"]["person_id"] == nate.get("/api/me")["person_id"]
    # Holding a second link later does not lose the challenge.
    claim(nate, make_link(host, agent, published, role="commenter", name="again"))
    assert nate.get(f"/api/reports/{published}/challenges/{ch['id']}")["mine"]


def test_open_challenges_across_reports_use_each_membership(host, agent, nate, published):
    claim(nate, make_link(host, agent, published, role="commenter"))
    nate.post(f"/api/reports/{published}/challenges", {"target": {"claim": "c1"}, "body": "?"}, expect=201)
    assert [c["report_id"] for c in nate.get("/api/challenges/open")] == [published]


# -- grants -------------------------------------------------------------------------------


def test_an_admin_grants_a_bound_link(host, agent, nate, published):
    admin = Api(host, admin_secret(host, agent, published))
    out = admin.post(f"/api/reports/{published}/links",
                     {"name": "for nate", "role": "editor", "person": "nate"}, expect=201)
    assert "secret" not in out and out["person_id"] == nate.get("/api/me")["person_id"]
    assert nate.get(f"/api/reports/{published}")["role"] == "editor"
    links = admin.get(f"/api/reports/{published}/links")
    assert [l["bound_person_id"] for l in links if l["name"] == "for nate"] == [out["person_id"]]
    log = admin.get(f"/api/reports/{published}/activity")
    assert "grant" in [e["action"] for e in log]


def test_a_grant_to_nobody_is_404(host, agent, published):
    admin = Api(host, admin_secret(host, agent, published))
    admin.post(f"/api/reports/{published}/links", {"name": "x", "role": "reader", "person": "nobody"}, expect=404)
    admin.post(f"/api/reports/{published}/links", {"name": "x", "role": "reader", "person": ""}, expect=422)


def test_a_revoked_person_cannot_be_granted(host, agent, nate, published):
    host.service.revoke_person("nate")
    admin = Api(host, admin_secret(host, agent, published))
    admin.post(f"/api/reports/{published}/links", {"name": "x", "role": "reader", "person": "nate"}, expect=404)


# -- leaving and removal ------------------------------------------------------------------


def test_leaving_keeps_the_link_valid(host, agent, nate, published):
    secret = make_link(host, agent, published, role="reader")
    claim(nate, secret)
    nate.post(f"/api/reports/{published}/leave")
    assert nate.get("/api/me/reports") == []
    nate.get(f"/api/reports/{published}", expect=404)
    nate.post(f"/api/reports/{published}/leave", expect=404)
    assert claim(nate, secret)["created"] is True


def _grant(host, agent, rid, person, role="admin"):
    admin = Api(host, admin_secret(host, agent, rid))
    return admin.post(f"/api/reports/{rid}/links", {"name": f"for {person}", "role": role, "person": person},
                      expect=201)


def test_the_only_admin_cannot_leave(host, agent, nate, published):
    # Nate's admin link is bound to him and has no secret, so leaving would orphan the report's admin.
    _grant(host, agent, published, "nate")
    _revoke_other_admin_links(host, published, keep="for nate")
    err = nate.post(f"/api/reports/{published}/leave", expect=409)
    assert err["error"]["code"] == "last_admin"
    assert nate.get("/api/me/reports")  # still a member


def test_an_admin_can_leave_when_another_admin_remains(host, agent, nate, dana, published):
    _grant(host, agent, published, "nate")
    _grant(host, agent, published, "dana")
    _revoke_other_admin_links(host, published, keep=("for nate", "for dana"))
    nate.post(f"/api/reports/{published}/leave")
    # Dana is now the last one.
    assert dana.post(f"/api/reports/{published}/leave", expect=409)["error"]["code"] == "last_admin"


def test_an_admin_can_leave_when_an_unbound_admin_link_exists(host, agent, nate, published):
    _grant(host, agent, published, "nate")
    admin_secret(host, agent, published)  # an admin link with a secret someone holds
    nate.post(f"/api/reports/{published}/leave")


def test_a_non_admin_can_always_leave(host, agent, nate, published):
    _grant(host, agent, published, "nate", role="editor")
    _revoke_other_admin_links(host, published, keep="for nate")
    nate.post(f"/api/reports/{published}/leave")


def _revoke_other_admin_links(host, rid, keep):
    keep = {keep} if isinstance(keep, str) else set(keep)
    from bevis.host.auth import Principal

    creator = host.service.list_agent_keys()[0]["id"]
    p = Principal(creator, "link", "Nate", "human", rid, "admin", "full", "draft")
    for l in host.service.list_links(p, rid):
        if l["role"] == "admin" and l["name"] not in keep and not l["revoked_at"]:
            host.service.revoke_link(p, rid, l["id"])


def test_members_are_listed_and_removed(host, agent, nate, dana, published):
    claim(nate, admin_secret(host, agent, published))
    shared = make_link(host, agent, published, role="commenter")
    claim(dana, shared)
    members = nate.get(f"/api/reports/{published}/members")
    assert {(m["name"], m["role"]) for m in members} == {("nate", "admin"), ("dana", "commenter")}
    assert next(m for m in members if m["name"] == "nate")["current"] is True

    nate.delete(f"/api/reports/{published}/members/{nate.get('/api/me')['person_id']}", expect=409)
    out = nate.delete(f"/api/reports/{published}/members/{dana.get('/api/me')['person_id']}")
    assert len(out["revoked_links"]) == 1 and out["also_removed"] == []
    dana.get(f"/api/reports/{published}", expect=404)
    Api(host, shared).get("/api/me", expect=401)  # the link behind the membership is revoked
    nate.delete(f"/api/reports/{published}/members/{dana.get('/api/me')['person_id']}", expect=404)


def test_removing_a_member_names_others_on_the_same_link(host, agent, nate, dana, published):
    claim(nate, admin_secret(host, agent, published))
    shared = make_link(host, agent, published, role="reader")
    claim(dana, shared)
    lee = Api(host, host.service.create_person("lee")["secret"])
    claim(lee, shared)
    members = nate.get(f"/api/reports/{published}/members")
    assert {m["shared_with"] for m in members if m["role"] == "reader"} == {1}
    out = nate.delete(f"/api/reports/{published}/members/{dana.get('/api/me')['person_id']}")
    assert out["also_removed"] == [lee.get("/api/me")["person_id"]]
    lee.get(f"/api/reports/{published}", expect=404)


def test_members_are_for_admins(host, agent, nate, published):
    claim(nate, make_link(host, agent, published, role="editor"))
    nate.get(f"/api/reports/{published}/members", expect=403)


def test_a_person_revoked_then_rotated_keeps_their_reports(host, agent, nate, published):
    claim(nate, admin_secret(host, agent, published))
    old = nate.secret
    host.service.revoke_person("nate")
    nate.get("/api/me", expect=401)
    fresh = Api(host, host.service.rotate_person("nate")["secret"])
    old_client = Api(host, old)
    old_client.get("/api/me", expect=401)
    assert [r["id"] for r in fresh.get("/api/me/reports")] == [published]


# -- the index and the inbox --------------------------------------------------------------


def test_the_index_counts_what_a_draft_holder_can_see(host, agent, other_agent, nate, published):
    claim(nate, admin_secret(host, agent, published))
    row = nate.get("/api/me/reports")[0]
    assert row["id"] == published and row["role"] == "admin" and row["latest_version"] == 1
    assert row["unpublished_changes"] is False and row["pending_suggestions"] == 0
    assert row["needs_review"] == 0 and row["unresolved_pins"] == 0
    assert row["open_challenges"] == {"awaiting_me": 0, "awaiting_agent": 0}
    assert isinstance(row["revision"], int) and row["last_activity"]

    agent.post(f"/api/reports/{published}/edits", {"ops": [HEADING]})
    other_agent.post(f"/api/reports/{published}/suggestions", {"ops": [HEADING], "message": "add"}, expect=201)
    row = nate.get("/api/me/reports")[0]
    assert row["unpublished_changes"] is True and row["pending_suggestions"] == 1


def test_a_reader_sees_nulls_not_zeros(host, agent, nate, published):
    claim(nate, make_link(host, agent, published, role="reader"))
    row = nate.get("/api/me/reports")[0]
    assert row["role"] == "reader" and row["latest_version"] == 1
    for key in ("revision", "unpublished_changes", "pending_suggestions", "needs_review", "unresolved_pins",
                "open_challenges"):
        assert row[key] is None, key
    assert row["last_activity"]


def test_stale_pins_are_counted(host, agent, nate, published):
    claim(nate, admin_secret(host, agent, published))
    from .conftest import example_evidence

    changed = dict(next(e for e in example_evidence() if e["id"] == "q-rev-yoy"))
    changed["rows"] = [list(r) for r in changed["rows"]]
    changed["rows"][0][1] = "999"
    agent.post(f"/api/reports/{published}/evidence", changed)
    assert nate.get("/api/me/reports")[0]["unresolved_pins"] > 0


def test_challenges_are_split_by_who_is_waiting(host, agent, nate, published):
    claim(nate, admin_secret(host, agent, published))
    commenter = Api(host, make_link(host, agent, published, role="commenter"))
    ch = commenter.post(f"/api/reports/{published}/challenges", {"target": {"claim": "c1"}, "body": "Why?"},
                        expect=201)
    row = nate.get("/api/me/reports")[0]
    assert row["open_challenges"] == {"awaiting_me": 1, "awaiting_agent": 0}  # an admin answers it
    mine = nate.post(f"/api/reports/{published}/challenges", {"target": {"claim": "c2"}, "body": "And this?"},
                     expect=201)
    row = nate.get("/api/me/reports")[0]
    assert row["open_challenges"] == {"awaiting_me": 1, "awaiting_agent": 1}  # theirs waits on the agent
    agent.post(f"/api/reports/{published}/challenges/{mine['id']}/replies", {"body": "Because."}, expect=201)
    row = nate.get("/api/me/reports")[0]
    assert row["open_challenges"] == {"awaiting_me": 2, "awaiting_agent": 0}  # answered: the filer decides
    assert ch["id"]


def test_the_inbox_lists_what_needs_action(host, agent, other_agent, nate, published):
    claim(nate, admin_secret(host, agent, published))
    assert nate.get("/api/me/inbox") == []
    commenter = Api(host, make_link(host, agent, published, role="commenter"))
    ch = commenter.post(f"/api/reports/{published}/challenges", {"target": {"claim": "c1"}, "body": "Why?"},
                        expect=201)
    sug = other_agent.post(f"/api/reports/{published}/suggestions", {"ops": [HEADING], "message": "add heading"},
                           expect=201)
    agent.post(f"/api/reports/{published}/edits", {"ops": [HEADING]})
    inbox = nate.get("/api/me/inbox")
    kinds = {i["kind"]: i for i in inbox}
    assert set(kinds) == {"challenge", "suggestion", "publish"}
    assert kinds["challenge"]["id"] == ch["id"] and kinds["challenge"]["open"] == {"report": published,
                                                                                   "challenge": ch["id"]}
    assert kinds["suggestion"]["id"] == sug["id"] and kinds["suggestion"]["summary"] == "add heading"
    assert kinds["publish"]["report_id"] == published
    assert [i["at"] for i in inbox] == sorted((i["at"] for i in inbox), reverse=True)


def test_the_inbox_only_lists_what_the_role_can_act_on(host, agent, other_agent, nate, published):
    claim(nate, make_link(host, agent, published, role="suggester"))
    other_agent.post(f"/api/reports/{published}/suggestions", {"ops": [HEADING], "message": "add"}, expect=201)
    agent.post(f"/api/reports/{published}/edits", {"ops": [HEADING]})
    # A suggester sees the draft but cannot accept a suggestion or publish.
    assert nate.get("/api/me/inbox") == []
    assert nate.get("/api/me/reports")[0]["pending_suggestions"] == 1


def test_a_person_sees_only_their_own_reports(host, agent, nate, dana, published):
    claim(nate, admin_secret(host, agent, published))
    assert dana.get("/api/me/reports") == []
    assert dana.get("/api/reports") == []
    dana.get(f"/api/reports/{published}", expect=404)


# -- MCP ----------------------------------------------------------------------------------


def test_mcp_resolves_a_person_to_the_named_report(host, agent, nate, published):
    claim(nate, admin_secret(host, agent, published))
    from bevis.host.service import HostError

    p = host.service.authenticate(nate.secret, published)
    assert p.report_id == published and p.role == "admin" and p.person_id
    bare = host.service.authenticate(nate.secret, "rpt_unknown")
    assert bare.report_id is None
    with pytest.raises(HostError) as err:
        host.service.get_report(bare, "rpt_unknown")
    assert err.value.status == 404
