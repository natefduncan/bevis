import pytest

from bevis.bundle import BundleError


def problems(example):
    with pytest.raises(BundleError) as info:
        example.load()
    return "\n".join(info.value.problems)


def test_example_loads(example):
    b = example.load()
    assert b.title == "Revenue Review: 2024 vs 2025"
    assert len(b.evidence) == 5


def test_hash_mismatch(example):
    example.edit("evidence/q-rev-yoy.v1.json", lambda d: d["rows"][0].__setitem__(1, "4210001"))
    assert "data_hash does not match" in problems(example)


def test_row_count_mismatch(example):
    example.edit("evidence/q-rev-yoy.v1.json", lambda d: d.update(row_count=3))
    assert "row_count is 3 but 2 rows" in problems(example)


def test_duplicate_key(example):
    example.edit("evidence/q-rev-yoy.v1.json", lambda d: d["rows"].append([2024, "1"]))
    assert "key is not unique" in problems(example)


def test_bad_value_type(example):
    example.edit("evidence/q-rev-yoy.v1.json", lambda d: d["rows"][0].__setitem__(1, 4210000.0))
    assert "decimals are serialized as strings" in problems(example)


def test_missing_evidence_file(example):
    (example.root / "evidence/q-churn.v1.json").unlink()
    assert "q-churn.v1.json: missing" in problems(example)


def test_schema_error_has_location(example):
    example.edit("report.json", lambda r: r["claims"][0].update(status="green"))
    assert "report.json#/claims/0/status" in problems(example)


def test_unlisted_pin(example):
    example.edit("report.json", lambda r: r["claims"][0]["evidence"].append({"id": "q-nope", "version": 1}))
    assert "pins q-nope v1, which the bundle does not list" in problems(example)


def test_two_versions_of_one_id(example):
    def add_v2(m):
        m["evidence"].append({"id": "q-rev-yoy", "version": 2, "file": "evidence/q-rev-yoy.v2.json"})

    doc = example.read("evidence/q-rev-yoy.v1.json")
    doc["version"] = 2
    example.write("evidence/q-rev-yoy.v2.json", doc)
    example.edit("manifest.json", add_v2)
    example.edit("report.json", lambda r: r["claims"][0]["evidence"].append({"id": "q-rev-yoy", "version": 2}))
    assert "pins two versions of q-rev-yoy" in problems(example)


def test_disclosure_level_mismatch(example):
    example.edit("evidence/q-churn.v1.json", lambda d: d["disclosure"].update(level="cited"))
    assert "differs from the manifest's full" in problems(example)


@pytest.mark.parametrize(
    "data, message",
    [
        ({"values": [{"a": 1}]}, "must be {\"evidence\""),
        ({"url": "https://example.com/d.json"}, "url is not allowed"),
        ({"evidence": {"id": "q-churn", "version": 1}}, "which the block does not pin"),
        ({"evidence": {"id": ["x"], "version": 1}}, "must be {\"evidence\""),
        ({"evidence": {"id": {"a": 1}, "version": 1}}, "must be {\"evidence\""),
        ({"evidence": {"id": "q-churn", "version": [1]}}, "must be {\"evidence\""),
        ({"evidence": {"id": "q-churn", "version": "1"}}, "must be {\"evidence\""),
        ({"evidence": {"id": "q-churn", "version": True}}, "must be {\"evidence\""),
    ],
)
def test_chart_data_must_be_pinned_evidence(example, data, message):
    example.edit("report.json", lambda r: r["blocks"][4]["spec"].update(data=data))
    assert message in problems(example)


def test_chart_layer_data_is_checked(example):
    def layer(r):
        spec = r["blocks"][4]["spec"]
        spec["layer"] = [{"mark": "bar", "data": {"values": []}}]
    example.edit("report.json", layer)
    assert "spec.layer[0].data" in problems(example)


def test_chart_image_mark_rejected(example):
    example.edit("report.json", lambda r: r["blocks"][4]["spec"].update(mark={"type": "image"}))
    assert "image marks load external files" in problems(example)


def test_metadata_bundle_has_no_rows(example):
    example.restrict("metadata")
    b = example.load()
    assert all(r.rows is None for r in b.evidence.values())


def test_cited_bundle_loads(example):
    example.restrict("cited")
    b = example.load()
    top = b.record("q-top-accounts", 1)
    assert len(top.rows) == 3
    assert top.column_names == ["account_id", "change"]


def test_image_must_be_in_assets(example):
    example.edit("report.json", lambda r: r["blocks"].append(
        {"id": "b9", "type": "image", "src": "assets/missing.png", "alt": "x", "evidence": []}))
    assert "is not a file in assets/" in problems(example)


def test_withheld_columns_are_not_listed_or_keys(example):
    example.restrict("cited")
    name = example.read("evidence/q-top-accounts.v1.json")["columns"][1]["name"]
    example.edit("evidence/q-top-accounts.v1.json",
                 lambda d: d["disclosure"].update(withheld_columns=[name, d["key"][0]]))
    text = problems(example)
    assert f"withheld column {name!r} is also listed" in text
    assert "key column" in text and "cannot be withheld" in text


def _cited_with_cells(example):
    """The cited bundle with a withheld cell planted in a charted record."""
    example.restrict("cited")
    path = "evidence/q-monthly.v1.json"
    doc = example.read(path)
    assert "withheld_cells" not in doc["disclosure"]
    return path, doc


def test_withheld_cell_must_be_null_unique_and_not_a_key(example):
    path, doc = _cited_with_cells(example)
    name = next(c["name"] for c in doc["columns"] if c["name"] not in doc["key"])
    example.edit(path, lambda d: d["disclosure"].update(
        withheld_cells=[[0, name], [0, name], [1, doc["key"][0]]]))
    text = problems(example)
    assert "must be null" in text and "listed twice" in text
    assert "key column" in text


def test_withheld_cell_outside_the_rows(example):
    path, doc = _cited_with_cells(example)
    name = next(c["name"] for c in doc["columns"] if c["name"] not in doc["key"])
    example.edit(path, lambda d: d["disclosure"].update(withheld_cells=[[999, name]]))
    assert "is not a cell of the rows" in problems(example)


def test_withheld_cell_that_is_null_loads(example):
    path, doc = _cited_with_cells(example)
    name = next(c["name"] for c in doc["columns"] if c["name"] not in doc["key"])
    idx = [c["name"] for c in doc["columns"]].index(name)

    def plant(d):
        d["rows"][0][idx] = None
        d["disclosure"]["withheld_cells"] = [[0, name]]
    example.edit(path, plant)
    assert example.load().record("q-monthly", 1).is_withheld(
        tuple(example.load().record("q-monthly", 1).rows[0][i].canonical() for i in
              example.load().record("q-monthly", 1).key_indexes), name)


def test_nothing_is_withheld_at_full_disclosure_by_cell(example):
    name = example.read("evidence/q-churn.v1.json")["columns"][-1]["name"]
    example.edit("evidence/q-churn.v1.json", lambda d: d["disclosure"].update(withheld_cells=[[0, name]]))
    assert "nothing may be withheld at full disclosure" in problems(example)
