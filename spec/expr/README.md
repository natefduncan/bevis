# Expression language

This directory is the normative definition of the Bevis expression
language (SPEC §4). An implementation conforms when it passes every case in
`cases.json`.

## Grammar

```
expr     := term (("+" | "-") term)*
term     := factor (("*" | "/") factor)*
factor   := number | cell | call | "(" expr ")" | "-" factor
cell     := "cell" "(" evidence_id ("," key_value)* "," column ")"
call     := name "(" [expr ("," expr)*] ")"
name     := "pct_change" | "share" | "sum" | "min" | "max" | "rank"

evidence_id := [a-z][a-z0-9-]{2,63}
column      := identifier | string
identifier  := [A-Za-z_][A-Za-z0-9_]*
key_value   := number | "-" number | string | "true" | "false" | "null"
number      := [0-9]+ ("." [0-9]+)?
string      := '"' ( strchar | escape )* '"'
strchar     := any character except '"' and '\'
escape      := '\' '"' | '\' '\'
```

Single quotes in the grammar enclose one literal character.

## Lexical rules

- Whitespace (space, tab, CR, LF) between tokens is ignored. Keywords and
  function names are case-sensitive.
- The first argument of `cell` is read as a slug, so its hyphens are not
  minus signs. A slug is read greedily over `[a-z0-9-]`.
- The last argument of `cell` is the column. The arguments between the
  slug and the column are key values. `true`, `false`, and `null` are key
  values in key position and column names in last position.
- A number is followed by neither a letter, digit, `_`, nor `.`, so `1e5`,
  `2x`, and `1.2.3` are parse errors. Leading zeros are allowed.
- Only ASCII `-` is minus. `−` (U+2212) is a parse error here, though the
  number matcher (SPEC §4.1) accepts it in displayed text.

## Static checks (parse errors)

Function arity is checked at parse time:

| Function | Arguments |
|---|---|
| `pct_change`, `share` | exactly 2 |
| `sum`, `min`, `max` | 1 or more |
| `rank` | 2 or more (`x` and at least one value) |

## Evaluation

- Values are exact decimals. `+`, `-`, `*`, unary minus, `sum`, `min`, and
  `max` are exact.
- **Division is rounded to 34 significant digits, half-even**, the
  IEEE 754 decimal128 precision. It applies to `/`, `pct_change`, and
  `share`. Each division rounds on its own, so `(1 / 3) * 3` is
  `0.9999999999999999999999999999999999`. Matching to displayed text
  (SPEC §4.1) then applies half-up rounding at the displayed precision
  to this 34-digit result, never to the exact quotient, so every
  implementation reaches the same verdict.
- Results are compared as plain decimal text: no exponent, no trailing
  fractional zeros, no `-0`.
- Key values are coerced to the key column's type (SPEC §4). An integer
  key accepts any integral number, so `2024.0` matches `2024`. Datetimes
  compare as instants: `2025-03-01T01:00:00+01:00` matches
  `2025-03-01T00:00:00Z`. A datetime without an offset matches only
  another datetime without an offset.
- `null` as a key value matches a null key cell of any type.

## Error codes

Evaluation errors carry a stable code, used by `cases.json`:

| Code | Meaning |
|---|---|
| `unknown_evidence` | the slug is not pinned by the claim or chart |
| `key_arity` | number of key values differs from the record's key arity |
| `key_coercion` | a key value cannot be coerced to its column type |
| `missing_row` | no row has that key |
| `missing_column` | the record has no such column |
| `withheld` | rows are not present at this disclosure level |
| `null_cell` | a null cell is used in arithmetic |
| `non_numeric` | a non-numeric cell is used in arithmetic |
| `division_by_zero` | a divisor is zero |
| `rank_not_found` | `rank`'s `x` equals none of the ranked values |
| `precision` | an exact result needs more than 100,000 digits |

## `cases.json`

```json
{
  "evidence": { "<slug>": { "key": [...], "columns": [...], "rows": [...] } },
  "cases": [ { "name": "...", "expr": "...", "expect": { "value": "..." } } ]
}
```

`evidence` uses the evidence-file encoding (`spec/bundle/`). Each case
evaluates `expr` with every fixture pinned. `expect` is one of:

- `{"value": "<decimal text>"}`: the result, as plain decimal text.
- `{"error": "parse"}`: parsing fails.
- `{"error": "<code>"}`: parsing succeeds and evaluation fails with that
  code.

## Reading displayed numbers: `matching.json`

A **numeric token** in text is read with this pattern (Python `re.X`
syntax):

```
(?<![A-Za-z0-9_.])                        not glued to a word or a decimal point
(?P<sign>[+\-−](?=\$?[0-9]))?             explicit sign, before the number or its $
(?P<cur>\$)?
(?P<int>[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)  thousands separators, or none
(?P<frac>\.[0-9]+)?
(?![0-9])
(?P<suffix>%|[KkMmBb](?![A-Za-z]))?
(?P<tail>[A-Za-z]+)?
```

- Every class is ASCII. Only `0`–`9` are digits, so text in other
  digits (`٣٠`, `１２%`) holds no token and no claim number can be written
  in them. A letter or digit outside ASCII does not glue to a token
  either: `é5` holds the token `5`.
- A token with a `tail` (`3rd`, `10am`, `4.2bn`) is a number but is not
  parsed, so it cannot be matched.
- `Q4`, `FY2025`, and `v1` are not tokens, because a letter comes right
  before the digits.
- A `-` right after a digit or a letter is a hyphen, not a sign. So
  `2024-2025` holds two unsigned tokens.
- A claim number's `text` must be exactly one token with no tail.

**Matching** (SPEC §4.1) scales the value to the displayed unit: ×100 for
`%`, ÷10³/10⁶/10⁹ for `K`/`M`/`B`. It then rounds **half-up, away from
zero**, to the displayed decimals. Without an explicit sign, magnitudes are
compared. With one, signed values are compared, and −0 equals 0.

`matching.json` holds:

- `tokens`: text, and the tokens read from it, in order.
- `cases`: a displayed number, a value, and whether they match. `match: null`
  means the display does not parse as a claim number.
- `written_numbers`: words that produce the "written-out number" warning.
  They match whole words, case-insensitively.

## Direction words: `direction.json`

`down` and `up` are the direction lexicon (SPEC §4.2). Words match whole
words, case-insensitively, only in the listed forms.

Each case is one claim span holding one number. `text` is the number as
displayed, and `value` is its evaluated value. With `change: true` the
number is a computed change, and with `change: false` it is a direct cell.
`expect` lists the codes of every finding the validator reports for that
span. An empty list means the span is clean.
