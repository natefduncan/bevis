import re

import pytest

from bevis.derived import render_html, render_markdown

from .test_pdf import set_text


def test_markdown(example, tmp_path):
    out = tmp_path / "out" / "report.md"
    render_markdown(example.load(), out)
    md = out.read_text()
    assert md.startswith("# Revenue Review: 2024 vs 2025\n")
    assert "Revenue fell 12.0%, from \\$4.21M to \\$3.71M \\[[q-rev-yoy](#q-rev-yoy-v1)\\]" in md
    assert "## q-rev-yoy v1" in md  # GitHub's anchor for this heading is q-rev-yoy-v1
    assert "![Revenue by segment and year](charts/b5.svg)" in md
    assert (out.parent / "charts" / "b5.svg").read_text().startswith("<svg")
    assert "\\clearpage" not in md and "<span" not in md
    assert "](#c" not in md  # claim back-links have no anchor on GitHub
    assert "| **A-1042** |" in md and "Showing 3 of 40 rows (cited rows only)." in md


def test_html_is_self_contained(example, tmp_path):
    out = tmp_path / "report.html"
    render_html(example.load(), out)
    html = out.read_text()
    assert "<title>Revenue Review: 2024 vs 2025</title>" in html
    assert html.count("data:image/svg+xml") == 2
    assert not re.search(r"<(script|link)\b", html)
    assert re.search(r'<a href="#ev-q-rev-yoy-v1"', html) and 'id="ev-q-rev-yoy-v1"' in html
    assert 'id="c1"' in html  # derivations link back to the claim


def test_html_neutralizes_author_markup(example, tmp_path):
    set_text(example, "b2", (
        '[Revenue fell 12.0%, from $4.21M to $3.71M]{#c1 onclick="alert(1)"}. '
        '[x](javascript:alert(1)) [ok](https://example.com) <script>alert(2)</script> [y]{onmouseover=z}'
    ))
    out = tmp_path / "report.html"
    render_html(example.load(), out)
    html = out.read_text()
    for bad in ("onclick", "javascript:", "<script", "onmouseover"):
        assert bad not in html
    assert '<a href="https://example.com">ok</a>' in html


@pytest.mark.parametrize("fmt", ["md", "html"])
def test_cli(example, fmt):
    from bevis.cli import main

    assert main(["export", fmt, str(example.root)]) == 0
    assert (example.root / f"report.{fmt}").stat().st_size > 1000


def add_href(r):
    """Give chart b5 an href channel at the top level and b8 one inside a layer."""
    blocks = {b["id"]: b for b in r["blocks"]}
    blocks["b5"]["spec"]["encoding"]["href"] = {"value": "javascript:alert(1)"}
    s = blocks["b8"]["spec"]
    s["layer"] = [{"mark": s.pop("mark"), "encoding": {**s.pop("encoding"), "href": {"field": "month"}}}]


@pytest.mark.parametrize("fmt", ["pdf", "md", "html"])
def test_chart_href_is_dropped_in_exports(example, fmt):
    from bevis.cli import main
    from bevis.validate import Subject, validate

    example.edit("report.json", add_href)
    assert validate(Subject.from_bundle(example.load())).ok
    assert main(["export", fmt, str(example.root)]) == 0
    if fmt == "html":
        html = (example.root / "report.html").read_text()
        assert html.count("data:image/svg+xml") == 2 and "javascript:" not in html


def test_drop_href_reaches_nested_views_and_leaves_data_alone():
    from bevis.charts import drop_href

    spec = {
        "data": {"values": [{"href": "kept"}]},
        "encoding": {"x": {"field": "a"}, "href": {"field": "a"}},
        "vconcat": [{"layer": [{"encoding": {"href": {"value": "https://example.com"}, "y": {"field": "b"}}}]}],
    }
    drop_href(spec)
    assert spec == {
        "data": {"values": [{"href": "kept"}]},
        "encoding": {"x": {"field": "a"}},
        "vconcat": [{"layer": [{"encoding": {"y": {"field": "b"}}}]}],
    }
