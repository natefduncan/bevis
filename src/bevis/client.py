"""A thin Python client for a Bevis host (SPEC §9).

    from bevis.client import Client
    bevis = Client()  # BEVIS_URL and BEVIS_KEY from the environment
    bevis.run_query("rpt_…", "q-rev-yoy", conn, "SELECT year, SUM(amount) AS revenue …",
                    key=["year"], source="warehouse.orders")

The HTTP API is the contract; this only saves typing. It runs the query
where you already run queries, reads the column types off the values, and
posts the rows in the evidence encoding (decimals as exact strings).
"""

from __future__ import annotations

import os
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Sequence

import httpx

from .values import format_decimal


class BevisError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = details


# -- typing rows ----------------------------------------------------------------------


def infer_type(values: Iterable[Any]) -> str:
    """The evidence type of a column, from its non-null values."""
    seen = set()
    for v in values:
        if v is None:
            continue
        if isinstance(v, bool):
            seen.add("boolean")
        elif isinstance(v, int):
            seen.add("integer")
        elif isinstance(v, (Decimal, float)):
            seen.add("decimal")
        elif isinstance(v, datetime):  # before date: a datetime is a date
            seen.add("datetime")
        elif isinstance(v, date):
            seen.add("date")
        elif isinstance(v, str):
            seen.add("string")
        else:
            raise TypeError(f"cannot store a {type(v).__name__} value ({v!r}); convert it in SQL")
    if not seen:
        return "null"
    if seen <= {"integer", "decimal"}:
        return "decimal" if "decimal" in seen else "integer"
    if len(seen) > 1:
        raise TypeError(f"a column mixes {', '.join(sorted(seen))}; give its type in `types`")
    return seen.pop()


def encode(value: Any, type_: str) -> Any:
    """One value in the evidence encoding (spec/bundle/README.md)."""
    if value is None:
        return None
    if type_ == "integer":
        return int(value)
    if type_ == "decimal":
        if isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                raise ValueError(f"{value} is not a decimal")
            value = Decimal(repr(value))  # the float's shortest exact-looking form, e.g. 0.1 not 0.1000000000000000055
        return format_decimal(Decimal(value) if not isinstance(value, Decimal) else value)
    if type_ in ("date", "datetime"):
        return value if isinstance(value, str) else value.isoformat()
    if type_ == "boolean":
        return bool(value)
    return str(value)


def to_evidence(columns: Sequence[str], rows: Sequence[Sequence[Any]],
                types: dict[str, str] | None = None) -> tuple[list[dict], list[list]]:
    """(columns with types, encoded rows) for add_evidence."""
    types = dict(types or {})
    out_cols = []
    for i, name in enumerate(columns):
        t = types.get(name) or infer_type(r[i] for r in rows)
        out_cols.append({"name": name, "type": t})
    out_rows = [[encode(v, c["type"]) for v, c in zip(row, out_cols)] for row in rows]
    return out_cols, out_rows


# -- the client -------------------------------------------------------------------------


class Client:
    def __init__(self, url: str | None = None, key: str | None = None, *, http: httpx.Client | None = None,
                 timeout: float = 120):
        self.url = (url or os.environ.get("BEVIS_URL") or "http://localhost:8000").rstrip("/")
        self.key = key or os.environ.get("BEVIS_KEY")
        if not self.key:
            raise ValueError("pass key= or set BEVIS_KEY to an agent key")
        self.http = http or httpx.Client(timeout=timeout)

    def _call(self, method: str, path: str, body: Any = None, params: dict | None = None) -> Any:
        r = self.http.request(method, self.url + path, json=body, params=params,
                              headers={"Authorization": f"Bearer {self.key}"})
        if r.is_error:
            try:
                err = r.json()["error"]
            except (ValueError, KeyError, TypeError):
                raise BevisError(r.status_code, "http_error", r.text[:500]) from None
            raise BevisError(r.status_code, err["code"], err["message"], err.get("details"))
        return r.json()

    # evidence

    def add_evidence(self, report_id: str, id: str, *, columns: list[dict], rows: list[list], key: list[str],
                     sql: str, source: str, params: dict | None = None, assumptions: Sequence[str] = (),
                     partial: bool | None = None, source_row_count: int | None = None,
                     run_at: str | None = None, create_only: bool = False) -> dict:
        body: dict[str, Any] = {
            "id": id, "source": source, "sql": sql, "params": params or {}, "assumptions": list(assumptions),
            "key": list(key), "columns": columns, "rows": rows,
        }
        if partial is not None:
            body["partial"] = partial
        if source_row_count is not None:
            body["source_row_count"] = source_row_count
        if run_at:
            body["run_at"] = run_at
        if create_only:
            body["create_only"] = True
        return self._call("POST", f"/api/reports/{report_id}/evidence", body)

    def add_rows(self, report_id: str, id: str, columns: Sequence[str], rows: Sequence[Sequence[Any]], *,
                 types: dict[str, str] | None = None, **kwargs: Any) -> dict:
        """Submit Python values; column types are read off the values unless given in `types`."""
        cols, encoded = to_evidence(columns, rows, types)
        return self.add_evidence(report_id, id, columns=cols, rows=encoded, **kwargs)

    def run_query(self, report_id: str, id: str, conn: Any, sql: str, *, key: list[str], source: str,
                  params: dict | Sequence | None = None, types: dict[str, str] | None = None,
                  **kwargs: Any) -> dict:
        """Run `sql` on a DB-API connection and submit the result as evidence `id`."""
        run_at = datetime.now().astimezone().replace(microsecond=0).isoformat()
        cur = conn.cursor()
        try:
            cur.execute(sql, params if params is not None else ())
            columns = [d[0] for d in cur.description]
            rows = [tuple(r) for r in cur.fetchall()]
        finally:
            cur.close()
        recorded = params if isinstance(params, dict) else ({"args": list(params)} if params else {})
        return self.add_rows(report_id, id, columns, rows, types=types, key=key, sql=sql, source=source,
                             params=recorded, run_at=kwargs.pop("run_at", run_at), **kwargs)

    # reports

    def create_report(self, title: str, owner: str | None = None) -> dict:
        return self._call("POST", "/api/reports", {"title": title, "owner": owner})

    def get_report(self, report_id: str, version: int | None = None) -> dict:
        return self._call("GET", f"/api/reports/{report_id}", params={"version": version} if version else None)

    def edit(self, report_id: str, ops: list[dict], base_revision: int | None = None) -> dict:
        return self._call("POST", f"/api/reports/{report_id}/edits", {"ops": ops, "base_revision": base_revision})

    def validate(self, report_id: str, ops: list[dict] | None = None) -> dict:
        return self._call("POST", f"/api/reports/{report_id}/validate", {"ops": ops} if ops else None)

    def suggest(self, report_id: str, ops: list[dict], message: str = "", base_revision: int | None = None) -> dict:
        return self._call("POST", f"/api/reports/{report_id}/suggestions",
                          {"ops": ops, "message": message, "base_revision": base_revision})

    def publish(self, report_id: str, override_reason: str | None = None) -> dict:
        return self._call("POST", f"/api/reports/{report_id}/publish", {"override_reason": override_reason})

    # challenges

    def list_open_challenges(self, report_id: str | None = None) -> list[dict]:
        return self._call("GET", "/api/challenges/open", params={"report_id": report_id} if report_id else None)

    def reply_to_challenge(self, report_id: str, challenge_id: str, body: str, ops: list[dict] | None = None,
                           message: str = "") -> dict:
        return self._call("POST", f"/api/reports/{report_id}/challenges/{challenge_id}/replies",
                          {"body": body, "ops": ops, "message": message})
