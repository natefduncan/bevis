import json
from pathlib import Path

import pytest

from bevis import expr
from bevis.evidence import Record
from bevis.values import format_decimal

SPEC = json.loads((Path(__file__).parents[1] / "spec/expr/cases.json").read_text())
RECORDS = {
    eid: Record.from_json({"id": eid, "version": 1, **doc}) for eid, doc in SPEC["evidence"].items()
}


@pytest.mark.parametrize("case", SPEC["cases"], ids=[c["name"] for c in SPEC["cases"]])
def test_shared_case(case):
    expect = case["expect"]
    if expect.get("error") == "parse":
        with pytest.raises(expr.ParseError):
            expr.parse(case["expr"])
        return
    node = expr.parse(case["expr"])
    resolve = expr.resolver(RECORDS)
    if "error" in expect:
        with pytest.raises(expr.EvalError) as info:
            expr.evaluate(node, resolve)
        assert info.value.code == expect["error"]
    else:
        assert format_decimal(expr.evaluate(node, resolve)) == expect["value"]


@pytest.mark.parametrize(
    "text, change",
    [
        ("pct_change(1, 2)", True),
        ("2 - 1", True),
        ("(2 - 1)", True),
        ("((2 - 1))", True),
        ("(2 - 1) / 2", False),
        ("share(2 - 1, 3)", False),
        ("-2", False),
        ("1 + 2 - 3", True),
        ("1 - 2 + 3", False),
        ("sum(1, 2)", False),
    ],
)
def test_is_change(text, change):
    assert expr.is_change(expr.parse(text)) is change


def test_cells_in_source_order():
    node = expr.parse('pct_change(cell(q-a, 1, x), cell(q-b, "k", "y z"))')
    assert [(c.evidence, c.column) for c in expr.cells(node)] == [("q-a", "x"), ("q-b", "y z")]


def test_slug_hyphens_are_not_minus():
    node = expr.parse("cell(q-rev-yoy, 2024, revenue)-1")
    assert isinstance(node, expr.BinOp) and node.op == "-"
    assert node.left.evidence == "q-rev-yoy"
