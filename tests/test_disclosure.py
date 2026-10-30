import copy

import pytest

from bevis.disclosure import restrict
from bevis.evidence import LookupFailed, Record

DOC = {
    "id": "r", "version": 1, "key": ["k"],
    "columns": [{"name": "k", "type": "string"}, {"name": "a", "type": "integer"}, {"name": "b", "type": "integer"}],
    "rows": [["x", 1, 10], ["y", 2, 20], ["z", 3, 30]],
    "row_count": 3, "disclosure": {"level": "full", "withheld_rows": 0},
}


def report(chart: bool):
    claim = {"id": "c1", "evidence": [{"id": "r", "version": 1}], "numbers": [
        {"tier": "direct", "cell": {"evidence": "r", "version": 1, "key": ["y"], "column": "b"}}]}
    blocks = [{"type": "chart", "id": "b1", "evidence": [{"id": "r", "version": 1}],
               "spec": {"mark": "bar", "encoding": {"x": {"field": "a"}}}}] if chart else []
    return {"claims": [claim], "blocks": blocks}


def cited(chart):
    return restrict(report(chart), {("r", 1): DOC}, "cited")[("r", 1)]


def test_a_charted_record_withholds_a_claim_column_in_the_rows_it_does_not_cite():
    doc = cited(chart=True)
    assert doc["rows"] == [["x", 1, None], ["y", 2, 20], ["z", 3, None]]
    assert doc["disclosure"]["withheld_cells"] == [[0, "b"], [2, "b"]]
    assert doc["disclosure"]["withheld_rows"] == 0


def test_an_uncharted_record_keeps_only_cited_rows_and_has_no_withheld_cells():
    doc = cited(chart=False)
    assert doc["rows"] == [["y", 20]]
    assert "withheld_cells" not in doc["disclosure"]


def test_a_withheld_cell_reads_as_withheld_not_null():
    record = Record.from_json(cited(chart=True))
    assert record.cell(("y",), "b").canonical() == "20"
    with pytest.raises(LookupFailed) as e:
        record.cell(("x",), "b")
    assert e.value.code == "withheld"
    assert record.is_withheld(("x",), "b") and not record.is_withheld(("y",), "b")
    assert record.cell(("x",), "a").canonical() == "1"


@pytest.mark.parametrize("cell", [[5, "b"], [0, "nope"]])
def test_withheld_cells_must_point_into_the_rows(cell):
    from bevis.evidence import EvidenceError
    doc = copy.deepcopy(cited(chart=True))
    doc["disclosure"]["withheld_cells"].append(cell)
    with pytest.raises(EvidenceError):
        Record.from_json(doc)
