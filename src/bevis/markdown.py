"""Author Markdown, parsed with Pandoc into its JSON AST."""

from __future__ import annotations

import json
import subprocess
from functools import lru_cache
from typing import Any

# Pandoc Markdown without raw TeX/HTML, metadata blocks, implicit figures, or
# math. Dollar amounts are common in reports and must stay text.
AUTHOR_FORMAT = (
    "markdown-raw_tex-raw_html-raw_attribute-yaml_metadata_block-pandoc_title_block"
    "-implicit_figures-tex_math_dollars-tex_math_single_backslash-tex_math_double_backslash"
)


class MarkdownError(Exception):
    pass


def pandoc_json(markdown: str, fmt: str = AUTHOR_FORMAT) -> dict:
    proc = subprocess.run(
        ["pandoc", "-f", fmt, "-t", "json"],
        input=markdown,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise MarkdownError(f"pandoc could not parse Markdown: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def str_node(text: str) -> dict:
    return {"t": "Str", "c": text}


def stringify(node: Any) -> str:
    if isinstance(node, list):
        return "".join(stringify(n) for n in node)
    if not isinstance(node, dict):
        return ""
    t = node.get("t")
    if t == "Str":
        return node["c"]
    if t in ("Space", "SoftBreak", "LineBreak"):
        return " "
    if t == "Code":
        return node["c"][1]
    if t == "Quoted":
        q = "“”" if node["c"][0]["t"] == "DoubleQuote" else "‘’"
        return q[0] + stringify(node["c"][1]) + q[1]
    return stringify(node.get("c"))


# -- HTML for the web view --------------------------------------------------------

# Where each tagged node keeps its Attr. Author attributes never reach the HTML:
# pandoc would write `{onclick=...}` through as a real attribute.
_ATTR_AT = {"Code": 0, "CodeBlock": 0, "Header": 1, "Div": 0, "Span": 0, "Link": 0, "Image": 0,
            "Table": 0, "Figure": 0}
_SAFE_LINK = ("http://", "https://", "mailto:")
_NO_ATTR = ["", [], []]


def _clean(node: Any, claims: frozenset[str]) -> Any:
    if isinstance(node, list):
        out = []
        for item in node:
            out.extend(_clean_item(item, claims))
        return out
    return node


def _clean_item(item: Any, claims: frozenset[str]) -> list:
    if not isinstance(item, dict) or "t" not in item:
        return [_clean(item, claims)]
    t, c = item["t"], item.get("c")
    if t in ("RawBlock", "RawInline"):
        return []
    if t == "Image":
        return _clean(c[1], claims)
    if t == "Link" and not str(c[2][0]).lower().startswith(_SAFE_LINK):
        return _clean(c[1], claims)
    if t == "Span" and c[0][0] in claims:
        return [{"t": "Span", "c": [["", ["claim"], [["claim", c[0][0]]]], _clean(c[1], claims)]}]
    if not isinstance(c, list):
        return [item]
    if all(isinstance(x, dict) and "t" in x for x in c):
        return [{"t": t, "c": _clean(c, claims)}]  # Para, Emph, ...: c is a list of nodes
    c = [_clean(x, claims) if isinstance(x, list) else x for x in c]
    if t in _ATTR_AT:
        c[_ATTR_AT[t]] = _NO_ATTR
    if t == "Link":
        c[2] = [c[2][0], ""]  # keep the URL, drop the title
    return [{"t": t, "c": c}]


@lru_cache(maxsize=4096)
def to_html(markdown: str, claims: frozenset[str]) -> str:
    """Author Markdown as an HTML fragment that is safe to insert into the page.

    Raw HTML and TeX, images, author attributes, and links other than http(s)
    and mailto are removed. A claim span becomes
    `<span class="claim" data-claim="cN">`.
    """
    doc = pandoc_json(markdown)
    doc["blocks"] = _clean(doc["blocks"], claims)
    proc = subprocess.run(
        ["pandoc", "-f", "json", "-t", "html", "--wrap=none"],
        input=json.dumps(doc),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise MarkdownError(f"pandoc could not write HTML: {proc.stderr.strip()}")
    return proc.stdout
