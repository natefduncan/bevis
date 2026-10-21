"""Render a bundle to PDF with Pandoc and XeLaTeX (SPEC §10.4).

The renderer reads only the bundle. Author Markdown is parsed one block at
a time, so a block cannot break into its neighbours or the appendix, and
raw TeX, raw HTML, and images are removed from it. Everything else is
generated Markdown with all punctuation escaped.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .bundle import Bundle, resolve_direct
from .cited import Citations, citations
from .evidence import Record
from .markdown import ATTR_AT, AUTHOR_FORMAT, NO_ATTR, SAFE_LINKS, pandoc_json, stringify
from .markdown import str_node as _str
from .values import Value

FULL_TABLE_ROWS = 25  # tables up to this many rows are shown in full
MAX_COLUMNS = 7  # page-width limit for a full table

GENERATED_FORMAT = "markdown-implicit_figures-tex_math_dollars-raw_tex-raw_html-raw_attribute"

LATEX_HEADER = r"""
\usepackage{fvextra}
\DefineVerbatimEnvironment{Highlighting}{Verbatim}{breaklines,breakanywhere,commandchars=\\\{\}}
\fvset{breaklines,breakanywhere}
\usepackage{xcolor}
\definecolor{bevislink}{HTML}{1F4E9A}
"""


class RenderError(Exception):
    pass


# -- Markdown helpers -----------------------------------------------------------

_PUNCT = re.compile(r"([!-/:-@\[-`{-~])")


def esc(text: str) -> str:
    """Escape text for inline Markdown: every ASCII punctuation mark is literal."""
    return _PUNCT.sub(r"\\\1", " ".join(str(text).split()))


def code(text: str) -> str:
    """Inline code span that survives any backticks in `text`."""
    text = " ".join(text.split())
    run = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * (run + 1)
    return f"{fence} {text} {fence}"


def code_block(text: str, lang: str = "") -> str:
    run = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, run + 1)
    return f"{fence}{lang}\n{text.rstrip()}\n{fence}"


def anchor(record: Record) -> str:
    return f"ev-{record.id}-v{record.version}"


def plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def literal(raw: Any) -> str:
    """A JSON key value written as an expression literal."""
    if raw is None:
        return "null"
    if isinstance(raw, bool):
        return "true" if raw else "false"
    if isinstance(raw, str):
        return '"' + raw.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return str(raw)


def column_ref(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else literal(name)


def direct_ref(cell: dict) -> str:
    parts = [cell["evidence"], *(literal(k) for k in cell["key"]), column_ref(cell["column"])]
    return f"cell({', '.join(parts)})"


# -- Pandoc AST ---------------------------------------------------------------


class _AuthorText:
    """Sanitizes author blocks and turns claim spans into evidence markers."""

    def __init__(self, bundle: Bundle):
        self.bundle = bundle
        self.claims = bundle.claims_by_id()
        self.span_text: dict[str, str] = {}
        ids = [i for i, _ in bundle.evidence]
        self.multi_version = {i for i in ids if ids.count(i) > 1}

    def marker(self, claim: dict) -> list[dict]:
        out: list[dict] = [{"t": "Space"}, _str("[")]
        for n, p in enumerate(claim["evidence"]):
            if n:
                out += [_str(","), {"t": "Space"}]
            label = p["id"] + (f" v{p['version']}" if p["id"] in self.multi_version else "")
            target = f"#ev-{p['id']}-v{p['version']}"
            out.append({"t": "Link", "c": [["", [], []], [_str(label)], [target, ""]]})
        if not claim["evidence"]:
            out.append(_str("no evidence"))
        if claim["status"] != "ok":
            out += [_str(";"), {"t": "Space"}, {"t": "Emph", "c": [_str(claim["status"])]}]
        return out + [_str("]")]

    def walk(self, node: Any) -> Any:
        if isinstance(node, list):
            out = []
            for item in node:
                out.extend(self.expand(item))
            return out
        if isinstance(node, dict) and "c" in node:
            return {**node, "c": self.walk(node["c"])}
        return node

    def expand(self, item: Any) -> list:
        if not isinstance(item, dict) or "t" not in item:
            return [self.walk(item)]
        t = item["t"]
        if t in ("RawBlock", "RawInline"):
            return []
        if t == "Image":  # images in prose would read files outside the bundle
            return self.walk(item["c"][1])
        if t == "Link" and not str(item["c"][2][0]).lower().startswith(SAFE_LINKS):
            return self.walk(item["c"][1])
        if t == "Span":
            span_id = item["c"][0][0]
            claim = self.claims.get(span_id)
            walked = self.walk(item)
            walked["c"][0] = [span_id, [], []] if claim else list(NO_ATTR)
            if claim is None:
                return [walked]
            self.span_text[span_id] = stringify(walked["c"][1])
            return [walked, *self.marker(claim)]
        walked = self.walk(item)
        if t in ATTR_AT:  # author attributes would become HTML attributes (onclick=...) in exports
            walked["c"][ATTR_AT[t]] = list(NO_ATTR)
        if t == "Link":
            walked["c"][2] = [walked["c"][2][0], ""]
        return [walked]


# -- the document ---------------------------------------------------------------


class _Renderer:
    def __init__(self, bundle: Bundle, workdir: Path, chart_format: str = "pdf", relative: bool = False):
        """`chart_format` is pdf (for LaTeX) or svg. With `relative`, charts go in
        `workdir/charts/` and images are referenced by relative path, as a file
        written into `workdir` needs."""
        self.b = bundle
        self.workdir = workdir
        self.chart_format = chart_format
        self.relative = relative
        self.cites = citations(bundle)
        self.author = _AuthorText(bundle)
        self.slots: dict[str, list] = {}

    def document(self) -> dict:
        # Author blocks first, so span text is known when the appendix is written.
        for block in self.b.report["blocks"]:
            if block["type"] == "text":
                parsed = pandoc_json(block["text"], AUTHOR_FORMAT)
                self.slots[block["id"]] = self.author.walk(parsed["blocks"])

        parts = [self.header(), *self.body(), self.appendix()]
        doc = pandoc_json("\n\n".join(parts), GENERATED_FORMAT)
        doc["blocks"] = self.fill_slots(doc["blocks"])
        doc["meta"] = {"title": {"t": "MetaInlines", "c": [_str(self.b.title)]}}
        return doc

    def fill_slots(self, blocks: list) -> list:
        out = []
        for block in blocks:
            if block.get("t") == "Div" and block["c"][0][0] == "bevis-pagebreak":
                out.append({"t": "RawBlock", "c": ["latex", "\\clearpage"]})
            elif block.get("t") == "Div" and block["c"][0][0].startswith("slot-"):
                out.extend(self.slots[block["c"][0][0][len("slot-"):]])
            else:
                out.append(block)
        return out

    # header

    def header(self) -> str:
        m = self.b.manifest
        v = m["version"]
        when = datetime.fromisoformat(v["published_at"].replace("Z", "+00:00"))
        by = v["published_by"]
        val = m["validation"]
        lines = [
            f"**Version {v['number']}** · published {esc(when.strftime('%Y-%m-%d %H:%M %Z'))}"
            f" by {esc(by['name'])} ({esc(by['kind'])}{'' if by['verified'] else ', unverified'})",
            f"**Validation at publish:** {plural(val['errors'], 'error')}, "
            f"{plural(val['warnings'], 'warning')}, {val['info']:,} info",
            f"**Disclosure:** {esc(m['disclosure'])}" + {
                "full": "",
                "cited": " " + esc("(only cited rows and columns are included)"),
                "metadata": " " + esc("(no data values are included)"),
            }[m["disclosure"]],
        ]
        if v["override"]:
            lines.append(f"**Published with an override:** {esc(v['override']['reason'])}")
        return "\\\n".join(lines)

    # body

    def body(self) -> list[str]:
        out = []
        blocks = self.b.report["blocks"]
        for i, block in enumerate(blocks):
            t = block["type"]
            if t == "heading":
                if i == 0 and block["level"] == 1 and block["text"].strip() == self.b.title.strip():
                    continue  # the title is already in the header
                out.append("#" * block["level"] + " " + esc(block["text"]) + " {-}")
            elif t == "text":
                out.append(f"::: {{#slot-{block['id']}}}\n:::")
            elif t == "chart":
                out.append(self.chart(block))
            elif t == "image":
                out.append(self.image(block))
        return out

    def evidence_links(self, pins: list[dict]) -> str:
        return "\\[" + ", ".join(
            f"[{esc(p['id'])}](#ev-{p['id']}-v{p['version']})" for p in pins
        ) + "\\]"

    def caption(self, block: dict, note: str = "") -> str:
        text = esc(block.get("caption", "")) if block.get("caption") else ""
        return " ".join(x for x in (f"*{text}*" if text else "", note, self.evidence_links(block["evidence"])) if x)

    def chart(self, block: dict) -> str:
        spec = copy.deepcopy(block["spec"])
        try:
            self._inline_data(spec)
        except _Withheld:
            return self.caption(block, esc("Chart not drawn: its data is not disclosed at this level."))
        name = f"{block['id']}.{self.chart_format}"
        path = self.workdir / "charts" / name if self.relative else self.workdir / f"chart-{name}"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            import vl_convert as vlc

            if self.chart_format == "svg":
                path.write_text(vlc.vegalite_to_svg(spec, allowed_base_urls=[]), encoding="utf-8")
            else:
                path.write_bytes(vlc.vegalite_to_pdf(spec, allowed_base_urls=[]))
        except Exception as e:  # vl-convert raises plain ValueError on bad specs
            raise RenderError(f"chart {block['id']}: {e}") from e
        ref = f"charts/{name}" if self.relative else path.as_posix()
        return f"![{esc(block.get('caption', 'Chart'))}]({ref})\n\n{self.caption(block)}"

    def _inline_data(self, node: Any) -> None:
        if isinstance(node, dict):
            for k, v in list(node.items()):
                if k == "data" and isinstance(v, dict) and "evidence" in v:
                    record = self.b.record(v["evidence"]["id"], v["evidence"]["version"])
                    if record.rows is None:
                        raise _Withheld()
                    node[k] = {"values": [_row_object(record, row) for row in record.rows]}
                else:
                    self._inline_data(v)
        elif isinstance(node, list):
            for v in node:
                self._inline_data(v)

    def image(self, block: dict) -> str:
        path = block["src"] if self.relative else (self.b.root / block["src"]).resolve().as_posix()
        note = "**" + esc("Figure-level evidence only.") + "**"
        return f"![{esc(block['alt'])}]({path}){{width=80%}}\n\n{self.caption(block, note)}"

    # appendix

    def appendix(self) -> str:
        out = ["::: {#bevis-pagebreak}\n:::", "# Evidence {-}"]
        for ident, record in self.b.evidence.items():
            out.append(self.evidence_section(record, self.cites[ident]))
        return "\n\n".join(out)

    def evidence_section(self, record: Record, cited: Citations) -> str:
        doc = record.raw
        title = f"{record.id} v{record.version}"
        parts = [f"## {esc(title)} {{#{anchor(record)} -}}"]

        facts = [
            f"**Source:** {esc(doc['source'])}",
            f"**Run at:** {esc(doc['run_at'])} (reported by the agent)",
            f"**Provenance:** {esc(doc['provenance'])}",
            f"**Rows:** {self.row_summary(doc)}",
            f"**Data hash:** {code(doc['data_hash'])}",
        ]
        if doc["params"]:
            facts.append(f"**Params:** {code(json.dumps(doc['params'], ensure_ascii=False))}")
        parts.append("\\\n".join(facts))
        parts.append("**SQL**")
        parts.append(code_block(doc["sql"], "sql"))
        if doc["assumptions"]:
            parts.append("**Assumptions**")
            parts.append("\n".join(f"- {esc(a)}" for a in doc["assumptions"]))
        derivations = self.derivations(record)
        if derivations:
            parts.append("**Derivations**")
            parts.append(derivations)
        parts.append("**Result**")
        parts.append(self.table(record, cited))
        return "\n\n".join(parts)

    def row_summary(self, doc: dict) -> str:
        text = plural(doc["row_count"], "row")
        if doc.get("partial"):
            text = f"sample of {doc['row_count']:,} of {doc['source_row_count']:,} rows (partial result)"
        return esc(text)

    def derivations(self, record: Record) -> str:
        items = []
        for claim in self.b.report["claims"]:
            pins = {(p["id"], p["version"]) for p in claim["evidence"]}
            if (record.id, record.version) not in pins:
                continue
            span = self.author.span_text.get(claim["id"])
            head = f"[**{esc(claim['id'])}**](#{claim['id']})"
            if span:
                head += f" “{esc(span)}”"
            head += f" — {esc(claim['status'])}"
            lines = [f"- {head}"]
            if claim.get("derivation"):
                lines.append(f"    - {esc(claim['derivation'])}")
            for n in claim["numbers"]:
                lines.append("    - " + self.number_line(n))
            items.append("\n".join(lines))
        return "\n".join(items)

    def number_line(self, n: dict) -> str:
        lead = f"{code(n['text'])} ({esc(n['tier'])}, {esc(n['status'])})"
        if n["tier"] == "direct":
            body = code(direct_ref(n["cell"]))
            try:
                body += " = " + code(_display(resolve_direct(self.b, n["cell"])))
            except Exception:
                pass  # withheld or missing; the status already says so
        elif n["tier"] == "computed":
            body = code(n["expression"])
            if n.get("value") is not None:
                body += " = " + code(n["value"])
        else:
            body = esc(n["derivation"])
        if n.get("message"):
            body += f" — {esc(n['message'])}"
        return f"{lead}: {body}"

    def table(self, record: Record, cited: Citations) -> str:
        doc = record.raw
        if record.rows is None:
            return esc(f"Rows are not disclosed at metadata level ({plural(doc['row_count'], 'row')}).")

        keyed = [(r, _key(record, r)) for r in record.rows]
        if len(record.rows) <= FULL_TABLE_ROWS:
            shown = keyed
        else:
            shown = [(r, k) for r, k in keyed if k in cited.cells]
        cut = len(shown) < doc["row_count"]

        names = record.column_names
        if cut or len(names) > MAX_COLUMNS:
            keep = set(record.key) | cited.claim_columns
            if not cited.claim_columns:
                keep |= cited.chart_columns
            columns = [c for c in names if c in keep]
        else:
            columns = names
        if not columns:
            columns = names[:MAX_COLUMNS]
        idx = [names.index(c) for c in columns]
        hidden = len(names) - len(columns)
        withheld_cols = doc["disclosure"].get("withheld_columns") or []

        notes = []
        if cut:
            note = f"Showing {len(shown):,} of {doc['row_count']:,} rows"
            if len(record.rows) > FULL_TABLE_ROWS:
                note += " (cited rows only)"
            elif len(record.rows) < doc["row_count"]:
                note += " (the rest are not disclosed at this level)"
            notes.append(note + ".")
        if hidden:
            notes.append(f"{plural(hidden, 'column')} not shown: {', '.join(n for n in names if n not in columns)}.")
        if withheld_cols:
            notes.append(f"Not disclosed at this level: {', '.join(withheld_cols)}.")
        if cited.cells:
            notes.append("Cited rows and cells are in bold.")

        if not shown:
            return esc(" ".join(notes) or "No rows.")

        numeric = {c for c, t in record.columns if t in ("integer", "decimal")}
        lines = [
            "| " + " | ".join(esc(c) for c in columns) + " |",
            "|" + "|".join("---:" if c in numeric else ":---" for c in columns) + "|",
        ]
        for row, key in shown:
            bold = cited.cells.get(key, set())
            cells = []
            for i in idx:
                name = names[i]
                text = _display(row[i])
                text = "*null*" if row[i].is_null else esc(text)
                if key in cited.cells and (name in bold or name in record.key):
                    text = f"**{text}**"
                cells.append(text)
            lines.append("| " + " | ".join(cells) + " |")
        table = "\n".join(lines)
        return table + ("\n\n" + esc(" ".join(notes)) if notes else "")


class _Withheld(Exception):
    pass


def _key(record: Record, row: list[Value]) -> tuple:
    return tuple(row[i].canonical() for i in record.key_indexes)


def _display(v: Value) -> str:
    return "null" if v.is_null else v.canonical() or ""


def _row_object(record: Record, row: list[Value]) -> dict:
    """A row as Vega-Lite data. Numbers become floats here only, for drawing."""
    out = {}
    for (name, type_), v in zip(record.columns, row):
        if v.is_null:
            out[name] = None
        elif type_ == "integer":
            out[name] = int(v.py)  # type: ignore[arg-type]
        elif type_ == "decimal":
            out[name] = float(v.py)  # type: ignore[arg-type]
        elif type_ == "boolean":
            out[name] = v.py
        else:
            out[name] = v.canonical()
    return out


# -- entry points -----------------------------------------------------------------


def document_ast(bundle: Bundle, workdir: Path, chart_format: str = "pdf", relative: bool = False) -> dict:
    """The Pandoc AST of the rendered report. Charts are written to `workdir`."""
    return _Renderer(bundle, workdir, chart_format, relative).document()


def render_pdf(bundle: Bundle, out: Path) -> None:
    for tool in ("pandoc", "xelatex"):
        if shutil.which(tool) is None:
            raise RenderError(f"{tool} is not installed")
    with tempfile.TemporaryDirectory(prefix="bevis-pdf-") as tmp:
        work = Path(tmp)
        doc = document_ast(bundle, work)
        (work / "header.tex").write_text(LATEX_HEADER)
        out = out.resolve()
        tmp_out = work / "report.pdf"
        proc = subprocess.run(
            [
                "pandoc", "-f", "json", "-o", str(tmp_out),
                "--pdf-engine=xelatex",
                "-H", str(work / "header.tex"),
                "-V", "geometry:margin=1in",
                "-V", "colorlinks=true",
                "-V", "linkcolor=bevislink",
                "-V", "urlcolor=bevislink",
            ],
            input=json.dumps(doc),
            capture_output=True,
            text=True,
            cwd=work,
        )
        if proc.returncode != 0:
            raise RenderError("PDF rendering failed:\n" + proc.stderr.strip()[-3000:])
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(tmp_out, out)
