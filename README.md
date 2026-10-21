# bevis

A host for auditable reports. A report is prose, charts, and the evidence
behind them. Every claim links to the queries, rows, and arithmetic that
support it. See [SPEC.md](SPEC.md).

## Status

Build steps 1 and 2 (SPEC §12): the bundle format, the expression
language, the PDF renderer, and the validator.

- `spec/bundle/`: JSON Schemas, integrity rules, value encodings, and the
  data hash for export bundles.
- `spec/expr/`: the expression grammar and evaluation rules, how displayed
  numbers are read and matched, the direction lexicon, and shared test
  cases for other implementations.
- `examples/revenue-review/`: an example bundle, written by
  `examples/build_revenue_review.py`.

## Usage

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), Pandoc, and XeLaTeX.

```sh
uv sync
uv run bevis validate examples/revenue-review      # check claims against evidence
uv run bevis export pdf examples/revenue-review    # writes examples/revenue-review/report.pdf
uv run pytest
```
