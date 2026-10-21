import json
import shutil
import subprocess

import pytest

from bevis import pdf


def ast(example, tmp_path):
    return pdf.document_ast(example.load(), tmp_path)


def plain(doc) -> str:
    out = subprocess.run(
        ["pandoc", "-f", "json", "-t", "plain", "--wrap=none"],
        input=json.dumps(doc), capture_output=True, text=True, check=True,
    )
    return out.stdout


def nodes(node, tag):
    if isinstance(node, dict):
        if node.get("t") == tag:
            yield node
        yield from nodes(node.get("c"), tag)
    elif isinstance(node, list):
        for n in node:
            yield from nodes(n, tag)


def section(text: str, start: str, end: str | None = None) -> str:
    i = text.index(start)
    j = text.index(end, i) if end else len(text)
    return text[i:j]


def set_text(example, block_id, text):
    def fn(r):
        for b in r["blocks"]:
            if b["id"] == block_id:
                b["text"] = text
    example.edit("report.json", fn)


# -- body -------------------------------------------------------------------


def test_claim_markers_link_to_appendix(example, tmp_path):
    doc = ast(example, tmp_path)
    targets = [link["c"][2][0] for link in nodes(doc["blocks"], "Link")]
    assert "#ev-q-rev-yoy-v1" in targets
    text = plain(doc)
    assert "Revenue fell 12.0%, from $4.21M to $3.71M [q-rev-yoy]." in text
    assert "54% of the total decrease [q-seg-delta, q-rev-yoy]" in text
    assert "new pricing tier [q-top-accounts; unverified]" in text


def test_header(example, tmp_path):
    text = plain(ast(example, tmp_path))
    assert "Version 3 · published 2026-10-20 14:30 UTC by Nate Duncan (human, unverified)" in text
    assert "Validation at publish: 0 errors, 0 warnings, 1 info" in text
    assert "Disclosure: full" in text
    # The leading title heading is not repeated in the body.
    assert text.count("Revenue Review: 2024 vs 2025") == 0  # the title lives in metadata


def test_override_is_shown(example, tmp_path):
    example.edit("manifest.json", lambda m: m["version"].update(override={"reason": "known false alarm on c2"}))
    assert "Published with an override: known false alarm on c2" in plain(ast(example, tmp_path))


def test_charts_are_rendered_from_evidence(example, tmp_path):
    doc = ast(example, tmp_path)
    images = [img["c"][2][0] for img in nodes(doc["blocks"], "Image")]
    assert len(images) == 2
    for path in images:
        assert open(path, "rb").read(5) == b"%PDF-"
    assert "Revenue by segment and year [q-seg-delta]" in plain(doc)


def test_author_markdown_is_sanitized(example, tmp_path):
    set_text(example, "b2", (
        "[Revenue fell 12.0%, from $4.21M to $3.71M]{#c1}.\n\n"
        "\\input{/etc/passwd} `\\input{x}`{=latex} <b>bold</b> ![leak](/etc/passwd)\n\n"
        "```{=latex}\n\\input{/etc/hostname}\n```\n\n"
        "It cost $4M and $5M."
    ))
    doc = ast(example, tmp_path)
    assert not list(nodes(doc["blocks"], "RawInline"))
    raw_blocks = [b["c"][1] for b in nodes(doc["blocks"], "RawBlock")]
    assert raw_blocks == ["\\clearpage"]  # only the renderer's own page break
    assert not list(nodes(doc["blocks"], "Math"))
    images = [img["c"][2][0] for img in nodes(doc["blocks"], "Image")]
    assert "/etc/passwd" not in images
    text = plain(doc)
    assert "leak" in text and "It cost $4M and $5M." in text


def test_author_block_cannot_swallow_the_appendix(example, tmp_path):
    set_text(example, "b2", "[Revenue fell 12.0%, from $4.21M to $3.71M]{#c1}.\n\n```\nunclosed fence")
    text = plain(ast(example, tmp_path))
    assert "q-rev-yoy v1" in text
    assert "Accounts behind the decline" in text


def test_span_without_claim_gets_no_marker(example, tmp_path):
    set_text(example, "b2", "[Revenue fell 12.0%, from $4.21M to $3.71M]{#c1}. [Not a claim]{#c99}.")
    assert "Not a claim." in plain(ast(example, tmp_path))


def test_versions_shown_when_ids_repeat(example, tmp_path):
    doc = example.read("evidence/q-churn.v1.json")
    doc["version"] = 2
    example.write("evidence/q-churn.v2.json", doc)
    example.edit("manifest.json", lambda m: m["evidence"].append(
        {"id": "q-churn", "version": 2, "file": "evidence/q-churn.v2.json"}))
    text = plain(ast(example, tmp_path))
    assert "[q-churn v1]" in text
    assert "q-churn v2" in text


# -- appendix -----------------------------------------------------------------


def test_appendix_metadata(example, tmp_path):
    text = plain(ast(example, tmp_path))
    rev = section(text, "q-rev-yoy v1", "q-seg-delta v1")
    assert "Source: warehouse.orders" in rev
    assert "Provenance: agent-reported" in rev
    assert "Data hash: sha256:" in rev
    assert "SELECT year, SUM(amount) AS revenue FROM orders" in rev
    assert "revenue = SUM(amount), refunds excluded" in rev
    top = section(text, "q-top-accounts v1", "q-churn v1")
    assert "Rows: sample of 40 of 1,284 rows (partial result)" in top
    assert 'Params: {"n": 40}' in top


def test_derivations(example, tmp_path):
    text = plain(ast(example, tmp_path))
    rev = section(text, "q-rev-yoy v1", "q-seg-delta v1")
    assert "c1 “Revenue fell 12.0%, from $4.21M to $3.71M” — ok" in rev
    assert ("12.0% (computed, ok): pct_change(cell(q-rev-yoy, 2024, revenue), "
            "cell(q-rev-yoy, 2025, revenue)) = -0.1199524940617577197149643705463183") in rev
    assert "$4.21M (direct, ok): cell(q-rev-yoy, 2024, revenue) = 4210000" in rev
    top = section(text, "q-top-accounts v1", "q-churn v1")
    assert "c5 “The decline is concentrated" in top and "— unverified" in top
    assert "Tier is not in this query." in top
    assert '$84.2K (direct, ok): cell(q-top-accounts, "A-1042", change) = -84210' in top


def test_small_table_is_shown_in_full(example, tmp_path):
    text = plain(ast(example, tmp_path))
    monthly = section(text, "q-monthly v1", "q-top-accounts v1")
    assert monthly.count("2024-") == 12 and monthly.count("2025-") == 12
    assert "Showing" not in monthly
    seg = section(text, "q-seg-delta v1", "q-monthly v1")
    assert "customers" in seg  # all four columns fit


def test_large_table_shows_cited_rows_and_columns(example, tmp_path):
    doc = ast(example, tmp_path)
    text = plain(doc)
    top = section(text, "q-top-accounts v1", "q-churn v1")
    assert "Showing 3 of 40 rows (cited rows only)." in top
    assert "4 columns not shown: account_name, segment, rev_2024, rev_2025." in top
    assert "A-1042" in top and "A-1153" not in top
    strong = {pdf.stringify(s) for s in nodes(doc["blocks"], "Strong")}
    assert {"A-1042", "-84210", "A-1079", "-62400"} <= strong


def test_uncited_large_table_shows_note(example, tmp_path):
    example.edit("report.json", lambda r: r.update(claims=[c for c in r["claims"] if c["id"] not in ("c6", "c7")]))
    top = section(plain(ast(example, tmp_path)), "q-top-accounts v1", "q-churn v1")
    assert "Showing 0 of 40 rows (cited rows only)." in top


def test_wide_table_keeps_key_and_cited_columns(example, tmp_path, monkeypatch):
    monkeypatch.setattr(pdf, "MAX_COLUMNS", 3)
    seg = section(plain(ast(example, tmp_path)), "q-seg-delta v1", "q-monthly v1")
    assert "1 column not shown: customers." in seg


def test_null_cells(example, tmp_path):
    def null_first(d):
        d["rows"][0][3] = None
    example.edit("evidence/q-seg-delta.v1.json", null_first)
    from bevis.evidence import Record
    example.edit("evidence/q-seg-delta.v1.json",
                 lambda d: d.update(data_hash=Record.from_json(d).compute_hash()))
    doc = ast(example, tmp_path)
    assert "null" in {pdf.stringify(e) for e in nodes(doc["blocks"], "Emph")}


# -- disclosure ---------------------------------------------------------------


def test_metadata_disclosure(example, tmp_path):
    example.restrict("metadata")
    doc = ast(example, tmp_path)
    text = plain(doc)
    assert "Disclosure: metadata (no data values are included)" in text
    assert "Chart not drawn: its data is not disclosed at this level." in text
    assert "Rows are not disclosed at metadata level (2 rows)." in text
    assert not list(nodes(doc["blocks"], "Image"))
    assert "4210000" not in section(text, "Evidence")


def test_cited_disclosure(example, tmp_path):
    example.restrict("cited")
    text = plain(ast(example, tmp_path))
    assert "Disclosure: cited (only cited rows and columns are included)" in text
    top = section(text, "q-top-accounts v1", "q-churn v1")
    assert "Showing 3 of 40 rows (the rest are not disclosed at this level)." in top
    assert "Not disclosed at this level: account_name, segment, rev_2024, rev_2025." in top
    table = section(top, "Result")
    assert "A-1079" in table and "Blue Heron" not in table  # a cited row whose name is withheld


# -- helpers ------------------------------------------------------------------


def test_escape_makes_punctuation_literal():
    assert pdf.esc("a*b_c `d` $e$ [f](g) <h> \\i") == r"a\*b\_c \`d\` \$e\$ \[f\]\(g\) \<h\> \\i"


def test_direct_ref_writes_expression_literals():
    cell = {"evidence": "q-x", "version": 1, "key": ['say "hi"', -3, True, None], "column": "net revenue"}
    assert pdf.direct_ref(cell) == 'cell(q-x, "say \\"hi\\"", -3, true, null, "net revenue")'


def test_code_span_survives_backticks():
    assert pdf.code("a ``b`` c") == "``` a ``b`` c ```"


# -- end to end -----------------------------------------------------------------


@pytest.mark.skipif(shutil.which("xelatex") is None or shutil.which("pdftotext") is None,
                    reason="needs xelatex and pdftotext")
def test_cli_writes_pdf(example):
    from bevis.cli import main

    assert main(["export", "pdf", str(example.root)]) == 0
    out = example.root / "report.pdf"
    text = subprocess.run(["pdftotext", "-layout", str(out), "-"], capture_output=True, text=True, check=True).stdout
    assert "Revenue Review: 2024 vs 2025" in text
    assert "[q-rev-yoy]" in text
    assert "Showing 3 of 40 rows" in text


def test_cli_reports_bad_bundle(example, capsys):
    from bevis.cli import main

    (example.root / "manifest.json").write_text("{")
    assert main(["export", "pdf", str(example.root)]) == 1
    assert "manifest.json: not valid JSON" in capsys.readouterr().err
