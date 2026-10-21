import json
from pathlib import Path

import pytest

from bevis.evidence import Record
from bevis.validate import Subject, analyze_text, loose_numbers, validate

DIRECTION = json.loads((Path(__file__).parents[1] / "spec/expr/direction.json").read_text())


def run(example):
    return validate(Subject.from_bundle(example.load()))


def codes(result, **where):
    return sorted(
        f.code for f in result.findings
        if all(getattr(f, k) == v for k, v in where.items())
    )


def edit_block(example, block_id, fn):
    def go(r):
        for b in r["blocks"]:
            if b["id"] == block_id:
                fn(b)
    example.edit("report.json", go)


def edit_claim(example, claim_id, fn):
    def go(r):
        for c in r["claims"]:
            if c["id"] == claim_id:
                fn(c)
    example.edit("report.json", go)


def replace_text(example, block_id, old, new):
    def fn(b):
        assert old in b["text"], old
        b["text"] = b["text"].replace(old, new)
    edit_block(example, block_id, fn)


# -- shared direction cases -------------------------------------------------------


def one_span_subject(case) -> Subject:
    record = Record.from_json({
        "id": "q-val", "version": 1, "key": [],
        "columns": [{"name": "v", "type": "decimal"}], "rows": [[case["value"]]],
    })
    number = {"text": case["text"], "status": "ok"}
    if case["change"]:
        number |= {"tier": "computed", "expression": "cell(q-val, v) - 0", "value": None}
    else:
        number |= {"tier": "direct", "cell": {"evidence": "q-val", "version": 1, "key": [], "column": "v"}}
    report = {
        "blocks": [{"id": "b1", "type": "text", "text": f"[{case['span']}]{{#c1}}"}],
        "claims": [{"id": "c1", "block_id": "b1", "evidence": [{"id": "q-val", "version": 1}],
                    "numbers": [number], "status": "ok"}],
        "non_data": [],
    }
    return Subject(report, {("q-val", 1): record}, {"q-val": 1})


@pytest.mark.parametrize("case", DIRECTION["cases"], ids=[c["name"] for c in DIRECTION["cases"]])
def test_direction_case(case):
    assert codes(validate(one_span_subject(case))) == sorted(case["expect"])


# -- the example ----------------------------------------------------------------------


def test_example_is_clean(example):
    r = run(example)
    assert r.counts() == {"errors": 0, "warnings": 0, "info": 1}
    assert codes(r) == ["claim.asserted"]
    assert r.claims["c5"].status == "unverified"
    assert all(r.claims[c].status == "ok" for c in ("c1", "c2", "c3", "c4", "c6", "c7"))
    assert r.claims["c1"].numbers[0].value == "-0.1199524940617577197149643705463183"


def test_example_records_match_validator(example):
    # The committed report and manifest carry exactly what the validator computes.
    b = example.load()
    r = validate(Subject.from_bundle(b))
    assert r.apply(b.report) == b.report
    assert r.counts() == b.manifest["validation"]


# -- spans and claims ----------------------------------------------------------------


def test_span_without_claim(example):
    replace_text(example, "b2", "one segment", "[one segment]{#c99}")
    assert "span.no_claim" in codes(run(example), block="b2")


def test_claim_without_span(example):
    replace_text(example, "b2", "]{#c1}", "]")
    replace_text(example, "b2", "[Revenue", "Revenue")
    r = run(example)
    assert "claim.no_span" in codes(r, claim="c1")
    assert r.claims["c1"].status == "error"


def test_span_in_wrong_block(example):
    edit_claim(example, "c1", lambda c: c.update(block_id="b4"))
    assert "claim.wrong_block" in codes(run(example), claim="c1")


def test_duplicate_span(example):
    replace_text(example, "b2", "one segment", "[one segment]{#c1}")
    assert "span.duplicate" in codes(run(example))


def test_nested_span(example):
    replace_text(example, "b4", "[Mid-market revenue dropped $275K, 54% of the total decrease]{#c2}",
                 "[Mid-market revenue dropped $275K, [54% of the total decrease]{#c8}]{#c2}")
    assert "span.nested" in codes(run(example))


def test_claim_without_evidence(example):
    edit_claim(example, "c5", lambda c: c.update(evidence=[]))
    assert "claim.no_evidence" in codes(run(example), claim="c5")


def test_number_not_in_span(example):
    edit_claim(example, "c1", lambda c: c["numbers"][1].update(text="$4.2M"))
    r = run(example)
    assert codes(r, claim="c1", number=1) == ["number.not_in_span"]
    assert "span.uncovered_number" in codes(r, claim="c1")  # $4.21M is now unclaimed


def test_uncovered_number_in_span(example):
    replace_text(example, "b2", "]{#c1}", " over 12 months in 2025]{#c1}")
    r = run(example)
    msgs = [f.message for f in r.findings if f.code == "span.uncovered_number"]
    assert msgs == ["“12” is in the span but not among the claim's numbers"]  # 2025 is a year


def test_unparsed_number_in_span(example):
    replace_text(example, "b2", "]{#c1}", ", the 3rd drop]{#c1}")
    assert "span.unparsed_number" in codes(run(example), claim="c1")


def test_written_number_in_span(example):
    replace_text(example, "b2", "]{#c1}", " in twelve months]{#c1}")
    assert "text.written_number" in codes(run(example), claim="c1")


# -- matching -----------------------------------------------------------------------------


def test_direct_mismatch(example):
    replace_text(example, "b2", "$4.21M", "$4.22M")
    edit_claim(example, "c1", lambda c: c["numbers"][1].update(text="$4.22M"))
    r = run(example)
    [f] = [f for f in r.findings if f.code == "number.mismatch"]
    assert f.message == "“$4.22M” does not match 4210000, which displays as 4.21 at that precision"
    assert r.claims["c1"].numbers[1].status == "error"
    assert r.claims["c1"].status == "error"


def test_computed_mismatch(example):
    replace_text(example, "b2", "12.0%", "12.1%")
    edit_claim(example, "c1", lambda c: c["numbers"][0].update(text="12.1%"))
    assert codes(run(example), claim="c1", number=0) == ["number.mismatch"]


def test_less_precise_display_is_weaker(example):
    replace_text(example, "b2", "$4.21M", "$4.2M")
    edit_claim(example, "c1", lambda c: c["numbers"][1].update(text="$4.2M"))
    assert codes(run(example)) == ["claim.asserted"]


def test_sign_mismatch(example):
    replace_text(example, "b2", "fell 12.0%", "fell +12.0%")
    edit_claim(example, "c1", lambda c: c["numbers"][0].update(text="+12.0%"))
    assert codes(run(example), claim="c1", number=0) == ["number.sign_mismatch"]


def test_direction_contradiction(example):
    replace_text(example, "b2", "fell", "rose")
    assert codes(run(example), claim="c1", number=0) == ["direction.contradicts"]


def test_direction_unstated(example):
    replace_text(example, "b2", "fell", "moved")
    assert codes(run(example), claim="c1", number=0) == ["direction.unstated"]
    assert run(example).claims["c1"].status == "warning"


def test_unparseable_number_text(example):
    replace_text(example, "b2", "12.0%", "twelve percent")
    edit_claim(example, "c1", lambda c: c["numbers"][0].update(text="twelve percent"))
    assert "number.unparseable" in codes(run(example), claim="c1", number=0)


@pytest.mark.parametrize(
    "expression, code",
    [
        ("pct_change(cell(q-rev-yoy, 2024, revenue)", "expr.parse"),
        ("cell(q-rev-yoy, 2023, revenue)", "eval.missing_row"),
        ("cell(q-rev-yoy, 2024, profit)", "eval.missing_column"),
        ("cell(q-rev-yoy, revenue)", "eval.key_arity"),
        ('cell(q-rev-yoy, "2024", revenue)', "eval.key_coercion"),
        ("cell(q-churn, churned_accounts)", "eval.unknown_evidence"),
        ("cell(q-rev-yoy, 2024, revenue) / 0", "eval.division_by_zero"),
    ],
)
def test_expression_failures(example, expression, code):
    edit_claim(example, "c1", lambda c: c["numbers"][0].update(expression=expression))
    r = run(example)
    assert codes(r, claim="c1", number=0) == [code]
    assert r.claims["c1"].numbers[0].value is None


def test_direct_cell_must_exist(example):
    edit_claim(example, "c1", lambda c: c["numbers"][1]["cell"].update(key=[2023]))
    assert codes(run(example), claim="c1", number=1) == ["eval.missing_row"]


def test_direct_cell_must_be_numeric(example):
    edit_claim(example, "c3", lambda c: c.update(numbers=[{
        "text": "53%", "tier": "direct", "status": "ok",
        "cell": {"evidence": "q-seg-delta", "version": 1, "key": ["Mid-market", 2025], "column": "segment"},
    }]))
    assert codes(run(example), claim="c3", number=0) == ["number.non_numeric"]


def test_asserted_number(example):
    def assert_count(c):
        n = c["numbers"][0]
        del n["cell"]
        n.update(tier="asserted", derivation="counted by hand")
    edit_claim(example, "c4", assert_count)
    r = run(example)
    assert codes(r, claim="c4", number=0) == ["number.asserted"]
    assert r.claims["c4"].numbers[0].status == "unverified"
    assert r.claims["c4"].status == "unverified"


# -- numbers outside spans ----------------------------------------------------------------


def test_uncited_numbers(example):
    replace_text(example, "b2", "one segment", "one of 3 segments")
    r = run(example)
    assert codes(r, block="b2") == ["text.uncited_number"]
    assert r.claims["c1"].status == "ok"


def test_non_data_clears_warning(example):
    replace_text(example, "b2", "one segment", "one of 3 segments")
    example.edit("report.json", lambda r: r["non_data"].append(
        {"block_id": "b2", "text": "3", "reason": "count of segments, stated for orientation"}))
    assert codes(run(example), block="b2") == []


def test_heading_and_caption_numbers(example):
    edit_block(example, "b6", lambda b: b.update(text="Top 40 accounts behind the decline"))
    edit_block(example, "b5", lambda b: b.update(caption="Revenue in 3 segments, 2024 and 2025"))
    r = run(example)
    assert codes(r, block="b6") == ["text.uncited_number"]
    assert codes(r, block="b5") == ["text.uncited_number"]


@pytest.mark.parametrize(
    "text, loose",
    [
        ("In 2025 we grew.", []),
        ("2,025 customers", ["2,025"]),
        ("2025 customers", []),  # read as a year; put it in a span to check it
        ("On 2025-03-01T10:00:00Z and 2025-03-02.", []),
        ("March 3, 2025 and 3 March 2025 and Sept. 14", []),
        ("March 45 and May 32", ["45", "32"]),
        ("may 3 options", ["3"]),
        ("$2025 and 2025% and 2025.0", ["$2025", "2025%", "2025.0"]),
        ("1899 and 2101", ["1899", "2101"]),
        ("Steps:\n1. first\n2) second\nnot 3. here", ["3"]),
        ("We saw 48 accounts.", ["48"]),
    ],
)
def test_loose_numbers(text, loose):
    assert [d.text for d in loose_numbers(text)] == loose


def test_code_is_ignored():
    t = analyze_text("b1", "Run `LIMIT 40` then\n\n```\nSELECT 5\n```\n\n1. step")
    assert loose_numbers(t.outside) == []


def test_numbers_do_not_join_across_spans():
    t = analyze_text("b1", "4[2]{#c1}7")
    assert [d.text for d in loose_numbers(t.outside)] == ["4", "7"]
    assert [s.text for s in t.spans] == ["2"]


def test_table_cells_stay_separate():
    t = analyze_text("b1", "| a | b |\n|---|---|\n| 12 | 34 |")
    assert [d.text for d in loose_numbers(t.outside)] == ["12", "34"]


# -- pins, charts, images --------------------------------------------------------------------


def test_stale_pin(example):
    doc = example.read("evidence/q-rev-yoy.v1.json")
    doc["version"] = 2
    example.write("evidence/q-rev-yoy.v2.json", doc)
    example.edit("manifest.json", lambda m: m["evidence"].append(
        {"id": "q-rev-yoy", "version": 2, "file": "evidence/q-rev-yoy.v2.json"}))
    r = run(example)
    assert "pin.stale" in codes(r, claim="c1")
    assert r.claims["c1"].status == "warning"


@pytest.mark.parametrize(
    "change, code",
    [
        (lambda s: s.update(transform=[{"aggregate": [{"op": "sum", "as": "t"}]}]), "chart.transform"),
        (lambda s: s.update(transform=[{"filter": "datum.year == 2025"}, {"calculate": "1", "as": "x"}]),
         "chart.transform"),
        (lambda s: s["encoding"]["y"].update(aggregate="sum"), "chart.encoding"),
        (lambda s: s["encoding"]["x"].update(timeUnit="year"), "chart.encoding"),
        (lambda s: s["encoding"].update(tooltip=[{"field": "revenue", "bin": True}]), "chart.encoding"),
        (lambda s: s.update(description="x" * 70000), "chart.size"),
    ],
)
def test_chart_strictness(example, change, code):
    edit_block(example, "b5", lambda b: change(b["spec"]))
    assert code in codes(run(example), block="b5")


@pytest.mark.parametrize(
    "change",
    [
        lambda s: s.update(transform=[{"filter": "datum.year == 2025"}]),
        lambda s: s["encoding"]["x"].update(sort="-y"),
        lambda s: s["encoding"]["y"].update(bin=False),
        lambda s: s.update(layer=[{"mark": "bar"}, {"mark": "text"}]),
    ],
)
def test_chart_allowed(example, change):
    edit_block(example, "b5", lambda b: change(b["spec"]))
    assert codes(run(example), block="b5") == []


def test_image_is_figure_level(example):
    (example.root / "assets").mkdir()
    (example.root / "assets/fig.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    example.edit("report.json", lambda r: r["blocks"].append({
        "id": "b9", "type": "image", "src": "assets/fig.png", "alt": "fig",
        "evidence": [{"id": "q-churn", "version": 1}]}))
    assert codes(run(example), block="b9") == ["image.figure_level"]


# -- disclosure and output ------------------------------------------------------------------


def test_cited_bundle_still_verifies(example):
    example.restrict("cited")
    assert codes(run(example)) == ["claim.asserted"]


def test_metadata_bundle_is_unverifiable(example):
    example.restrict("metadata")
    r = run(example)
    assert r.counts()["errors"] == 0
    assert "eval.withheld" in codes(r, claim="c1")
    assert r.claims["c1"].status == "unverified"


def test_apply_writes_statuses(example):
    replace_text(example, "b2", "fell", "rose")
    b = example.load()
    out = validate(Subject.from_bundle(b)).apply(b.report)
    c1 = next(c for c in out["claims"] if c["id"] == "c1")
    assert c1["status"] == "error"
    assert c1["numbers"][0]["status"] == "error"
    assert c1["numbers"][0]["message"].startswith("“rose” but “12.0%” is a decrease")
    assert b.report["claims"][0]["status"] == "ok"  # the input is untouched


# -- command line -------------------------------------------------------------------------------


def test_cli_validate_clean(example, capsys):
    from bevis.cli import main

    assert main(["validate", str(example.root)]) == 0
    out = capsys.readouterr().out
    assert "0 errors, 0 warnings, 1 info" in out
    assert "note:" not in out


def test_cli_validate_errors(example, capsys):
    from bevis.cli import main

    replace_text(example, "b2", "fell", "rose")
    assert main(["validate", str(example.root)]) == 1
    out = capsys.readouterr().out
    assert "direction.contradicts" in out and "c1 #1:" in out
    assert "note: the bundle recorded 0 errors, 0 warnings, 1 info at publish" in out


def test_cli_validate_json(example, capsys):
    from bevis.cli import main

    assert main(["validate", "--json", str(example.root)]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["counts"] == doc["recorded"] == {"errors": 0, "warnings": 0, "info": 1}
    assert doc["findings"][0]["code"] == "claim.asserted"
