import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from bevis.client import BevisError, Client, encode, infer_type, to_evidence


@pytest.fixture
def client(host):
    key = host.service.create_agent_key("py-client")["secret"]
    return Client("http://testserver", key, http=host)


@pytest.mark.parametrize(
    "values, type_",
    [
        ([1, 2, None], "integer"),
        ([1, Decimal("2.5")], "decimal"),
        ([1, 2.5], "decimal"),
        ([True, None], "boolean"),
        ([date(2025, 1, 1)], "date"),
        ([datetime(2025, 1, 1, 9, 30)], "datetime"),
        (["a"], "string"),
        ([None, None], "null"),
    ],
)
def test_infer_type(values, type_):
    assert infer_type(values) == type_


def test_infer_type_rejects_mixtures():
    with pytest.raises(TypeError, match="mixes"):
        infer_type([1, "a"])


@pytest.mark.parametrize(
    "value, type_, encoded",
    [
        (Decimal("4210000.00"), "decimal", "4210000"),
        (0.1, "decimal", "0.1"),
        (1e-7, "decimal", "0.0000001"),
        (3705000.0, "decimal", "3705000"),
        (7, "decimal", "7"),
        ("12.50", "decimal", "12.5"),
        (2024, "integer", 2024),
        (datetime(2025, 3, 1, 1, 0, tzinfo=timezone.utc), "datetime", "2025-03-01T01:00:00+00:00"),
        (date(2025, 3, 1), "date", "2025-03-01"),
    ],
)
def test_encode(value, type_, encoded):
    assert encode(value, type_) == encoded


def test_encode_rejects_nan():
    with pytest.raises(ValueError):
        encode(float("nan"), "decimal")


def test_run_query_end_to_end(client):
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE orders (year INTEGER, amount REAL, refunded INTEGER);
        INSERT INTO orders VALUES (2024, 4000000, 0), (2024, 210000, 0), (2024, 5, 1),
                                  (2025, 3705000, 0);
    """)
    rid = client.create_report("From SQLite")["id"]
    sql = "SELECT year, SUM(amount) AS revenue FROM orders WHERE refunded = ? GROUP BY year ORDER BY year"
    out = client.run_query(rid, "q-rev-yoy", db, sql, params=[0], key=["year"], source="sqlite:orders",
                           assumptions=["refunds excluded"])
    assert (out["outcome"], out["version"], out["row_count"]) == ("created", 1, 2)
    again = client.run_query(rid, "q-rev-yoy", db, sql, params=[0], key=["year"], source="sqlite:orders")
    assert again["outcome"] == "same_result"

    ev = client._call("GET", f"/api/reports/{rid}/evidence/q-rev-yoy")["evidence"]
    assert ev["columns"] == [{"name": "year", "type": "integer"}, {"name": "revenue", "type": "decimal"}]
    assert ev["rows"] == [[2024, "4210000"], [2025, "3705000"]]
    assert ev["params"] == {"args": [0]}

    out = client.edit(rid, [
        {"op": "upsert_block", "block": {"type": "text", "text": "[Revenue fell 12.0%]{#c1}."}},
        {"op": "link_claim", "claim": {"id": "c1", "block_id": "b1", "evidence": [{"id": "q-rev-yoy", "version": 1}],
                                       "numbers": [{"text": "12.0%", "tier": "computed", "expression":
                                                    "pct_change(cell(q-rev-yoy, 2024, revenue), "
                                                    "cell(q-rev-yoy, 2025, revenue))"}]}}])
    assert out["validation"]["counts"]["errors"] == 0
    assert client.publish(rid)["version"] == 1


def test_errors_are_raised(client):
    with pytest.raises(BevisError) as info:
        client.get_report("rpt_missing")
    assert (info.value.status, info.value.code) == (404, "not_found")


def test_types_override(client):
    rid = client.create_report("Override")["id"]
    out = client.add_rows(rid, "q-text-money", ["k", "amount"], [["a", "12.30"]], types={"amount": "decimal"},
                          key=["k"], sql="SELECT ...", source="x")
    assert out["outcome"] == "created"
    assert to_evidence(["k", "amount"], [["a", "12.30"]], {"amount": "decimal"})[1] == [["a", "12.3"]]
