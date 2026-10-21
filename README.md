# bevis

A host for auditable reports. A report is prose, charts, and the evidence
behind them. Every claim links to the queries, rows, and arithmetic that
support it. See [SPEC.md](SPEC.md).

## Status

All six build steps (SPEC §12): the bundle format, the expression language,
the PDF renderer, the validator, the host (REST and MCP over one SQLite
file), the web view, sharing (share links, disclosure levels, and
challenges), Markdown and HTML export, Python and R clients, and
deployment.

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
uv run bevis export md examples/revenue-review     # GitHub Markdown, charts in charts/
uv run bevis export html examples/revenue-review   # one self-contained HTML file
uv run pytest
```

## Running the host

```sh
export BEVIS_DATA_DIR=./bevis-data           # the SQLite file and PDF cache live here
uv run bevis keys create claude-analyst      # prints an agent key once
uv run bevis serve                           # REST at /api, MCP at /mcp
```

To deploy behind HTTPS with Caddy, see [docs/deploy.md](docs/deploy.md):

```sh
cd deploy && BEVIS_DOMAIN=bevis.example.com docker compose up -d --build
docker compose exec bevis bevis keys create claude-analyst
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

## Clients

Agents can call the API directly or over MCP. For scripts and notebooks:

```python
from bevis.client import Client   # BEVIS_URL and BEVIS_KEY from the environment
Client().run_query("rpt_…", "q-rev-yoy", conn, "SELECT year, SUM(amount) AS revenue …",
                   key=["year"], source="warehouse.orders")
```

```r
library(bevis)                    # clients/r
bevis_run_query(bevis_client(), "rpt_…", "q-rev-yoy", con, "SELECT …",
                key = "year", source = "warehouse.orders")
```

Settings: `BEVIS_DATA_DIR`, `BEVIS_MAX_ROWS` (default 10000), and
`BEVIS_MAX_BYTES` (default 5 MB) per evidence record.
