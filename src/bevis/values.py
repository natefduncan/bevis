"""Typed evidence values: parsing, canonical text form, and the data hash.

The canonical forms and the hash serialization are specified in
spec/bundle/README.md. Keep the two in step.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Union

TYPES = ("string", "integer", "decimal", "date", "datetime", "boolean", "null")
NUMERIC_TYPES = ("integer", "decimal")

_DECIMAL_RE = re.compile(r"-?[0-9]+(\.[0-9]+)?")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_DATETIME_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?(Z|[+-][0-9]{2}:[0-9]{2})?"
)

# A typed value. Integers and decimals are Decimal so arithmetic is exact;
# dates are date, datetimes are datetime, null is None.
Py = Union[str, Decimal, date, datetime, bool, None]


class BadValue(ValueError):
    """A value does not fit its declared type."""


@dataclass(frozen=True)
class Value:
    type: str
    py: Py

    @property
    def is_null(self) -> bool:
        return self.py is None

    def canonical(self) -> str | None:
        """Canonical text form, or None for null."""
        return canonical_text(self.type, self.py)

    def to_json(self) -> Any:
        """The row encoding used in evidence files."""
        if self.py is None:
            return None
        if self.type == "integer":
            return int(self.py)  # type: ignore[arg-type]
        if self.type == "boolean":
            return self.py
        return self.canonical()


def _normalize_decimal(d: Decimal) -> str:
    if d == 0:
        return "0"
    text = format(d, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def format_decimal(d: Decimal) -> str:
    """Plain decimal text: no exponent, no trailing fractional zeros."""
    return _normalize_decimal(d)


def canonical_text(type_: str, py: Py) -> str | None:
    if py is None:
        return None
    if type_ in NUMERIC_TYPES:
        return _normalize_decimal(py)  # type: ignore[arg-type]
    if type_ == "boolean":
        return "true" if py else "false"
    if type_ == "date":
        return py.isoformat()  # type: ignore[union-attr]
    if type_ == "datetime":
        assert isinstance(py, datetime)
        if py.tzinfo is not None:
            py = py.astimezone(timezone.utc).replace(tzinfo=None)
            suffix = "Z"
        else:
            suffix = ""
        text = py.strftime("%Y-%m-%dT%H:%M:%S")
        if py.microsecond:
            text += "." + f"{py.microsecond:06d}".rstrip("0")
        return text + suffix
    return py  # type: ignore[return-value]


def parse_date(text: str) -> date:
    if not _DATE_RE.fullmatch(text):
        raise BadValue(f"not an ISO 8601 date: {text!r}")
    try:
        return date.fromisoformat(text)
    except ValueError as e:
        raise BadValue(f"not a valid date: {text!r}") from e


def parse_datetime(text: str) -> datetime:
    if not _DATETIME_RE.fullmatch(text):
        raise BadValue(f"not an ISO 8601 datetime: {text!r}")
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as e:
        raise BadValue(f"not a valid datetime: {text!r}") from e


def parse_decimal(text: str) -> Decimal:
    if not _DECIMAL_RE.fullmatch(text):
        raise BadValue(f"not a decimal string: {text!r}")
    try:
        return Decimal(text)
    except InvalidOperation as e:  # pragma: no cover - regex already guards
        raise BadValue(f"not a decimal: {text!r}") from e


def from_json(type_: str, raw: Any) -> Value:
    """Parse one cell from its evidence-file encoding."""
    if raw is None:
        return Value(type_, None)
    if type_ == "string":
        if not isinstance(raw, str):
            raise BadValue(f"expected a string, got {raw!r}")
        return Value(type_, raw)
    if type_ == "integer":
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise BadValue(f"expected an integer, got {raw!r}")
        return Value(type_, Decimal(raw))
    if type_ == "decimal":
        if not isinstance(raw, str):
            raise BadValue(f"decimals are serialized as strings, got {raw!r}")
        return Value(type_, parse_decimal(raw))
    if type_ == "date":
        if not isinstance(raw, str):
            raise BadValue(f"expected a date string, got {raw!r}")
        return Value(type_, parse_date(raw))
    if type_ == "datetime":
        if not isinstance(raw, str):
            raise BadValue(f"expected a datetime string, got {raw!r}")
        return Value(type_, parse_datetime(raw))
    if type_ == "boolean":
        if not isinstance(raw, bool):
            raise BadValue(f"expected a boolean, got {raw!r}")
        return Value(type_, raw)
    if type_ == "null":
        raise BadValue(f"a null column holds only nulls, got {raw!r}")
    raise BadValue(f"unknown type {type_!r}")


def key_tuple(values: list[Value]) -> tuple[str | None, ...]:
    """Hashable identity of a row key: its canonical texts."""
    return tuple(v.canonical() for v in values)


def _sort_key(key: tuple[str | None, ...]) -> tuple:
    return tuple((0, b"") if k is None else (1, k.encode("utf-8")) for k in key)


def data_hash(
    columns: list[tuple[str, str]],
    rows: list[list[Value]],
    key_indexes: list[int],
) -> str:
    """SHA-256 over the canonical serialization (spec/bundle/README.md)."""
    ordered = sorted(rows, key=lambda r: _sort_key(key_tuple([r[i] for i in key_indexes])))
    payload = [
        [[name, type_] for name, type_ in columns],
        [[v.canonical() for v in row] for row in ordered],
    ]
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
