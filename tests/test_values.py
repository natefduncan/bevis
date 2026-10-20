from datetime import date
from decimal import Decimal

import pytest

from bevis import values
from bevis.values import BadValue, Value


@pytest.mark.parametrize(
    "type_, raw, canonical",
    [
        ("integer", 2024, "2024"),
        ("integer", -7, "-7"),
        ("decimal", "4210000.00", "4210000"),
        ("decimal", "-0.0", "0"),
        ("decimal", "0.050", "0.05"),
        ("decimal", "-12.5", "-12.5"),
        ("date", "2025-03-01", "2025-03-01"),
        ("datetime", "2025-03-01T01:00:00.500+01:00", "2025-03-01T00:00:00.5Z"),
        ("datetime", "2025-03-01T00:00:00Z", "2025-03-01T00:00:00Z"),
        ("datetime", "2025-03-01T09:30:00", "2025-03-01T09:30:00"),
        ("boolean", True, "true"),
        ("string", "Mid-market", "Mid-market"),
        ("string", None, None),
    ],
)
def test_canonical(type_, raw, canonical):
    assert values.from_json(type_, raw).canonical() == canonical


@pytest.mark.parametrize(
    "type_, raw",
    [
        ("integer", "2024"),
        ("integer", 2024.0),
        ("integer", True),
        ("decimal", 1.5),
        ("decimal", "1e5"),
        ("decimal", "1,000"),
        ("decimal", ".5"),
        ("date", "2025-02-30"),
        ("date", "2025-3-1"),
        ("datetime", "2025-03-01"),
        ("boolean", "true"),
        ("string", 5),
        ("null", 0),
    ],
)
def test_rejects(type_, raw):
    with pytest.raises(BadValue):
        values.from_json(type_, raw)


def test_hash_matches_spec_example():
    # The worked example in spec/bundle/README.md.
    import hashlib

    text = '[[["year","integer"],["revenue","decimal"]],[["2024","4210000"],["2025","3705000"]]]'
    expected = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
    columns = [("year", "integer"), ("revenue", "decimal")]
    rows = [
        [Value("integer", Decimal(2025)), Value("decimal", Decimal("3705000"))],
        [Value("integer", Decimal(2024)), Value("decimal", Decimal("4210000.00"))],
    ]
    assert values.data_hash(columns, rows, [0]) == expected


def test_hash_sorts_bytewise_and_nulls_first():
    columns = [("k", "string")]
    rows = [[Value("string", "b")], [Value("string", None)], [Value("string", "a")]]
    import hashlib

    text = '[[["k","string"]],[[null],["a"],["b"]]]'
    assert values.data_hash(columns, rows, [0]) == "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def test_hash_ignores_row_order_and_number_formatting():
    columns = [("d", "date"), ("v", "decimal")]
    a = [[Value("date", date(2025, 1, 1)), Value("decimal", Decimal("1.50"))],
         [Value("date", date(2025, 1, 2)), Value("decimal", Decimal("2"))]]
    b = [list(reversed(a))[0], list(reversed(a))[1]]
    b[1] = [Value("date", date(2025, 1, 1)), Value("decimal", Decimal("1.5"))]
    assert values.data_hash(columns, a, [0]) == values.data_hash(columns, b, [0])


def test_hash_keeps_non_ascii_as_utf8():
    import hashlib

    rows = [[Value("string", "Zürich")]]
    text = '[[["city","string"]],[["Zürich"]]]'
    assert values.data_hash([("city", "string")], rows, [0]) == "sha256:" + hashlib.sha256(text.encode()).hexdigest()
