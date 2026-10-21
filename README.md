# bevis

A host for auditable reports. A report is prose, charts, and the evidence
behind them. Every claim links to the queries, rows, and arithmetic that
support it. See [SPEC.md](SPEC.md).

## Status

Build steps 1 to 5 (SPEC §12): the bundle format, the expression
language, the PDF renderer, the validator, the host (REST and MCP over
one SQLite file), the web view, and sharing (share links, disclosure
levels, and challenges).

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

## Running the host

```sh
export BEVIS_DATA_DIR=./bevis-data           # the SQLite file and PDF cache live here
uv run bevis keys create claude-analyst      # prints an agent key once
uv run bevis serve                           # REST at /api, MCP at /mcp
```

Or in a container, with the same commands inside it:

```sh
docker build -t bevis .
docker run -d -p 8000:8000 -v bevis-data:/data --name bevis bevis
docker exec bevis bevis keys create claude-analyst
```

An agent connects its MCP client to `http://localhost:8000/mcp/` with the
header `Authorization: Bearer <agent key>`. Creating a report returns a
one-time admin link for the report's human owner. The owner uses it to
make share links (`POST /api/reports/{id}/links`) with a role, a
disclosure level, and a version scope.

A link opens in a browser at `http://localhost:8000/view#key=<link>`. To try
it with the example report:

```sh
BEVIS_URL=http://localhost:8000 BEVIS_KEY=<agent key> uv run python examples/seed_host.py
```

This prints view links for the draft (admin) and for readers at each
disclosure level.

Settings: `BEVIS_DATA_DIR`, `BEVIS_MAX_ROWS` (default 10000), and
`BEVIS_MAX_BYTES` (default 5 MB) per evidence record.
