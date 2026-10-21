"""Cut evidence down to a disclosure level (SPEC §7)."""

from __future__ import annotations

import copy
from pathlib import Path

from .bundle import Bundle
from .cited import citations
from .evidence import Record

Ident = tuple[str, int]


def restrict(report: dict, docs: dict[Ident, dict], level: str) -> dict[Ident, dict]:
    """Copies of `docs` (evidence-file encoding, full disclosure) at `level`.

    At `cited`, a record keeps the rows and columns claims cite. A chart cites
    every row it draws and the columns it encodes. Key columns always stay.
    """
    out = {ident: copy.deepcopy(doc) for ident, doc in docs.items()}
    for doc in out.values():
        doc["disclosure"] = {"level": level, "withheld_rows": 0}
    if level == "full":
        return out
    if level == "metadata":
        for doc in out.values():
            doc.pop("rows", None)
            doc["disclosure"]["withheld_rows"] = doc["row_count"]
        return out

    records = {ident: Record.from_json(doc) for ident, doc in docs.items()}
    cites = citations(Bundle(Path("."), {}, report, records))
    for ident, doc in out.items():
        c = cites[ident]
        record = records[ident]
        names = record.column_names
        keep = [n for n in names if n in set(record.key) | c.claim_columns | c.chart_columns]
        rows = []
        for raw, typed in zip(doc["rows"], record.rows or []):
            key = tuple(typed[i].canonical() for i in record.key_indexes)
            if c.charted or key in c.cells:
                rows.append([raw[names.index(n)] for n in keep])
        doc["columns"] = [col for col in doc["columns"] if col["name"] in keep]
        doc["rows"] = rows
        doc["disclosure"] = {
            "level": level,
            "withheld_rows": doc["row_count"] - len(rows),
            "withheld_columns": [n for n in names if n not in keep],
        }
    return out
