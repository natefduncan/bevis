"""An evidence record held in memory: typed rows indexed by key."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import values
from .values import BadValue, Value


class EvidenceError(Exception):
    """An evidence record is malformed."""


class LookupFailed(Exception):
    """A cell reference does not resolve. `code` is stable for test cases."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class KeyLiteral:
    """A key value as written in an expression, before coercion."""

    kind: str  # "number" | "string" | "boolean" | "null"
    value: Any  # Decimal | str | bool | None


@dataclass
class Record:
    id: str
    version: int
    key: list[str]
    columns: list[tuple[str, str]]  # (name, type), in declared order
    rows: list[list[Value]] | None  # None when withheld (metadata disclosure)
    raw: dict = field(repr=False, default_factory=dict)
    _index: dict = field(init=False, repr=False, default_factory=dict)

    def __post_init__(self) -> None:
        names = [c for c, _ in self.columns]
        if len(set(names)) != len(names):
            raise EvidenceError(f"{self.id}: duplicate column names")
        for k in self.key:
            if k not in names:
                raise EvidenceError(f"{self.id}: key column {k!r} is not a column")
        if len(set(self.key)) != len(self.key):
            raise EvidenceError(f"{self.id}: key repeats a column")
        if self.rows is None:
            return
        if not self.key and len(self.rows) > 1:
            raise EvidenceError(f"{self.id}: an empty key is allowed only for a single row")
        idx = self.key_indexes
        for row in self.rows:
            k = values.key_tuple([row[i] for i in idx])
            if k in self._index:
                raise EvidenceError(f"{self.id}: key is not unique (duplicate {list(k)})")
            self._index[k] = row

    @property
    def column_names(self) -> list[str]:
        return [c for c, _ in self.columns]

    @property
    def key_indexes(self) -> list[int]:
        names = self.column_names
        return [names.index(k) for k in self.key]

    def column_type(self, name: str) -> str:
        for c, t in self.columns:
            if c == name:
                return t
        raise KeyError(name)

    @classmethod
    def from_json(cls, doc: dict) -> "Record":
        """Build from the evidence-file encoding (spec/bundle/evidence.schema.json)."""
        columns = [(c["name"], c["type"]) for c in doc["columns"]]
        rows = None
        if doc.get("rows") is not None:
            rows = []
            for n, raw_row in enumerate(doc["rows"]):
                if len(raw_row) != len(columns):
                    raise EvidenceError(
                        f"{doc['id']}: row {n} has {len(raw_row)} values, expected {len(columns)}"
                    )
                try:
                    rows.append([values.from_json(t, v) for (_, t), v in zip(columns, raw_row)])
                except BadValue as e:
                    raise EvidenceError(f"{doc['id']}: row {n}: {e}") from e
        return cls(
            id=doc["id"],
            version=doc.get("version", 1),
            key=list(doc["key"]),
            columns=columns,
            rows=rows,
            raw=doc,
        )

    def compute_hash(self) -> str:
        assert self.rows is not None
        return values.data_hash(self.columns, self.rows, self.key_indexes)

    # -- lookup -------------------------------------------------------------

    def coerce_key(self, literals: list[KeyLiteral]) -> tuple[str | None, ...]:
        if len(literals) != len(self.key):
            raise LookupFailed(
                "key_arity",
                f"{self.id} has {len(self.key)} key column(s) but {len(literals)} key value(s) were given",
            )
        out = []
        for lit, col in zip(literals, self.key):
            out.append(coerce_literal(lit, self.column_type(col), col).canonical())
        return tuple(out)

    def coerce_json_key(self, raw_key: list) -> tuple[str | None, ...]:
        """A key written in report.json uses the row encoding of each column."""
        if len(raw_key) != len(self.key):
            raise LookupFailed(
                "key_arity",
                f"{self.id} has {len(self.key)} key column(s) but {len(raw_key)} key value(s) were given",
            )
        out = []
        for raw, col in zip(raw_key, self.key):
            try:
                out.append(values.from_json(self.column_type(col), raw).canonical())
            except BadValue as e:
                raise LookupFailed("key_coercion", f"key column {col!r}: {e}") from e
        return tuple(out)

    def row(self, key: tuple[str | None, ...]) -> list[Value] | None:
        return self._index.get(key)

    def cell(self, key: tuple[str | None, ...], column: str) -> Value:
        if column not in self.column_names:
            raise LookupFailed("missing_column", f"{self.id} has no column {column!r}")
        if self.rows is None:
            raise LookupFailed("withheld", f"{self.id}: rows are withheld at this disclosure level")
        row = self._index.get(key)
        if row is None:
            raise LookupFailed("missing_row", f"{self.id} has no row with key {list(key)}")
        return row[self.column_names.index(column)]


def coerce_literal(lit: KeyLiteral, type_: str, column: str) -> Value:
    def fail(why: str) -> LookupFailed:
        return LookupFailed("key_coercion", f"key column {column!r} ({type_}): {why}")

    if lit.kind == "null":
        return Value(type_, None)
    if type_ == "integer":
        if lit.kind != "number" or lit.value != lit.value.to_integral_value():
            raise fail(f"expected an integer, got {lit.value!r}")
        return Value(type_, lit.value)
    if type_ == "decimal":
        if lit.kind != "number":
            raise fail(f"expected a number, got {lit.value!r}")
        return Value(type_, lit.value)
    if type_ == "string":
        if lit.kind != "string":
            raise fail(f"expected a string, got {lit.value!r}")
        return Value(type_, lit.value)
    if type_ in ("date", "datetime"):
        if lit.kind != "string":
            raise fail(f"expected an ISO 8601 string, got {lit.value!r}")
        try:
            parsed = values.parse_date(lit.value) if type_ == "date" else values.parse_datetime(lit.value)
        except BadValue as e:
            raise fail(str(e)) from e
        return Value(type_, parsed)
    if type_ == "boolean":
        if lit.kind != "boolean":
            raise fail(f"expected true or false, got {lit.value!r}")
        return Value(type_, lit.value)
    raise fail("only null matches a null column")
