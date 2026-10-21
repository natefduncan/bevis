import pytest

from bevis.host import ops


def draft_with(*ids):
    d = ops.empty_draft("T")
    d["blocks"] = [{"id": i, "type": "text", "text": i} for i in ids]
    return d


@pytest.mark.parametrize(
    "position, order",
    [
        (None, ["b1", "b2", "b3"]),
        ({"after": "b1"}, ["b1", "b3", "b2"]),
        ({"before": "b1"}, ["b3", "b1", "b2"]),
        ({"index": 1}, ["b1", "b3", "b2"]),
    ],
)
def test_insert_positions(position, order):
    op = {"op": "upsert_block", "block": {"type": "text", "text": "new"}}
    if position:
        op["position"] = position
    out = ops.apply(draft_with("b1", "b2"), [op])
    assert [b["id"] for b in out.draft["blocks"]] == order


def test_update_keeps_position_unless_moved():
    out = ops.apply(draft_with("b1", "b2", "b3"), [{"op": "upsert_block", "block": {"id": "b2", "type": "text",
                                                                                     "text": "x"}}])
    assert [b["id"] for b in out.draft["blocks"]] == ["b1", "b2", "b3"]
    assert out.changes[0].before["text"] == "b2" and out.changes[0].after["text"] == "x"


def test_ops_are_atomic():
    d = draft_with("b1")
    with pytest.raises(ops.OpError) as info:
        ops.apply(d, [{"op": "set_title", "title": "New"}, {"op": "delete_block", "id": "nope"}])
    assert info.value.index == 1
    assert d["title"] == "T"  # the input is never changed


def test_repin_updates_claims_cells_and_chart_specs():
    d = draft_with("b1")
    pin = {"id": "q-a", "version": 1}
    d["blocks"].append({"id": "b2", "type": "chart", "evidence": [dict(pin)],
                        "spec": {"layer": [{"data": {"evidence": dict(pin)}, "mark": "bar"}]}})
    d["claims"].append({"id": "c1", "block_id": "b1", "evidence": [dict(pin)], "status": "ok", "numbers": [
        {"text": "5", "tier": "direct", "status": "ok",
         "cell": {"evidence": "q-a", "version": 1, "key": [], "column": "x"}}]})
    out = ops.apply(d, [{"op": "repin", "evidence_id": "q-a", "version": 3}])
    chart, claim = out.draft["blocks"][1], out.draft["claims"][0]
    assert chart["evidence"] == [{"id": "q-a", "version": 3}]
    assert chart["spec"]["layer"][0]["data"]["evidence"]["version"] == 3
    assert claim["evidence"][0]["version"] == 3 and claim["numbers"][0]["cell"]["version"] == 3
    assert [c.action for c in out.changes] == ["repin_claim", "repin_block"]


def test_repin_can_target_one_claim():
    d = draft_with("b1")
    for cid in ("c1", "c2"):
        d["claims"].append({"id": cid, "block_id": "b1", "evidence": [{"id": "q-a", "version": 1}],
                            "numbers": [], "status": "ok"})
    out = ops.apply(d, [{"op": "repin", "evidence_id": "q-a", "version": 2, "claims": ["c2"]}])
    assert [c["evidence"][0]["version"] for c in out.draft["claims"]] == [1, 2]


def test_link_claim_resets_statuses():
    d = draft_with("b1")
    out = ops.apply(d, [{"op": "link_claim", "claim": {
        "id": "c1", "block_id": "b1", "evidence": [], "status": "ok",
        "numbers": [{"text": "1", "tier": "computed", "expression": "1", "value": "999", "status": "ok"}]}}])
    assert out.draft["claims"][0]["numbers"][0]["value"] is None


def test_next_ids():
    d = draft_with("b1", "b3")
    d["claims"] = [{"id": "c2"}, {"id": "c9"}]
    assert ops.next_block_id(d) == "b4" and ops.next_claim_id(d) == "c10"
