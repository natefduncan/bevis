"""Block revisions, the needs-review flag, and clearing it (SPEC §6.2)."""

import sqlite3

import pytest

from .conftest import Api, example_evidence
from .test_people import admin_secret, claim, dana, make_link, nate, published  # noqa: F401

B2 = "[Revenue fell 12.0%, from $4.21M to $3.71M]{#c1}. The decline was broad, but not even: one segment accounts for most of it."
B4 = ("[Mid-market revenue dropped $275K, 54% of the total decrease]{#c2}. In 2025, [Enterprise remained the largest "
      "segment, at 53% of revenue]{#c3}.\n\nLost accounts explain much of the gap: [142 accounts churned, taking "
      "$1.02M of prior-year revenue with them]{#c4}.")


def text(bid, body):
    return {"op": "upsert_block", "block": {"id": bid, "type": "text", "text": body}}


def state(api, rid):
    return api.get(f"/api/reports/{rid}")["block_state"]


@pytest.fixture
def editor(host, agent, nate, published):  # noqa: F811
    """A person who edits directly: the admin link, claimed."""
    claim(nate, admin_secret(host, agent, published))
    return nate


# -- revisions ----------------------------------------------------------------------------


def test_every_block_has_a_revision_that_counts_its_edits(agent, report):
    # The fixture built every block in one edit, and a revision counts edits, not changes.
    assert {v["revision"] for v in state(agent, report).values()} == {1}
    agent.post(f"/api/reports/{report}/edits", {"ops": [text("b2", B2 + " More.")]})
    assert state(agent, report)["b2"] == {"revision": 2, "needs_review": None}
    assert state(agent, report)["b4"]["revision"] == 1
    out = agent.post(f"/api/reports/{report}/edits", {"ops": [text("b2", B2)]})
    assert out["blocks"] == {"b2": {"revision": 3, "needs_review": False}}


def test_a_move_changes_no_block_but_a_claim_change_does(agent, report):
    agent.post(f"/api/reports/{report}/edits", {"ops": [{"op": "move_block", "id": "b2", "position": {"index": 4}}]})
    assert {v["revision"] for v in state(agent, report).values()} == {1}
    claim = next(c for c in agent.get(f"/api/reports/{report}")["claims"] if c["id"] == "c3")
    claim = {k: v for k, v in claim.items() if k != "status"}
    agent.post(f"/api/reports/{report}/edits", {"ops": [{"op": "link_claim", "claim": claim}]})
    assert state(agent, report)["b4"]["revision"] == 2


def test_a_deleted_block_leaves_the_state(agent, report):
    agent.post(f"/api/reports/{report}/edits", {"ops": [text("b2", B2 + "!")]})
    agent.post(f"/api/reports/{report}/edits", {"ops": [{"op": "delete_block", "id": "b2"}]})
    assert "b2" not in state(agent, report)


def test_blocks_read_from_get_report_go_back_unchanged(agent, report):
    blocks = agent.get(f"/api/reports/{report}")["blocks"]
    agent.post(f"/api/reports/{report}/edits", {"ops": [{"op": "upsert_block", "block": b} for b in blocks]})


def test_revisions_survive_a_publish_and_stay_out_of_it(agent, report):
    agent.post(f"/api/reports/{report}/edits", {"ops": [text("b2", B2 + " More.")]})
    agent.post(f"/api/reports/{report}/publish")
    assert state(agent, report)["b2"]["revision"] == 2
    version = agent.get(f"/api/reports/{report}", params={"version": 1})
    assert "block_state" not in version
    assert all(set(b) <= {"id", "type", "level", "text", "spec", "evidence", "caption"} for b in version["blocks"])
    row = sqlite3.connect(agent.client.service.settings.db_path).execute(
        "SELECT content FROM versions WHERE report_id = ?", (report,)).fetchone()[0]
    assert "meta" not in row and "needs_review" not in row


def test_new_evidence_keeps_the_state(agent, report):
    agent.post(f"/api/reports/{report}/edits", {"ops": [text("b2", B2 + " More.")]})
    changed = dict(next(e for e in example_evidence() if e["id"] == "q-rev-yoy"))
    changed["rows"] = [list(r) for r in changed["rows"]]
    changed["rows"][0][1] = "999"
    agent.post(f"/api/reports/{report}/evidence", changed)
    assert state(agent, report)["b2"]["revision"] == 2


# -- conflicts ----------------------------------------------------------------------------


def test_an_edit_conflicts_only_on_a_block_that_changed(agent, report):
    agent.post(f"/api/reports/{report}/edits", {"ops": [text("b4", B4 + " Later.")]})
    # b2 is untouched, so an edit based on revision 1 goes through even though the draft moved on.
    out = agent.post(f"/api/reports/{report}/edits",
                     {"ops": [text("b2", B2 + " More.")], "base_block_revisions": {"b2": 1}})
    assert out["blocks"]["b2"]["revision"] == 2
    r = agent.post(f"/api/reports/{report}/edits",
                   {"ops": [text("b4", B4 + " Mine.")], "base_block_revisions": {"b4": 1}}, expect=409)
    assert r["error"]["code"] == "block_conflict"
    (conflict,) = r["error"]["details"]["blocks"]
    assert conflict["id"] == "b4" and conflict["base_revision"] == 1 and conflict["revision"] == 2
    assert conflict["block"]["text"] == B4 + " Later."
    assert {c["id"] for c in conflict["claims"]} == {"c2", "c3", "c4"}
    # Nothing was applied.
    assert state(agent, report)["b4"]["revision"] == 2


def test_a_block_that_is_gone_conflicts(agent, report):
    agent.post(f"/api/reports/{report}/edits", {"ops": [{"op": "delete_block", "id": "b2"}]})
    r = agent.post(f"/api/reports/{report}/edits",
                   {"ops": [text("b2", B2)], "base_block_revisions": {"b2": 1}}, expect=409)
    assert r["error"]["details"]["blocks"][0]["block"] is None and r["error"]["details"]["blocks"][0]["revision"] is None


def test_malformed_base_block_revisions_are_422(agent, report):
    for bad in ([], {"b2": "0"}, {"b2": True}, {"b2": 1.5}):
        agent.post(f"/api/reports/{report}/edits", {"ops": [text("b2", B2)], "base_block_revisions": bad}, expect=422)


def test_validate_previews_a_conflict_too(agent, report):
    agent.post(f"/api/reports/{report}/edits", {"ops": [text("b2", B2 + " More.")]})
    r = agent.post(f"/api/reports/{report}/validate",
                   {"ops": [text("b2", B2)], "base_block_revisions": {"b2": 1}}, expect=409)
    assert r["error"]["code"] == "block_conflict"
    agent.post(f"/api/reports/{report}/validate", {"ops": [text("b2", B2)], "base_block_revisions": {"b2": 2}})


# -- the flag -----------------------------------------------------------------------------


def test_a_person_editing_a_claims_paragraph_flags_the_block(editor, published):
    out = editor.post(f"/api/reports/{published}/edits", {"ops": [text("b2", B2.replace("broad", "wide"))]})
    assert out["blocks"]["b2"] == {"revision": 2, "needs_review": True}
    flag = state(editor, published)["b2"]["needs_review"]
    assert flag["edited_by"]["name"] == "nate" and flag["edited_by"]["person_id"] and flag["revision"] == 2
    assert flag["edited_at"]
    log = [e["action"] for e in editor.get(f"/api/reports/{published}/activity")]
    assert "flag_review" in log


def test_prose_away_from_a_claim_flags_nothing(editor, published):
    editor.post(f"/api/reports/{published}/edits", {"ops": [text("b4", B4 + "\n\nA closing note.")]})
    assert state(editor, published)["b4"] == {"revision": 2, "needs_review": None}
    editor.post(f"/api/reports/{published}/edits", {"ops": [text("b4", B4 + "\n\nA different closing note.")]})
    assert state(editor, published)["b4"] == {"revision": 3, "needs_review": None}


def test_creating_a_claim_flags_the_block(editor, published):
    claim_ = {"id": "c9", "block_id": "b2", "evidence": [{"id": "q-rev-yoy", "version": 1}], "numbers": [
        {"text": "12.0%", "tier": "computed",
         "expression": "pct_change(cell(q-rev-yoy, 2024, revenue), cell(q-rev-yoy, 2025, revenue))"}]}
    editor.post(f"/api/reports/{published}/edits", {"ops": [
        text("b2", B2 + " Again: [revenue fell 12.0%]{#c9}."), {"op": "link_claim", "claim": claim_}]})
    assert state(editor, published)["b2"]["needs_review"]


def test_unlinking_a_claim_flags_the_block(editor, published):
    editor.post(f"/api/reports/{published}/edits", {"ops": [{"op": "unlink_claim", "id": "c3"}]})
    assert state(editor, published)["b4"]["needs_review"]


def test_an_agents_edit_never_flags(agent, report):
    agent.post(f"/api/reports/{report}/edits", {"ops": [text("b2", B2.replace("broad", "wide"))]})
    assert state(agent, report)["b2"] == {"revision": 2, "needs_review": None}


def test_an_agent_editing_a_flagged_block_leaves_the_flag(agent, editor, published):
    editor.post(f"/api/reports/{published}/edits", {"ops": [text("b2", B2.replace("broad", "wide"))]})
    agent.post(f"/api/reports/{published}/edits", {"ops": [text("b2", B2.replace("broad", "very wide"))]})
    got = state(agent, published)["b2"]
    assert got["revision"] == 3 and got["needs_review"]["edited_by"]["name"] == "nate"


def test_a_later_person_edit_near_the_claim_takes_the_flag_over(host, agent, editor, dana, published):  # noqa: F811
    editor.post(f"/api/reports/{published}/edits", {"ops": [text("b2", B2.replace("broad", "wide"))]})
    admin = Api(host, admin_secret(host, agent, published))
    admin.post(f"/api/reports/{published}/links", {"name": "for dana", "role": "editor", "person": "dana"}, expect=201)
    dana.post(f"/api/reports/{published}/edits", {"ops": [text("b2", B2.replace("broad", "narrow"))]})
    flag = state(editor, published)["b2"]["needs_review"]
    assert flag["edited_by"]["name"] == "dana" and flag["revision"] == 3
    # An edit elsewhere in the block keeps whoever's flag it is.
    editor.post(f"/api/reports/{published}/edits", {"ops": [text("b4", B4 + "\n\nNote.")]})
    assert state(editor, published)["b2"]["needs_review"]["edited_by"]["name"] == "dana"


def test_accepting_a_suggestion_near_a_claim_flags_it_for_a_person(host, agent, other_agent, editor, published):  # noqa: F811
    sug = other_agent.post(f"/api/reports/{published}/suggestions",
                           {"ops": [text("b2", B2.replace("broad", "wide"))], "message": "reword"}, expect=201)
    editor.post(f"/api/reports/{published}/suggestions/{sug['id']}/accept")
    flag = state(editor, published)["b2"]["needs_review"]
    assert flag["edited_by"]["name"] == "nate"  # the accepting editor, as if they had made the edit

    sug = other_agent.post(f"/api/reports/{published}/suggestions",
                           {"ops": [text("b4", B4.replace("dropped", "fell"))], "message": "reword"}, expect=201)
    agent.post(f"/api/reports/{published}/suggestions/{sug['id']}/accept")
    assert state(agent, published)["b4"]["needs_review"] is None  # an agent accepting is not a person's edit


def test_a_suggester_creates_no_flag_by_suggesting(host, agent, nate, published):  # noqa: F811
    claim(nate, make_link(host, agent, published, role="suggester"))
    nate.post(f"/api/reports/{published}/suggestions",
              {"ops": [text("b2", B2.replace("broad", "wide"))], "message": "reword"}, expect=201)
    assert state(agent, published)["b2"]["needs_review"] is None


# -- publishing and clearing --------------------------------------------------------------


def test_publish_is_refused_while_a_block_needs_review(editor, published):
    editor.post(f"/api/reports/{published}/edits", {"ops": [text("b2", B2.replace("broad", "wide"))]})
    r = editor.post(f"/api/reports/{published}/publish", expect=409)
    assert r["error"]["code"] == "needs_review"
    (b,) = r["error"]["details"]["blocks"]
    assert b["id"] == "b2" and b["edited_by"]["name"] == "nate" and b["edited_at"]
    # An override reason does not get past it.
    editor.post(f"/api/reports/{published}/publish", {"override_reason": "ship it"}, expect=409)


def test_an_admin_clears_a_flag_and_publish_goes_through(host, agent, editor, published):
    editor.post(f"/api/reports/{published}/edits", {"ops": [text("b2", B2.replace("broad", "wide"))]})
    out = editor.post(f"/api/reports/{published}/blocks/b2/clear_review", {"revision": 2})
    assert out == {"block_id": "b2", "revision": 2, "cleared": True, "self_cleared": True}
    assert state(editor, published)["b2"] == {"revision": 2, "needs_review": None}
    assert editor.post(f"/api/reports/{published}/publish")["version"] == 2
    entry = [e for e in editor.get(f"/api/reports/{published}/activity") if e["action"] == "clear_review"][0]
    assert entry["after"] == {"block_id": "b2", "revision": 2, "self_cleared": True}
    assert entry["before"]["edited_by"]["name"] == "nate"


def test_clearing_someone_elses_edit_is_not_a_self_clear(host, agent, editor, dana, published):  # noqa: F811
    editor.post(f"/api/reports/{published}/edits", {"ops": [text("b2", B2.replace("broad", "wide"))]})
    admin = Api(host, admin_secret(host, agent, published))
    admin.post(f"/api/reports/{published}/links", {"name": "for dana", "role": "admin", "person": "dana"}, expect=201)
    out = dana.post(f"/api/reports/{published}/blocks/b2/clear_review", {"revision": 2})
    assert out["self_cleared"] is False


def test_only_an_admin_clears(host, agent, editor, nate, published):  # noqa: F811
    editor.post(f"/api/reports/{published}/edits", {"ops": [text("b2", B2.replace("broad", "wide"))]})
    other = Api(host, make_link(host, agent, published, role="editor"))
    other.post(f"/api/reports/{published}/blocks/b2/clear_review", {"revision": 2}, expect=403)
    Api(host, make_link(host, agent, published, role="commenter")).post(
        f"/api/reports/{published}/blocks/b2/clear_review", {"revision": 2}, expect=403)


def test_a_clear_against_an_old_revision_is_refused_with_the_new_block(host, agent, editor, published):  # noqa: F811
    editor.post(f"/api/reports/{published}/edits", {"ops": [text("b2", B2.replace("broad", "wide"))]})
    agent.post(f"/api/reports/{published}/edits", {"ops": [text("b2", B2.replace("broad", "wide, and deep"))]})
    r = editor.post(f"/api/reports/{published}/blocks/b2/clear_review", {"revision": 2}, expect=409)
    assert r["error"]["code"] == "stale_block"
    assert "wide, and deep" in r["error"]["details"]["block"]["text"] and r["error"]["details"]["revision"] == 3
    assert editor.post(f"/api/reports/{published}/blocks/b2/clear_review", {"revision": 3})["cleared"]


def test_clearing_what_is_not_flagged_or_not_there(editor, published):
    r = editor.post(f"/api/reports/{published}/blocks/b2/clear_review", {"revision": 1}, expect=409)
    assert r["error"]["code"] == "not_flagged"
    editor.post(f"/api/reports/{published}/blocks/nope/clear_review", {"revision": 1}, expect=404)
    editor.post(f"/api/reports/{published}/blocks/b2/clear_review", {"revision": "0"}, expect=422)


def test_clearing_keeps_the_report_revision(editor, published):
    editor.post(f"/api/reports/{published}/edits", {"ops": [text("b2", B2.replace("broad", "wide"))]})
    before = editor.get(f"/api/reports/{published}")["revision"]
    editor.post(f"/api/reports/{published}/blocks/b2/clear_review", {"revision": 2})
    assert editor.get(f"/api/reports/{published}")["revision"] == before


def test_an_unflagged_report_publishes_as_before(agent, report):
    assert agent.post(f"/api/reports/{report}/publish")["version"] == 1


def test_two_blocks_flagged_are_listed_in_report_order(editor, published):
    editor.post(f"/api/reports/{published}/edits", {"ops": [
        text("b4", B4.replace("dropped", "fell")), text("b2", B2.replace("broad", "wide"))]})
    r = editor.post(f"/api/reports/{published}/publish", expect=409)
    assert [b["id"] for b in r["error"]["details"]["blocks"]] == ["b2", "b4"]


# -- the index and the inbox --------------------------------------------------------------


def test_the_index_counts_flags_and_the_inbox_shows_them_to_admins(host, agent, editor, published):  # noqa: F811
    editor.post(f"/api/reports/{published}/edits", {"ops": [text("b2", B2.replace("broad", "wide"))]})
    row = editor.get("/api/me/reports")[0]
    assert row["needs_review"] == 1 and row["unpublished_changes"] is True
    inbox = editor.get("/api/me/inbox")
    review = [i for i in inbox if i["kind"] == "review"]
    assert [(i["id"], i["open"]) for i in review] == [("b2", {"report": published, "block": "b2"})]
    assert review[0]["author"]["name"] == "nate"
    assert not [i for i in inbox if i["kind"] == "publish"]  # not ready while a block needs review
    editor.post(f"/api/reports/{published}/blocks/b2/clear_review", {"revision": 2})
    assert [i["kind"] for i in editor.get("/api/me/inbox")] == ["publish"]


def test_an_editor_who_cannot_clear_is_not_shown_the_item(host, agent, nate, published):  # noqa: F811
    claim(nate, make_link(host, agent, published, role="editor"))
    nate.post(f"/api/reports/{published}/edits", {"ops": [text("b2", B2.replace("broad", "wide"))]})
    assert nate.get("/api/me/reports")[0]["needs_review"] == 1
    assert [i for i in nate.get("/api/me/inbox") if i["kind"] == "review"] == []


# -- suggestions carry block revisions too ------------------------------------------------


def test_a_suggestion_conflicts_only_on_a_block_that_changed(host, agent, nate, published):  # noqa: F811
    claim(nate, make_link(host, agent, published, role="suggester"))
    agent.post(f"/api/reports/{published}/edits", {"ops": [text("b4", B4 + " Later.")]})
    ok = nate.post(f"/api/reports/{published}/suggestions",
                   {"ops": [text("b2", B2 + " More.")], "message": "m", "base_block_revisions": {"b2": 1}}, expect=201)
    assert ok["status"] == "open"
    r = nate.post(f"/api/reports/{published}/suggestions",
                  {"ops": [text("b4", B4 + " Mine.")], "base_block_revisions": {"b4": 1}}, expect=409)
    assert r["error"]["code"] == "block_conflict" and r["error"]["details"]["blocks"][0]["revision"] == 2
    assert [s["id"] for s in agent.get(f"/api/reports/{published}/suggestions")] == [ok["id"]]  # no second one stored
    nate.post(f"/api/reports/{published}/suggestions",
              {"ops": [text("b2", B2)], "base_block_revisions": {"b2": "1"}}, expect=422)
