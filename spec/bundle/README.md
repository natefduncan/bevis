# Bundle format, schema version 1

A bundle is the one-way export of one published version (SPEC §10). The
JSON files are canonical. Everything else is derived from them.

```
bundle/
  manifest.json                     manifest.schema.json
  report.json                       report.schema.json
  evidence/<id>.v<version>.json     evidence.schema.json
  assets/<file>                     images referenced by image blocks
  report.pdf, report.md, report.html (derived)
```

`common.schema.json` holds shared definitions. All files are UTF-8 JSON.

## Integrity rules

These rules cannot be expressed in JSON Schema. A reader rejects a bundle
that breaks any of them.

1. Every evidence file listed in `manifest.evidence` exists. Its `id` and
   `version` match the manifest entry. No `(id, version)` is listed twice.
2. Every pin in `report.json` (on claims, chart blocks, image blocks, and
   `direct` number cells) refers to a listed evidence version.
3. Each evidence file's `disclosure.level` equals `manifest.disclosure`.
4. Rows (when present) have one value per listed column. Each value fits
   its column type (below). Key columns are listed columns.
5. Keys are unique. An empty key holds at most one row.
6. At `full` disclosure, `rows` is present, `len(rows) == row_count`, and
   the data hash recomputed from the rows equals `data_hash`.
7. At `cited` disclosure, `rows` holds a subset of rows and `columns` a
   subset of columns. The subset always includes the key columns.
   `disclosure.withheld_rows` is `row_count - len(rows)`.
   `disclosure.withheld_columns` names the dropped columns. The hash
   cannot be rechecked and is carried as recorded.
8. At `metadata` disclosure, `rows` is absent.
9. Chart specs carry no inline data and no URLs. Every `data` object in the
   spec, including in layers, is `{"evidence": {"id", "version"}}`, and the
   pinned version is in the block's `evidence` list. A spec may not set
   `datasets`, use an `image` mark, or contain a `url` key anywhere,
   because each of these makes a renderer load something from outside
   the bundle.
10. An image block's `src` is a file inside `assets/`.

Report-level validation (number matching, spans, direction words) is not a
reading rule. Its results are recorded on the version: the manifest's
`validation` counts and the `status` fields in `report.json`.

## Value encoding

| Type | JSON encoding | Canonical text |
|---|---|---|
| `string` | string | the string itself |
| `integer` | number with no fraction or exponent | decimal digits, `-` if negative, no leading zeros |
| `decimal` | string matching `-?[0-9]+(\.[0-9]+)?` | plain decimal: no exponent, no trailing fractional zeros, no trailing `.`, `0` for zero |
| `date` | string `YYYY-MM-DD` | the same |
| `datetime` | string `YYYY-MM-DDTHH:MM:SS[.f{1,6}][Z\|±HH:MM]` | with an offset: converted to UTC and written `…Z`; without one: written without an offset. Fractional seconds lose trailing zeros and are dropped when zero |
| `boolean` | `true` / `false` | `true` / `false` |
| `null` | `null` | n/a |

Any cell may be `null`. A column of type `null` holds only nulls.

Examples: decimal `"4210000.00"` → `4210000`; `"-0.0"` → `0`;
datetime `"2025-03-01T01:00:00.500+01:00"` → `2025-03-01T00:00:00.5Z`.

A key written in `report.json` (a `direct` number's `cell.key`) uses the
JSON encoding of each key column. A key written in an expression uses
expression literals (`spec/expr/README.md`).

## Data hash

`data_hash` is `"sha256:"` followed by the lowercase hex SHA-256 of a UTF-8
JSON text built as follows:

1. `columns`: the list of `[name, type]` pairs in declared order.
2. `rows`: each row as the list of its values' canonical texts, with JSON
   `null` for null. Rows are sorted by key: compare key cells in key order.
   A null sorts before any value, and values compare by the bytes of the
   UTF-8 canonical text. (The sort is byte-wise, so `10` sorts before `9`.
   It only needs to be deterministic.) A record with an empty key has at
   most one row.
3. The text is the JSON array `[columns, rows]` serialized with no
   whitespace, with `,` and `:` as separators, and with non-ASCII characters
   written as UTF-8 rather than `\u` escapes. Strings escape only `"`, `\`,
   and control characters (as `\n`, `\t`, `\b`, `\f`, `\r`, or `\u00XX`).

For example, a record with key `["year"]`, columns `year:integer` and
`revenue:decimal`, and rows `[2025, "3705000"]` and `[2024, "4210000.00"]`
hashes this text:

```
[[["year","integer"],["revenue","decimal"]],[["2024","4210000"],["2025","3705000"]]]
```

The hash covers data only. SQL, source, params, and assumptions are not
part of it, so a re-run with the same data and changed SQL dedups to the
same version (SPEC §3.4).
