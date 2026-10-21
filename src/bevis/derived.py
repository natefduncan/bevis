"""Markdown and HTML renderings of a bundle (SPEC §10: derived files).

Both come from the same Pandoc document as the PDF, so the three agree.
Charts are SVG. Markdown is GitHub-flavored, with its charts in a
`charts/` folder beside it; HTML is one self-contained file.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .bundle import Bundle
from .markdown import stringify
from .pdf import RenderError, document_ast

HTML_STYLE = """
body { max-width: 46em; margin: 2em auto; padding: 0 1em; font: 17px/1.55 Georgia, serif; color: #1d1f23; }
h1, h2, h3 { font-family: system-ui, sans-serif; line-height: 1.3; }
code, pre { font-size: 0.85em; }
pre { white-space: pre-wrap; background: #f6f5f2; padding: 0.6em; border-radius: 6px; }
table { border-collapse: collapse; font: 14px system-ui, sans-serif; }
th, td { border-bottom: 1px solid #ddd; padding: 3px 8px; }
img { max-width: 100%; }
a { color: #1f4e9a; }
@media (prefers-color-scheme: dark) {
  body { background: #15171a; color: #e6e4df; }
  pre { background: #1f2226; }
  th, td { border-color: #333; }
  a { color: #8db3f0; }
  img { background: #fff; }
}
"""


def _pandoc(doc: dict, args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        ["pandoc", "-f", "json", *args],
        input=json.dumps(doc), capture_output=True, text=True, cwd=cwd,
    )
    if proc.returncode != 0:
        raise RenderError(f"pandoc failed: {proc.stderr.strip()[-2000:]}")
    return proc.stdout


def _gfm_id(text: str) -> str:
    """GitHub's heading anchor: lowercase, punctuation dropped, spaces to hyphens."""
    return re.sub(r"[^\w\- ]", "", text.lower()).replace(" ", "-")


def _for_gfm(node: Any, anchors: dict[str, str]) -> Any:
    """GitHub ignores explicit ids, so point appendix links at its own heading anchors
    and drop links back to claim spans, which have no anchor there."""
    if isinstance(node, list):
        out = []
        for item in node:
            if isinstance(item, dict) and item.get("t") == "Link":
                target = item["c"][2][0]
                if target.startswith("#") and target[1:] in anchors:
                    item = {**item, "c": [item["c"][0], _for_gfm(item["c"][1], anchors),
                                          ["#" + anchors[target[1:]], ""]]}
                elif target.startswith("#"):
                    out.extend(_for_gfm(item["c"][1], anchors))
                    continue
            elif isinstance(item, dict) and item.get("t") == "Span":
                out.extend(_for_gfm(item["c"][1], anchors))
                continue
            out.append(_for_gfm(item, anchors))
        return out
    if isinstance(node, dict) and "c" in node:
        return {**node, "c": _for_gfm(node["c"], anchors)}
    return node


def _headers(blocks: list) -> dict[str, str]:
    """Explicit header id -> GitHub anchor of that header's text."""
    return {b["c"][1][0]: _gfm_id(stringify(b["c"][2])) for b in blocks
            if isinstance(b, dict) and b.get("t") == "Header" and b["c"][1][0]}


def render_markdown(bundle: Bundle, out: Path) -> None:
    """Write GitHub-flavored Markdown to `out`, with charts in `out`'s folder under charts/."""
    out = out.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = document_ast(bundle, out.parent, chart_format="svg", relative=True)
    doc["blocks"] = [{"t": "Header", "c": [1, ["", [], []], [{"t": "Str", "c": bundle.title}]]}, *doc["blocks"]]
    doc["blocks"] = _for_gfm(doc["blocks"], _headers(doc["blocks"]))
    out.write_text(_pandoc(doc, ["-t", "gfm-raw_html", "--wrap=none"], out.parent), encoding="utf-8")


def render_html(bundle: Bundle, out: Path) -> None:
    """Write one self-contained HTML file: charts and styles are embedded, nothing is fetched."""
    out = out.resolve()
    with tempfile.TemporaryDirectory(prefix="bevis-html-") as tmp:
        work = Path(tmp)
        doc = document_ast(bundle, work, chart_format="svg")
        (work / "style.css").write_text(HTML_STYLE)
        html = _pandoc(doc, ["-t", "html5", "--standalone", "--embed-resources", "--css", "style.css",
                             "--metadata", "lang=en"], work)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
