"""The validator (SPEC §5).

It reads a report and the evidence versions it pins, and returns findings
plus the recomputed status of every claim and number. It does not change
its input; `Result.apply` writes the results into a copy of the report.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
from typing import Any

from . import charts, expr, numbers
from .evidence import LookupFailed, Record
from .markdown import pandoc_json
from .numbers import Displayed
from .values import NUMERIC_TYPES, format_decimal

ERROR, WARNING, INFO = "error", "warning", "info"
_RANK = {ERROR: 3, WARNING: 2, INFO: 1}
_STATUS_RANK = {"error": 3, "warning": 2, "unverified": 1, "ok": 0}

# Info findings that leave a number or claim unverified rather than ok.
_UNVERIFIED = {"number.asserted", "claim.asserted", "eval.withheld"}

CLAIM_SPAN_ID = re.compile(r"c[0-9]+")


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    message: str
    block: str | None = None
    claim: str | None = None
    number: int | None = None  # index into the claim's numbers

    def to_json(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}

    def where(self) -> str:
        parts = []
        if self.claim:
            parts.append(self.claim)
        elif self.block:
            parts.append(self.block)
        if self.number is not None:
            parts.append(f"#{self.number + 1}")
        return " ".join(parts)


@dataclass
class NumberResult:
    status: str = "ok"
    value: str | None = None  # computed tier: the evaluated value
    message: str | None = None


@dataclass
class ClaimResult:
    status: str = "ok"
    numbers: list[NumberResult] = field(default_factory=list)


@dataclass
class Result:
    findings: list[Finding]
    claims: dict[str, ClaimResult]

    def counts(self) -> dict[str, int]:
        return {
            "errors": sum(f.severity == ERROR for f in self.findings),
            "warnings": sum(f.severity == WARNING for f in self.findings),
            "info": sum(f.severity == INFO for f in self.findings),
        }

    @property
    def ok(self) -> bool:
        return not any(f.severity == ERROR for f in self.findings)

    def apply(self, report: dict) -> dict:
        """A copy of `report` with statuses, computed values, and messages filled in."""
        out = copy.deepcopy(report)
        for claim in out["claims"]:
            r = self.claims.get(claim["id"])
            if r is None:
                continue
            claim["status"] = r.status
            for n, nr in zip(claim["numbers"], r.numbers):
                n["status"] = nr.status
                if n["tier"] == "computed":
                    n["value"] = nr.value
                if nr.message:
                    n["message"] = nr.message
                else:
                    n.pop("message", None)
        return out


@dataclass
class Subject:
    """What the validator reads: a report and the evidence versions it pins."""

    report: dict
    records: dict[tuple[str, int], Record]
    latest: dict[str, int]  # newest known version of each evidence id

    @classmethod
    def from_bundle(cls, bundle) -> "Subject":
        latest: dict[str, int] = {}
        for eid, ver in bundle.evidence:
            latest[eid] = max(ver, latest.get(eid, 0))
        return cls(bundle.report, bundle.evidence, latest)


# -- text -----------------------------------------------------------------------

_BLOCK_TAGS = {
    "Plain", "Para", "LineBlock", "CodeBlock", "RawBlock", "BlockQuote", "OrderedList",
    "BulletList", "DefinitionList", "Header", "HorizontalRule", "Table", "Figure", "Div",
}
BOUNDARY = "￼"  # stands in for code and claim spans, so numbers never join across them


@dataclass
class _Span:
    id: str
    block: str
    text: str
    nested: bool = False


@dataclass
class _Text:
    """A text block reduced to plain text: what is outside claim spans, and each span."""

    outside: str
    spans: list[_Span]


class _TextBuilder:
    def __init__(self, block_id: str):
        self.block_id = block_id
        self.spans: list[_Span] = []
        self.depth = 0

    def build(self, node: Any) -> str:
        out: list[str] = []
        self._walk(node, out)
        return "".join(out)

    def _walk(self, node: Any, out: list[str]) -> None:
        if isinstance(node, list):
            for n in node:
                self._walk(n, out)
            return
        if not isinstance(node, dict) or "t" not in node:
            return
        t: str = node["t"]
        c: Any = node.get("c")
        if t in _BLOCK_TAGS:
            out.append("\n")
        if t == "Str":
            out.append(c)
        elif t == "Space":
            out.append(" ")
        elif t in ("SoftBreak", "LineBreak"):
            out.append("\n")
        elif t in ("Code", "CodeBlock", "RawInline", "RawBlock"):
            out.append(BOUNDARY)
        elif t == "Math":
            out.append(c[1])
        elif t == "Quoted":
            q = "“”" if c[0]["t"] == "DoubleQuote" else "‘’"
            out.append(q[0])
            self._walk(c[1], out)
            out.append(q[1])
        elif t == "Span" and CLAIM_SPAN_ID.fullmatch(c[0][0]):
            self.depth += 1
            inner: list[str] = []
            self._walk(c[1], inner)
            self.depth -= 1
            self.spans.append(_Span(c[0][0], self.block_id, "".join(inner), nested=self.depth > 0))
            out.append(BOUNDARY + ("".join(inner) if self.depth > 0 else "") + BOUNDARY)
        elif t in ("Link", "Image", "Span", "Cite"):
            self._walk(c[1], out)
        elif isinstance(c, list):
            self._walk(c, out)
        if t in _BLOCK_TAGS:
            out.append("\n")


@lru_cache(maxsize=4096)
def analyze_text(block_id: str, markdown: str) -> _Text:
    """Cached: the host validates on every write, and most blocks are unchanged."""
    builder = _TextBuilder(block_id)
    outside = builder.build(pandoc_json(markdown)["blocks"])
    return _Text(outside, builder.spans)


# -- numbers outside spans ---------------------------------------------------------

_MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|November|December"
    "|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)
_ISO = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
    r"(?:T[0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]+)?)?(?:Z|[+-][0-9]{2}:[0-9]{2})?)?"
)
_AFTER_MONTH = re.compile(rf"(?<![A-Za-z0-9_])(?:{_MONTHS})\.?[  ]+$")
_BEFORE_MONTH = re.compile(rf"^[  ]+(?:{_MONTHS})(?![A-Za-z0-9_])")


def is_year(d: Displayed) -> bool:
    return d.bare_integer and len(d.text) == 4 and 1900 <= int(d.magnitude) <= 2100


def is_exempt(text: str, d: Displayed, iso_ranges: list[tuple[int, int]]) -> bool:
    """Years, dates, and list markers are not data (SPEC §5)."""
    if is_year(d):
        return True
    if any(a <= d.start and d.end <= b for a, b in iso_ranges):
        return True
    if d.bare_integer and (1 <= d.magnitude <= 31 or is_year(d)):
        if _AFTER_MONTH.search(text[: d.start]) or _BEFORE_MONTH.match(text[d.end :]):
            return True
    line_start = text.rfind("\n", 0, d.start) + 1
    if d.bare_integer and not text[line_start : d.start].strip() and text[d.end : d.end + 1] in (".", ")"):
        return True
    return False


def loose_numbers(text: str) -> list[Displayed]:
    """Numeric tokens in `text` that are not years, dates, or list markers."""
    iso = [m.span() for m in _ISO.finditer(text)]
    return [d for d in numbers.tokens(text) if not is_exempt(text, d, iso)]


# -- the validator ------------------------------------------------------------------


class _Validator:
    def __init__(self, subject: Subject):
        self.s = subject
        self.report = subject.report
        self.findings: list[Finding] = []
        self.claims: dict[str, ClaimResult] = {}

    def add(self, severity: str, code: str, message: str, **where: Any) -> None:
        self.findings.append(Finding(severity, code, message, **where))

    def run(self) -> Result:
        texts = {
            b["id"]: analyze_text(b["id"], b["text"]) for b in self.report["blocks"] if b["type"] == "text"
        }
        spans = self.check_spans(texts)
        self.check_loose_numbers(texts)
        for block in self.report["blocks"]:
            if block["type"] == "chart":
                self.check_chart(block)
            elif block["type"] == "image":
                self.check_pins(block["evidence"], block=block["id"])
                self.add(INFO, "image.figure_level", "image chart: figure-level evidence only", block=block["id"])
        for claim in self.report["claims"]:
            self.check_claim(claim, spans.get(claim["id"]))
        self.findings.sort(key=lambda f: -_RANK[f.severity])
        self.settle_statuses()
        return Result(self.findings, self.claims)

    # spans

    def check_spans(self, texts: dict[str, _Text]) -> dict[str, _Span]:
        claims = {c["id"]: c for c in self.report["claims"]}
        found: dict[str, _Span] = {}
        for text in texts.values():
            for span in text.spans:
                if span.nested:
                    self.add(ERROR, "span.nested", f"claim span {span.id} is inside another claim span",
                             block=span.block)
                if span.id in found:
                    self.add(ERROR, "span.duplicate", f"claim span {span.id} appears more than once",
                             block=span.block)
                    continue
                found[span.id] = span
                if span.id not in claims:
                    self.add(ERROR, "span.no_claim", f"span {{#{span.id}}} has no claim", block=span.block)
        for cid, claim in claims.items():
            span = found.get(cid)
            if span is None:
                self.add(ERROR, "claim.no_span", f"claim {cid} has no span in the text", claim=cid,
                         block=claim["block_id"])
            elif span.block != claim["block_id"]:
                self.add(ERROR, "claim.wrong_block",
                         f"claim {cid} belongs to block {claim['block_id']} but its span is in {span.block}",
                         claim=cid, block=span.block)
        return found

    def check_loose_numbers(self, texts: dict[str, _Text]) -> None:
        non_data: dict[str, set[str]] = {}
        for nd in self.report["non_data"]:
            non_data.setdefault(nd["block_id"], set()).add(nd["text"])
        sources = [(bid, t.outside) for bid, t in texts.items()]
        for block in self.report["blocks"]:
            if block["type"] == "heading":
                sources.append((block["id"], block["text"]))
            elif block.get("caption"):
                sources.append((block["id"], block["caption"]))
        for bid, text in sources:
            for d in loose_numbers(text):
                if d.text in non_data.get(bid, ()):
                    continue
                self.add(WARNING, "text.uncited_number",
                         f"“{d.text}” is outside any claim span; cite it or mark it non-data", block=bid)

    # pins

    def check_pins(self, pins: list[dict], **where: Any) -> dict[str, Record]:
        out: dict[str, Record] = {}
        for p in pins:
            ident = (p["id"], p["version"])
            record = self.s.records.get(ident)
            if record is None:
                self.add(ERROR, "pin.unknown", f"pins {p['id']} v{p['version']}, which is not available", **where)
                continue
            if p["id"] in out:
                if out[p["id"]].version != p["version"]:
                    self.add(ERROR, "pin.two_versions", f"pins two versions of {p['id']}", **where)
                continue
            out[p["id"]] = record
            latest = self.s.latest.get(p["id"], p["version"])
            if latest > p["version"]:
                self.add(WARNING, "pin.stale", f"pins {p['id']} v{p['version']}; v{latest} is newer", **where)
        return out

    # charts

    def check_chart(self, block: dict) -> None:
        bid = block["id"]
        self.check_pins(block["evidence"], block=bid)
        for problem in charts.data_problems(block["spec"], block["evidence"]):
            self.add(ERROR, "chart.data", problem, block=bid)
        for code, message in charts.strictness_problems(block["spec"]):
            self.add(ERROR, code, message, block=bid)

    # claims

    def check_claim(self, claim: dict, span: _Span | None) -> None:
        cid = claim["id"]
        self.claims[cid] = ClaimResult(numbers=[NumberResult() for _ in claim["numbers"]])
        if not claim["evidence"]:
            self.add(ERROR, "claim.no_evidence", f"claim {cid} cites no evidence", claim=cid)
        pinned = self.check_pins(claim["evidence"], claim=cid)
        if not claim["numbers"]:
            self.add(INFO, "claim.asserted", f"claim {cid} states no numbers and is not verified", claim=cid)

        span_tokens = numbers.tokens(span.text) if span else []
        unclaimed = list(span_tokens)
        changes: list[tuple[int, Displayed, Decimal]] = []

        for i, n in enumerate(claim["numbers"]):
            where = {"claim": cid, "number": i}
            shown = numbers.parse(n["text"])
            if span is not None:
                if n["tier"] == "asserted" and not numbers.is_token(n["text"]):
                    # a written-out number ("twelve percent") is never a token: it only has to be there
                    found = bool(n["text"].strip()) and n["text"] in span.text
                else:
                    match = next((t for t in unclaimed if t.text == n["text"]), None)
                    found = match is not None
                    if found:
                        unclaimed.remove(match)
                if not found:
                    self.add(ERROR, "number.not_in_span", f"“{n['text']}” does not appear in the span", **where)
            if n["tier"] == "asserted":
                self.add(INFO, "number.asserted", f"“{n['text']}” is asserted: {n['derivation']}", **where)
                continue
            if shown is None:
                self.add(ERROR, "number.unparseable", f"“{n['text']}” is not a number the validator can read",
                         **where)
                continue
            if n["tier"] == "direct":
                value = self.direct_value(n["cell"], pinned, where)
            else:
                value, node = self.computed_value(n["expression"], pinned, where)
                if value is not None:
                    self.claims[cid].numbers[i].value = format_decimal(value)
                    if node is not None and expr.is_change(node):
                        changes.append((i, shown, value))
            if value is not None:
                self.check_match(shown, value, where)

        if span is not None:
            self.check_unclaimed(span, unclaimed, cid)
            for word in numbers.written_numbers(span.text):
                self.add(WARNING, "text.written_number",
                         f"“{word}” is a written-out number and is not checked", claim=cid)
            self.check_direction(span, changes, cid)

    def direct_value(self, cell: dict, pinned: dict[str, Record], where: dict) -> Decimal | None:
        record = pinned.get(cell["evidence"])
        if record is None or record.version != cell["version"]:
            self.add(ERROR, "pin.unpinned_cell",
                     f"cites {cell['evidence']} v{cell['version']}, which the claim does not pin", **where)
            return None
        try:
            v = record.cell(record.coerce_json_key(cell["key"]), cell["column"])
        except LookupFailed as e:
            self.eval_failed(e.code, str(e), where)
            return None
        if v.is_null or v.type not in NUMERIC_TYPES:
            self.add(ERROR, "number.non_numeric",
                     f"cited cell is {'null' if v.is_null else v.type}, not a number", **where)
            return None
        return v.py  # type: ignore[return-value]

    def computed_value(self, text: str, pinned: dict[str, Record], where: dict):
        try:
            node = expr.parse(text)
        except expr.ParseError as e:
            self.add(ERROR, "expr.parse", f"expression does not parse: {e}", **where)
            return None, None
        try:
            return expr.evaluate(node, expr.resolver(pinned)), node
        except expr.EvalError as e:
            self.eval_failed(e.code, str(e), where)
            return None, node

    def eval_failed(self, code: str, message: str, where: dict) -> None:
        if code == "withheld":
            self.add(INFO, "eval.withheld", "not checkable: the cited rows are not disclosed", **where)
        else:
            self.add(ERROR, f"eval.{code}", message, **where)

    def check_match(self, shown: Displayed, value: Decimal, where: dict) -> None:
        if numbers.matches(shown, value):
            return
        got = numbers.rounded(shown, value)
        if shown.sign and abs(got) == shown.magnitude:
            self.add(ERROR, "number.sign_mismatch",
                     f"“{shown.text}” has the wrong sign; the value is {format_decimal(value)}", **where)
        else:
            self.add(ERROR, "number.mismatch",
                     f"“{shown.text}” does not match {format_decimal(value)}, which displays as "
                     f"{format_decimal(abs(got) if not shown.sign else got)} at that precision", **where)

    def check_unclaimed(self, span: _Span, unclaimed: list[Displayed], cid: str) -> None:
        iso = [m.span() for m in _ISO.finditer(span.text)]
        for d in unclaimed:
            if is_exempt(span.text, d, iso):
                continue
            if d.parseable:
                self.add(ERROR, "span.uncovered_number",
                         f"“{d.text}” is in the span but not among the claim's numbers", claim=cid)
            else:
                self.add(WARNING, "span.unparsed_number",
                         f"“{d.text}” is in the span but cannot be parsed or checked", claim=cid)

    def check_direction(self, span: _Span, changes: list, cid: str) -> None:
        changes = [(i, d, v) for i, d, v in changes if v != 0]
        if not changes:
            return
        down, up = numbers.direction_words(span.text)
        if down and up:
            self.add(INFO, "direction.ambiguous",
                     f"span has both “{down[0]}” and “{up[0]}”; direction not checked (split the span)",
                     claim=cid)
            return
        for i, d, v in changes:
            where = {"claim": cid, "number": i}
            if down and v > 0:
                self.add(ERROR, "direction.contradicts",
                         f"“{down[0]}” but “{d.text}” is an increase ({format_decimal(v)})", **where)
            elif up and v < 0:
                self.add(ERROR, "direction.contradicts",
                         f"“{up[0]}” but “{d.text}” is a decrease ({format_decimal(v)})", **where)
            elif not down and not up and not d.sign:
                self.add(WARNING, "direction.unstated",
                         f"“{d.text}” is a change but the span states no direction", **where)

    # statuses

    def settle_statuses(self) -> None:
        def status_of(findings: list[Finding]) -> str:
            worst = "ok"
            for f in findings:
                s = f.severity if f.severity != INFO else ("unverified" if f.code in _UNVERIFIED else "ok")
                if _STATUS_RANK[s] > _STATUS_RANK[worst]:
                    worst = s
            return worst

        for cid, result in self.claims.items():
            mine = [f for f in self.findings if f.claim == cid]
            for i, nr in enumerate(result.numbers):
                about = [f for f in mine if f.number == i]
                nr.status = status_of(about)
                first = next((f for f in about if f.severity != INFO), None)
                nr.message = first.message if first else None
            claim_level = status_of([f for f in mine if f.number is None])
            result.status = max([claim_level, *(n.status for n in result.numbers)], key=_STATUS_RANK.__getitem__)


def validate(subject: Subject) -> Result:
    return _Validator(subject).run()
