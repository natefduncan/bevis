"""Author Markdown, parsed with Pandoc into its JSON AST."""

from __future__ import annotations

import json
import subprocess
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
