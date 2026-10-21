import json
from decimal import Decimal
from pathlib import Path

import pytest

from bevis import numbers

SPEC = json.loads((Path(__file__).parents[1] / "spec/expr/matching.json").read_text())


@pytest.mark.parametrize("case", SPEC["tokens"], ids=[c["text"] for c in SPEC["tokens"]])
def test_tokens(case):
    assert [d.text for d in numbers.tokens(case["text"])] == case["tokens"]


@pytest.mark.parametrize("case", SPEC["cases"], ids=[f"{c['display']} vs {c['value']}" for c in SPEC["cases"]])
def test_matching(case):
    shown = numbers.parse(case["display"])
    if case["match"] is None:
        assert shown is None
    else:
        assert numbers.matches(shown, Decimal(case["value"])) is case["match"]


def test_parsed_fields():
    d = numbers.parse("-$1,234.50K")
    assert (d.sign, d.currency, d.magnitude, d.decimals, d.exponent, d.percent) == (
        "-", True, Decimal("1234.50"), 2, 3, False)
    assert numbers.parse("−12%").sign == "-"


def test_written_numbers():
    assert numbers.written_numbers("Three accounts, one-third of them, and twelve percent") == [
        "Three", "one", "twelve"]


def test_direction_words():
    assert numbers.direction_words("Revenue Fell while costs rose and grew") == (["Fell"], ["rose", "grew"])
