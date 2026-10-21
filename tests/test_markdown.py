import pytest

from bevis.markdown import to_html

CLAIMS = frozenset({"c1", "c2"})


def test_claim_spans_become_marked_spans():
    html = to_html("[Revenue fell 12.0%]{#c1}. [Not a claim]{#c9}.", CLAIMS)
    assert '<span class="claim" data-claim="c1">Revenue fell 12.0%</span>' in html
    assert "<span>Not a claim</span>" in html


@pytest.mark.parametrize(
    "markdown, forbidden",
    [
        ('[x]{#c1 onclick="alert(1)"}', "onclick"),
        ("[x](javascript:alert(1))", "javascript"),
        ("[x](JaVaScRiPt:alert(1))", "JaVaScRiPt"),
        ("[x](data:text/html,<script>1</script>)", "data:"),
        ("[x](//evil.example/p)", "evil.example"),
        ("<script>alert(1)</script>", "<script"),
        ("<img src=x onerror=alert(1)>", "<img"),
        ("![leak](/etc/passwd)", "<img"),
        ("```{=html}\n<b>raw</b>\n```", "<b>"),
        ("`x`{=html}", "<x"),
        ("## Head {#h onclick=x}", "onclick"),
        ("`code`{.x onclick=y}", "onclick"),
        ("::: {onmouseover=z}\ntext\n:::", "onmouseover"),
        ('[t]{style="background:url(x)"}', "style"),
        ('[ok](https://a.example "title onclick")', "title="),
        ("| a |\n|---|\n| [t]{onclick=w} |", "onclick"),
    ],
)
def test_author_html_is_neutralized(markdown, forbidden):
    assert forbidden not in to_html(markdown, CLAIMS)


def test_safe_links_survive():
    assert '<a href="https://a.example/x">ok</a>' in to_html("[ok](https://a.example/x)", CLAIMS)
    assert '<a href="mailto:a@b.example">m</a>' in to_html("[m](mailto:a@b.example)", CLAIMS)


def test_dollar_amounts_stay_text():
    assert "$4M and $5M" in to_html("It cost $4M and $5M.", CLAIMS)
