"""Numbers as displayed in text, and matching them to values (SPEC §4.1, §4.2)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Context, Decimal
from functools import cache

from ._spec import spec_dir

_TOKEN = re.compile(
    r"""
    (?<![A-Za-z0-9_.])                      # not glued to a word or a decimal point
    (?P<sign>[+\-−](?=\$?[0-9]))?          # explicit sign, before the number or its $
    (?P<cur>\$)?
    (?P<int>[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)  # thousands separators, or none
    (?P<frac>\.[0-9]+)?
    (?![0-9])
    (?P<suffix>%|[KkMmBb](?![A-Za-z]))?
    (?P<tail>[A-Za-z]+)?                    # "3rd", "10am", "4.2bn": a number, but not one we parse
    """,
    re.X,
)

_UNITS = {"k": 3, "m": 6, "b": 9}
_EXACT = Context(prec=100_000)
_HALF_UP = Context(prec=100_000, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Displayed:
    text: str
    start: int
    end: int
    sign: str | None  # "+" or "-" when written; "−" is read as "-"
    currency: bool
    magnitude: Decimal  # as written, before scaling
    decimals: int
    exponent: int  # 3 for K, 6 for M, 9 for B
    percent: bool
    tail: str | None
    separators: bool

    @property
    def parseable(self) -> bool:
        return self.tail is None

    @property
    def bare_integer(self) -> bool:
        """Digits only: no sign, $, separator, decimal point, or unit."""
        return not (self.sign or self.currency or self.separators or self.decimals
                    or self.exponent or self.percent or self.tail)

    @property
    def signed(self) -> Decimal:
        return -self.magnitude if self.sign == "-" else self.magnitude


def _from_match(m: re.Match) -> Displayed:
    sign = m["sign"]
    if sign == "−":
        sign = "-"
    suffix = m["suffix"] or ""
    return Displayed(
        text=m.group(0),
        start=m.start(),
        end=m.end(),
        sign=sign,
        currency=bool(m["cur"]),
        magnitude=Decimal(m["int"].replace(",", "") + (m["frac"] or "")),
        decimals=len(m["frac"]) - 1 if m["frac"] else 0,
        exponent=_UNITS.get(suffix.lower(), 0),
        percent=suffix == "%",
        tail=m["tail"],
        separators="," in m["int"],
    )


def tokens(text: str) -> list[Displayed]:
    """Every numeric token in running text."""
    return [_from_match(m) for m in _TOKEN.finditer(text)]


def is_token(text: str) -> bool:
    """Whether `text` is exactly one numeric token, parseable or not."""
    return _TOKEN.fullmatch(text.strip()) is not None


def parse(text: str) -> Displayed | None:
    """One displayed number, or None when `text` is not exactly one parseable number."""
    m = _TOKEN.fullmatch(text.strip())
    if m is None:
        return None
    d = _from_match(m)
    return d if d.parseable else None


def scaled(d: Displayed, value: Decimal) -> Decimal:
    """`value` in the displayed unit: x100 for a percentage, /1000 for K, and so on."""
    if d.percent:
        return value.scaleb(2, _EXACT)
    return value.scaleb(-d.exponent, _EXACT)


def rounded(d: Displayed, value: Decimal) -> Decimal:
    """`value` rounded half-up (away from zero) to the displayed precision."""
    return _HALF_UP.quantize(scaled(d, value), Decimal(1).scaleb(-d.decimals))


def matches(d: Displayed, value: Decimal) -> bool:
    """Magnitude only, unless the display carries an explicit sign."""
    r = rounded(d, value)
    return r == d.signed if d.sign else abs(r) == d.magnitude


# -- lexicons -------------------------------------------------------------------


@cache
def _lexicon(name: str) -> dict:
    return json.loads((spec_dir("expr") / name).read_text())


def _word_re(words: list[str]) -> re.Pattern:
    return re.compile(r"(?<!\w)(" + "|".join(map(re.escape, words)) + r")(?!\w)", re.I)


@cache
def _direction_res() -> tuple[re.Pattern, re.Pattern]:
    lex = _lexicon("direction.json")
    return _word_re(lex["down"]), _word_re(lex["up"])


def direction_words(text: str) -> tuple[list[str], list[str]]:
    """(down words, up words) found in `text`, as written."""
    down, up = _direction_res()
    return [m.group(0) for m in down.finditer(text)], [m.group(0) for m in up.finditer(text)]


@cache
def _written_re() -> re.Pattern:
    return _word_re(_lexicon("matching.json")["written_numbers"])


def written_numbers(text: str) -> list[str]:
    return [m.group(0) for m in _written_re().finditer(text)]
