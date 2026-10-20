"""The expression language (SPEC §4; grammar and cases in spec/expr/).

Parsing needs no knowledge of the evidence. Evaluation takes a resolver
that turns a cell reference into a typed value.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, Inexact, Overflow, Underflow
from typing import Callable, Iterator, Mapping, Union

from .evidence import KeyLiteral, LookupFailed, Record
from .values import NUMERIC_TYPES, Value

# Division is the one inexact operation. Its result is rounded to this many
# significant digits, half-even (spec/expr/README.md).
DIVISION_DIGITS = 34

_EXACT = Context(prec=100_000, traps=[Inexact, Overflow, Underflow])
_DIVIDE = Context(prec=DIVISION_DIGITS, rounding=ROUND_HALF_EVEN, traps=[Overflow, Underflow])

ARITY = {  # name -> (min args, max args or None)
    "pct_change": (2, 2),
    "share": (2, 2),
    "sum": (1, None),
    "min": (1, None),
    "max": (1, None),
    "rank": (2, None),
}


class ParseError(Exception):
    def __init__(self, message: str, pos: int):
        super().__init__(f"{message} (at position {pos})")
        self.pos = pos


class EvalError(Exception):
    """Evaluation failed. `code` is stable and used by spec/expr/cases.json."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# -- AST ----------------------------------------------------------------------


@dataclass(frozen=True)
class Num:
    value: Decimal


@dataclass(frozen=True)
class Cell:
    evidence: str
    keys: tuple[KeyLiteral, ...]
    column: str


@dataclass(frozen=True)
class Neg:
    operand: "Node"


@dataclass(frozen=True)
class BinOp:
    op: str  # + - * /
    left: "Node"
    right: "Node"


@dataclass(frozen=True)
class Call:
    name: str
    args: tuple["Node", ...]


Node = Union[Num, Cell, Neg, BinOp, Call]


def cells(node: Node) -> Iterator[Cell]:
    """Every cell reference in the expression, in source order."""
    if isinstance(node, Cell):
        yield node
    elif isinstance(node, Neg):
        yield from cells(node.operand)
    elif isinstance(node, BinOp):
        yield from cells(node.left)
        yield from cells(node.right)
    elif isinstance(node, Call):
        for a in node.args:
            yield from cells(a)


def is_change(node: Node) -> bool:
    """True when the outermost operation is pct_change or subtraction (§4.2).

    Parentheses leave no trace in the tree, so `(a - b)` is a change too.
    """
    return (isinstance(node, Call) and node.name == "pct_change") or (
        isinstance(node, BinOp) and node.op == "-"
    )


# -- parser -------------------------------------------------------------------

_SLUG_START = "abcdefghijklmnopqrstuvwxyz"
_SLUG_CHARS = _SLUG_START + "0123456789-"
_IDENT_START = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_"
_IDENT_CHARS = _IDENT_START + "0123456789"
_DIGITS = "0123456789"
_WS = " \t\r\n"


class _Parser:
    def __init__(self, text: str):
        self.s = text
        self.i = 0

    # low-level helpers
    def ws(self) -> None:
        while self.i < len(self.s) and self.s[self.i] in _WS:
            self.i += 1

    def peek(self) -> str:
        self.ws()
        return self.s[self.i] if self.i < len(self.s) else ""

    def expect(self, ch: str) -> None:
        if self.peek() != ch:
            found = repr(self.s[self.i]) if self.i < len(self.s) else "end of input"
            raise ParseError(f"expected {ch!r}, found {found}", self.i)
        self.i += 1

    def take_while(self, chars: str) -> str:
        start = self.i
        while self.i < len(self.s) and self.s[self.i] in chars:
            self.i += 1
        return self.s[start : self.i]

    # grammar
    def parse(self) -> Node:
        node = self.expr()
        if self.peek():
            raise ParseError(f"unexpected {self.s[self.i]!r}", self.i)
        return node

    def expr(self) -> Node:
        node = self.term()
        while self.peek() in ("+", "-"):
            op = self.s[self.i]
            self.i += 1
            node = BinOp(op, node, self.term())
        return node

    def term(self) -> Node:
        node = self.factor()
        while self.peek() in ("*", "/"):
            op = self.s[self.i]
            self.i += 1
            node = BinOp(op, node, self.factor())
        return node

    def factor(self) -> Node:
        ch = self.peek()
        if not ch:
            raise ParseError("unexpected end of input", self.i)
        if ch == "(":
            self.i += 1
            node = self.expr()
            self.expect(")")
            return node
        if ch == "-":
            self.i += 1
            return Neg(self.factor())
        if ch in _DIGITS:
            return Num(self.number())
        if ch in _IDENT_START:
            start = self.i
            name = self.take_while(_IDENT_CHARS)
            if self.peek() != "(":
                raise ParseError(f"{name!r} is not a function call; a column belongs inside cell()", start)
            self.i += 1
            if name == "cell":
                return self.cell(start)
            if name not in ARITY:
                raise ParseError(f"unknown function {name!r}", start)
            return self.call(name, start)
        raise ParseError(f"unexpected {ch!r}", self.i)

    def number(self) -> Decimal:
        start = self.i
        self.take_while(_DIGITS)
        if self.i < len(self.s) and self.s[self.i] == ".":
            self.i += 1
            if not self.take_while(_DIGITS):
                raise ParseError("a decimal point must be followed by digits", self.i)
        if self.i < len(self.s) and self.s[self.i] in _IDENT_CHARS + ".":
            raise ParseError("malformed number (no exponents or separators)", start)
        return Decimal(self.s[start : self.i])

    def string(self) -> str:
        start = self.i
        self.i += 1  # opening quote
        out = []
        while True:
            if self.i >= len(self.s):
                raise ParseError("unterminated string", start)
            ch = self.s[self.i]
            if ch == '"':
                self.i += 1
                return "".join(out)
            if ch == "\\":
                nxt = self.s[self.i + 1] if self.i + 1 < len(self.s) else ""
                if nxt not in ('"', "\\"):
                    raise ParseError("the only escapes are \\\" and \\\\", self.i)
                out.append(nxt)
                self.i += 2
                continue
            out.append(ch)
            self.i += 1

    def call(self, name: str, start: int) -> Node:
        args: list[Node] = []
        if self.peek() != ")":
            args.append(self.expr())
            while self.peek() == ",":
                self.i += 1
                args.append(self.expr())
        self.expect(")")
        lo, hi = ARITY[name]
        if len(args) < lo or (hi is not None and len(args) > hi):
            want = f"{lo}" if lo == hi else f"at least {lo}"
            raise ParseError(f"{name} takes {want} argument(s), got {len(args)}", start)
        return Call(name, tuple(args))

    def cell(self, start: int) -> Node:
        self.ws()
        slug_at = self.i
        if self.peek() not in _SLUG_START or not self.peek():
            raise ParseError("cell() must start with an evidence id", slug_at)
        slug = self.take_while(_SLUG_CHARS)
        if not 3 <= len(slug) <= 64:
            raise ParseError(f"evidence id {slug!r} must be 3 to 64 characters", slug_at)
        args: list[tuple[str, object, int]] = []  # (kind, value, pos)
        while self.peek() == ",":
            self.i += 1
            args.append(self.cell_arg())
        self.expect(")")
        if not args:
            raise ParseError("cell() needs a column after the evidence id", start)
        *keys, (kind, column, pos) = args
        if kind not in ("identifier", "string"):
            raise ParseError("the last argument of cell() must be a column name", pos)
        literals = []
        for kind, value, pos in keys:
            if kind == "identifier":
                if value not in ("true", "false", "null"):
                    raise ParseError(f"key value {value!r} must be a number, string, true, false, or null", pos)
                literals.append(
                    KeyLiteral("null", None) if value == "null" else KeyLiteral("boolean", value == "true")
                )
            else:
                literals.append(KeyLiteral(kind, value))
        return Cell(slug, tuple(literals), column)  # type: ignore[arg-type]

    def cell_arg(self) -> tuple[str, object, int]:
        ch = self.peek()
        pos = self.i
        if ch == '"':
            return ("string", self.string(), pos)
        if ch == "-":
            self.i += 1
            if self.peek() not in _DIGITS or not self.peek():
                raise ParseError("'-' in a key value must be followed by a number", self.i)
            return ("number", -self.number(), pos)
        if ch and ch in _DIGITS:
            return ("number", self.number(), pos)
        if ch and ch in _IDENT_START:
            return ("identifier", self.take_while(_IDENT_CHARS), pos)
        raise ParseError("expected a key value or column name", pos)


def parse(text: str) -> Node:
    return _Parser(text).parse()


# -- evaluation ---------------------------------------------------------------

Resolver = Callable[[Cell], Value]


def evaluate(node: Node, resolve: Resolver) -> Decimal:
    try:
        return _eval(node, resolve)
    except LookupFailed as e:
        raise EvalError(e.code, str(e)) from e
    except (Inexact, Overflow, Underflow) as e:
        raise EvalError("precision", "result exceeds exact decimal precision") from e


def _divide(a: Decimal, b: Decimal) -> Decimal:
    if b == 0:
        raise EvalError("division_by_zero", "division by zero")
    return _DIVIDE.divide(a, b)


def _eval(node: Node, resolve: Resolver) -> Decimal:
    if isinstance(node, Num):
        return node.value
    if isinstance(node, Cell):
        v = resolve(node)
        if v.is_null:
            raise EvalError("null_cell", f"cell {node.evidence}.{node.column} is null")
        if v.type not in NUMERIC_TYPES:
            raise EvalError("non_numeric", f"cell {node.evidence}.{node.column} is {v.type}, not a number")
        return v.py  # type: ignore[return-value]
    if isinstance(node, Neg):
        return _EXACT.minus(_eval(node.operand, resolve))
    if isinstance(node, BinOp):
        a, b = _eval(node.left, resolve), _eval(node.right, resolve)
        if node.op == "+":
            return _EXACT.add(a, b)
        if node.op == "-":
            return _EXACT.subtract(a, b)
        if node.op == "*":
            return _EXACT.multiply(a, b)
        return _divide(a, b)
    args = [_eval(a, resolve) for a in node.args]
    if node.name == "pct_change":
        old, new = args
        return _divide(_EXACT.subtract(new, old), old)
    if node.name == "share":
        return _divide(args[0], args[1])
    if node.name == "sum":
        total = Decimal(0)
        for a in args:
            total = _EXACT.add(total, a)
        return total
    if node.name == "min":
        return min(args)
    if node.name == "max":
        return max(args)
    # rank: competition ranking, descending
    x, *others = args
    if x not in others:
        raise EvalError("rank_not_found", "rank: x is not among the ranked values")
    return Decimal(1 + sum(1 for e in others if e > x))


def resolver(records: Mapping[str, Record]) -> Resolver:
    """Resolve cells against records keyed by evidence id.

    The caller picks the records, so the version comes from the claim's
    pin (SPEC §3.5), never from the expression.
    """

    def resolve(cell: Cell) -> Value:
        record = records.get(cell.evidence)
        if record is None:
            raise LookupFailed("unknown_evidence", f"no evidence {cell.evidence!r} is pinned")
        return record.cell(record.coerce_key(list(cell.keys)), cell.column)

    return resolve
