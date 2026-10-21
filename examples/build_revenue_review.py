"""Write the example bundle in examples/revenue-review/.

This plays the part of an agent: it holds query results, writes claims
over them, and lets bevis compute the hashes. Statuses, computed values,
and the manifest's validation counts are filled in by the validator, as
the host does at publish. Run it after changing the example or the rules:

    uv run python examples/build_revenue_review.py
"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from bevis import bundle
from bevis.evidence import Record
from bevis.validate import Subject, validate

OUT = Path(__file__).parent / "revenue-review"
RUN_AT = "2026-10-20T14:01:47Z"


def evidence(id, *, source, sql, key, columns, rows, assumptions, params=None, **extra):
    doc = {
        "id": id,
        "version": 1,
        "source": source,
        "sql": sql,
        "params": params or {},
        "run_at": RUN_AT,
        "provenance": "agent-reported",
        "key": key,
        "columns": [{"name": n, "type": t} for n, t in columns],
        "rows": rows,
        "row_count": len(rows),
        "data_hash": "",
        "assumptions": assumptions,
        **extra,
        "disclosure": {"level": "full", "withheld_rows": 0},
    }
    doc["data_hash"] = Record.from_json(doc).compute_hash()
    return doc


# -- query results ------------------------------------------------------------

rev_yoy = evidence(
    "q-rev-yoy",
    source="warehouse.orders",
    sql=(
        "SELECT year, SUM(amount) AS revenue FROM orders "
        "WHERE refunded = false AND year IN (2024, 2025) GROUP BY year ORDER BY year;"
    ),
    key=["year"],
    columns=[("year", "integer"), ("revenue", "decimal")],
    rows=[[2024, "4210000"], [2025, "3705000"]],
    assumptions=["revenue = SUM(amount), refunds excluded"],
)

seg_delta = evidence(
    "q-seg-delta",
    source="warehouse.orders",
    sql=(
        "SELECT a.segment, o.year, SUM(o.amount) AS revenue, COUNT(DISTINCT o.account_id) AS customers\n"
        "FROM orders o JOIN accounts a USING (account_id)\n"
        "WHERE o.refunded = false AND o.year IN (2024, 2025)\n"
        "GROUP BY a.segment, o.year\n"
        "ORDER BY a.segment, o.year;"
    ),
    key=["segment", "year"],
    columns=[("segment", "string"), ("year", "integer"), ("revenue", "decimal"), ("customers", "integer")],
    rows=[
        ["Enterprise", 2024, "2100000", 61],
        ["Enterprise", 2025, "1950000", 58],
        ["Mid-market", 2024, "1460000", 412],
        ["Mid-market", 2025, "1185000", 366],
        ["SMB", 2024, "650000", 1903],
        ["SMB", 2025, "570000", 1821],
    ],
    assumptions=[
        "segment is the account's segment at year end",
        "revenue = SUM(amount), refunds excluded",
    ],
)

# Monthly revenue whose months sum to the yearly totals above.
_shape = [0.074, 0.071, 0.083, 0.080, 0.085, 0.082, 0.079, 0.081, 0.088, 0.090, 0.087, 0.100]


def _months(year, total):
    rows, running = [], Decimal(0)
    for m, w in enumerate(_shape, start=1):
        amount = Decimal(total) - running if m == 12 else (Decimal(total) * Decimal(str(w))).quantize(Decimal(1))
        running += amount
        rows.append([date(year, m, 1).isoformat(), str(amount)])
    return rows


monthly = evidence(
    "q-monthly",
    source="warehouse.orders",
    sql=(
        "SELECT date_trunc('month', ordered_at)::date AS month, SUM(amount) AS revenue\n"
        "FROM orders WHERE refunded = false AND year IN (2024, 2025)\n"
        "GROUP BY 1 ORDER BY 1;"
    ),
    key=["month"],
    columns=[("month", "date"), ("revenue", "decimal")],
    rows=_months(2024, 4210000) + _months(2025, 3705000),
    assumptions=["months are calendar months in UTC"],
)

_first = ["Northwind", "Blue Heron", "Cedar", "Halcyon", "Ironbark", "Juniper", "Kestrel", "Lumen",
          "Meridian", "Nimbus", "Orchard", "Pinecrest", "Quarry", "Redwood", "Saltmarsh", "Tidewater",
          "Upland", "Vantage", "Westfield", "Yarrow"]
_second = ["Traders", "Supply", "Logistics"]
_declines = [84210, 62400, 54800, 41250, 38900, 33120, 30480, 28750, 26010, 24400]
_declines += [22000 - 650 * i for i in range(30)]
_accounts = []
for i, decline in enumerate(_declines):
    name = f"{_first[i % 20]} {_second[i % 3]}" if i else "Northwind Traders"
    prior = 120000 + (i * 7919) % 90000
    _accounts.append([
        f"A-{1042 + i * 37}",
        name,
        ["Mid-market", "Enterprise", "SMB"][i % 3] if i else "Mid-market",
        str(prior),
        str(prior - decline),
        str(-decline),
    ])

top_accounts = evidence(
    "q-top-accounts",
    source="warehouse.orders",
    sql=(
        "WITH by_account AS (\n"
        "  SELECT account_id,\n"
        "         SUM(amount) FILTER (WHERE year = 2024) AS rev_2024,\n"
        "         SUM(amount) FILTER (WHERE year = 2025) AS rev_2025\n"
        "  FROM orders WHERE refunded = false GROUP BY account_id\n"
        ")\n"
        "SELECT b.account_id, a.name AS account_name, a.segment, b.rev_2024, b.rev_2025,\n"
        "       b.rev_2025 - b.rev_2024 AS change\n"
        "FROM by_account b JOIN accounts a USING (account_id)\n"
        "WHERE b.rev_2024 > 0\n"
        "ORDER BY change ASC\n"
        "LIMIT :n;"
    ),
    params={"n": 40},
    key=["account_id"],
    columns=[
        ("account_id", "string"),
        ("account_name", "string"),
        ("segment", "string"),
        ("rev_2024", "decimal"),
        ("rev_2025", "decimal"),
        ("change", "decimal"),
    ],
    rows=_accounts,
    assumptions=[
        "accounts with no 2024 revenue are excluded (new logos)",
        "top 40 by decline; the full result has one row per retained account",
    ],
    partial=True,
    source_row_count=1284,
)

churn = evidence(
    "q-churn",
    source="warehouse.accounts",
    sql=(
        "SELECT COUNT(*) AS churned_accounts, SUM(rev_2024) AS churned_revenue\n"
        "FROM account_years WHERE rev_2024 > 0 AND COALESCE(rev_2025, 0) = 0;"
    ),
    key=[],
    columns=[("churned_accounts", "integer"), ("churned_revenue", "decimal")],
    rows=[[142, "1018450.00"]],
    assumptions=["churned = revenue in 2024 and none in 2025"],
)

RECORDS = [rev_yoy, seg_delta, monthly, top_accounts, churn]


def pin(doc):
    return {"id": doc["id"], "version": doc["version"]}


def cell(doc, key, column):
    return {"evidence": doc["id"], "version": doc["version"], "key": key, "column": column}


# -- report -------------------------------------------------------------------

blocks = [
    {"id": "b1", "type": "heading", "level": 1, "text": "Revenue Review: 2024 vs 2025"},
    {"id": "b2", "type": "text", "text": (
        "[Revenue fell 12.0%, from $4.21M to $3.71M]{#c1}. "
        "The decline was broad, but not even: one segment accounts for most of it."
    )},
    {"id": "b3", "type": "heading", "level": 2, "text": "Where the decline came from"},
    {"id": "b4", "type": "text", "text": (
        "[Mid-market revenue dropped $275K, 54% of the total decrease]{#c2}. "
        "In 2025, [Enterprise remained the largest segment, at 53% of revenue]{#c3}.\n\n"
        "Lost accounts explain much of the gap: "
        "[142 accounts churned, taking $1.02M of prior-year revenue with them]{#c4}."
    )},
    {"id": "b5", "type": "chart", "caption": "Revenue by segment and year",
     "evidence": [pin(seg_delta)],
     "spec": {
         "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
         "data": {"evidence": pin(seg_delta)},
         "width": 360, "height": 200,
         "mark": "bar",
         "encoding": {
             "x": {"field": "segment", "type": "nominal", "title": None, "axis": {"labelAngle": 0}},
             "xOffset": {"field": "year", "type": "nominal"},
             "y": {"field": "revenue", "type": "quantitative", "title": "Revenue ($)",
                   "axis": {"format": "~s"}},
             "color": {"field": "year", "type": "nominal", "title": "Year"},
         },
     }},
    {"id": "b6", "type": "heading", "level": 2, "text": "Accounts behind the decline"},
    {"id": "b7", "type": "text", "text": (
        "[The largest single decline was Northwind Traders, down $84.2K]{#c6}, and "
        "[with Blue Heron Supply and Cedar Logistics, the largest declines came to $201K]{#c7}. "
        "[The decline is concentrated in accounts that renewed onto the new pricing tier]{#c5}."
    )},
    {"id": "b8", "type": "chart", "caption": "Monthly revenue",
     "evidence": [pin(monthly)],
     "spec": {
         "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
         "data": {"evidence": pin(monthly)},
         "width": 420, "height": 180,
         "mark": {"type": "line", "point": True},
         "encoding": {
             "x": {"field": "month", "type": "temporal", "title": None},
             "y": {"field": "revenue", "type": "quantitative", "title": "Revenue ($)",
                   "axis": {"format": "~s"}},
         },
     }},
]

SEG = 'cell(q-seg-delta, "Mid-market", {y}, revenue)'
claims = [
    {"id": "c1", "block_id": "b2", "evidence": [pin(rev_yoy)], "numbers": [
        {"text": "12.0%", "tier": "computed",
         "expression": "pct_change(cell(q-rev-yoy, 2024, revenue), cell(q-rev-yoy, 2025, revenue))",
         "value": "-0.1199524940617577197149643705463183", "status": "ok"},
        {"text": "$4.21M", "tier": "direct", "cell": cell(rev_yoy, [2024], "revenue"), "status": "ok"},
        {"text": "$3.71M", "tier": "direct", "cell": cell(rev_yoy, [2025], "revenue"), "status": "ok"},
    ], "status": "ok"},
    {"id": "c2", "block_id": "b4", "evidence": [pin(seg_delta), pin(rev_yoy)], "numbers": [
        {"text": "$275K", "tier": "computed",
         "expression": f"{SEG.format(y=2025)} - {SEG.format(y=2024)}",
         "value": "-275000", "status": "ok"},
        {"text": "54%", "tier": "computed",
         "expression": (f"share({SEG.format(y=2024)} - {SEG.format(y=2025)}, "
                        "cell(q-rev-yoy, 2024, revenue) - cell(q-rev-yoy, 2025, revenue))"),
         "value": "0.5445544554455445544554455445544554", "status": "ok"},
    ], "status": "ok"},
    {"id": "c3", "block_id": "b4", "evidence": [pin(seg_delta)], "numbers": [
        {"text": "53%", "tier": "computed",
         "expression": ('share(cell(q-seg-delta, "Enterprise", 2025, revenue), '
                        'sum(cell(q-seg-delta, "Enterprise", 2025, revenue), '
                        'cell(q-seg-delta, "Mid-market", 2025, revenue), '
                        'cell(q-seg-delta, "SMB", 2025, revenue)))'),
         "value": "0.5263157894736842105263157894736842", "status": "ok"},
    ], "status": "ok"},
    {"id": "c4", "block_id": "b4", "evidence": [pin(churn)], "numbers": [
        {"text": "142", "tier": "direct", "cell": cell(churn, [], "churned_accounts"), "status": "ok"},
        {"text": "$1.02M", "tier": "direct", "cell": cell(churn, [], "churned_revenue"), "status": "ok"},
    ], "status": "ok"},
    {"id": "c5", "block_id": "b7", "evidence": [pin(top_accounts)], "numbers": [],
     "derivation": ("Read from the account list: most of the largest declines are accounts "
                    "whose 2025 renewal moved them to the new tier. Tier is not in this query."),
     "status": "unverified"},
    {"id": "c6", "block_id": "b7", "evidence": [pin(top_accounts)], "numbers": [
        {"text": "$84.2K", "tier": "direct", "cell": cell(top_accounts, ["A-1042"], "change"), "status": "ok"},
    ], "status": "ok"},
    {"id": "c7", "block_id": "b7", "evidence": [pin(top_accounts)], "numbers": [
        {"text": "$201K", "tier": "computed",
         "expression": ('sum(cell(q-top-accounts, "A-1042", change), cell(q-top-accounts, "A-1079", change), '
                        'cell(q-top-accounts, "A-1116", change))'),
         "value": "-201410", "status": "ok"},
    ], "status": "ok"},
]

manifest = {
    "schema_version": 1,
    "report": {"id": "rpt_01", "title": "Revenue Review: 2024 vs 2025"},
    "version": {
        "number": 3,
        "published_at": "2026-10-20T14:30:00Z",
        "published_by": {"kind": "human", "name": "Nate Duncan", "verified": False},
        "override": None,
    },
    "disclosure": "full",
    "validation": {"errors": 0, "warnings": 0, "info": 0},  # set by the validator
    "evidence": [
        {"id": d["id"], "version": d["version"], "file": f"evidence/{d['id']}.v{d['version']}.json"}
        for d in RECORDS
    ],
}


def write(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    write(OUT / "manifest.json", manifest)
    write(OUT / "report.json", {"blocks": blocks, "claims": claims, "non_data": []})
    for d in RECORDS:
        write(OUT / f"evidence/{d['id']}.v{d['version']}.json", d)

    b = bundle.load(OUT)
    result = validate(Subject.from_bundle(b))
    write(OUT / "report.json", result.apply(b.report))
    manifest["validation"] = result.counts()
    write(OUT / "manifest.json", manifest)
    for f in result.findings:
        print(f"{f.severity}: {f.code} {f.where()}: {f.message}")
    print(f"wrote {OUT}")
