"""Which rows and columns of each evidence record a report cites."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator

from . import expr
from .bundle import Bundle
from .evidence import LookupFailed

Ident = tuple[str, int]
Key = tuple[str | None, ...]


@dataclass
class Citations:
    # Cells cited by claims: row key -> columns.
    cells: dict[Key, set[str]] = field(default_factory=dict)
    # Columns a chart draws from. Charts use every row, so these apply to all rows.
    chart_columns: set[str] = field(default_factory=set)
    charted: bool = False

    def add(self, key: Key, column: str) -> None:
        self.cells.setdefault(key, set()).add(column)

    @property
    def claim_columns(self) -> set[str]:
        return set().union(*self.cells.values()) if self.cells else set()


def chart_fields(spec: Any) -> Iterator[str]:
    """Field names a Vega-Lite spec reads, from encodings and filters."""
    if isinstance(spec, dict):
        for k, v in spec.items():
            if k == "field" and isinstance(v, str):
                yield v
            elif k == "filter" and isinstance(v, dict) and isinstance(v.get("field"), str):
                yield v["field"]
            else:
                yield from chart_fields(v)
    elif isinstance(spec, list):
        for v in spec:
            yield from chart_fields(v)


def citations(bundle: Bundle) -> dict[Ident, Citations]:
    out: dict[Ident, Citations] = {ident: Citations() for ident in bundle.evidence}

    for claim in bundle.report["claims"]:
        pinned = bundle.pinned(claim["evidence"])
        for number in claim["numbers"]:
            if number["tier"] == "direct":
                c = number["cell"]
                record = bundle.record(c["evidence"], c["version"])
                try:
                    key = record.coerce_json_key(c["key"])
                except LookupFailed:
                    continue
                out[(record.id, record.version)].add(key, c["column"])
            elif number["tier"] == "computed":
                try:
                    node = expr.parse(number["expression"])
                except expr.ParseError:
                    continue
                for cell in expr.cells(node):
                    record = pinned.get(cell.evidence)
                    if record is None:
                        continue
                    try:
                        key = record.coerce_key(list(cell.keys))
                    except LookupFailed:
                        continue
                    out[(record.id, record.version)].add(key, cell.column)

    for block in bundle.report["blocks"]:
        if block["type"] != "chart":
            continue
        fields = set(chart_fields(block["spec"]))
        for pin in block["evidence"]:
            c = out[(pin["id"], pin["version"])]
            c.charted = True
            c.chart_columns |= fields & set(bundle.record(pin["id"], pin["version"]).column_names)
    return out
