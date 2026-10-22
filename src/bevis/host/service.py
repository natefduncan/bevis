"""The host's operations: one contract behind REST and MCP (SPEC §2, §9).

Every method takes the calling `Principal` first and checks its role. The
methods are synchronous; SQLite does the work.
"""

from __future__ import annotations

import copy
import json
import re
import sqlite3
import tempfile
from collections import OrderedDict
from pathlib import Path
from typing import Any

from .. import bundle as bundle_mod
from .. import export
from .. import values
from ..charts import data_values
from ..cited import number_cells
from ..disclosure import restrict
from ..evidence import EvidenceError, Record
from ..markdown import to_html
from ..derived import render_html
from ..pdf import RenderError, render_pdf
from ..validate import Subject, validate
from . import ops as ops_mod
from .auth import DISCLOSURES, RANK, ROLES, Principal, hash_secret, new_id, new_secret
from .config import Settings
from .db import Database, dumps, loads, now

SLUG = re.compile(r"[a-z][a-z0-9-]{2,63}")
TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
EVIDENCE_FIELDS = {
    "id", "source", "sql", "params", "assumptions", "key", "columns", "rows",
    "run_at", "partial", "source_row_count", "create_only",
}
TARGET_KINDS = ("block", "claim")


class HostError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details

    def to_json(self) -> dict:
        out = {"code": self.code, "message": self.message}
        if self.details is not None:
            out["details"] = self.details
        return out


def _not_found(what: str) -> HostError:
    return HostError(404, "not_found", f"{what} not found")


class Service:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.db_path)
        self._records: OrderedDict[tuple[str, str, int], Record] = OrderedDict()

    # -- credentials ----------------------------------------------------------------

    def create_agent_key(self, name: str) -> dict:
        """Not exposed over the API: run `bevis keys create`."""
        secret = new_secret("bvk")
        cred_id = new_id("key")
        with self.db.write() as conn:
            conn.execute(
                "INSERT INTO credentials (id, secret_hash, kind, name, author_kind, created_at)"
                " VALUES (?, ?, 'agent_key', ?, 'agent', ?)",
                (cred_id, hash_secret(secret), name, now()),
            )
        return {"id": cred_id, "name": name, "secret": secret}

    def list_agent_keys(self) -> list[dict]:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT id, name, created_at, revoked_at FROM credentials WHERE kind = 'agent_key' ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def revoke_agent_key(self, cred_id: str) -> None:
        with self.db.write() as conn:
            cur = conn.execute(
                "UPDATE credentials SET revoked_at = ? WHERE id = ? AND kind = 'agent_key' AND revoked_at IS NULL",
                (now(), cred_id),
            )
            if cur.rowcount == 0:
                raise _not_found(f"agent key {cred_id}")

    def authenticate(self, secret: str | None) -> Principal:
        if not secret:
            raise HostError(401, "unauthenticated", "send Authorization: Bearer <agent key or link token>")
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM credentials WHERE secret_hash = ?", (hash_secret(secret),)).fetchone()
        if row is None or row["revoked_at"] or (row["expires_at"] and row["expires_at"] <= now()):
            raise HostError(401, "unauthenticated", "unknown, revoked, or expired credential")
        return Principal(
            credential_id=row["id"],
            kind=row["kind"],
            name=row["name"],
            author_kind=row["author_kind"],
            report_id=row["report_id"],
            role=row["role"],
            disclosure=row["disclosure"] or "full",
            scope=row["scope"],
        )

    # -- access ------------------------------------------------------------------------

    def _report_row(self, conn: sqlite3.Connection, p: Principal, rid: str, min_role: str) -> tuple[sqlite3.Row, str]:
        row = conn.execute("SELECT * FROM reports WHERE id = ?", (rid,)).fetchone()
        role = p.role_on(rid, row["creator_key"]) if row else None
        if row is None or role is None:
            raise _not_found(f"report {rid}")
        if RANK[role] < RANK[min_role]:
            raise HostError(403, "forbidden", f"this needs the {min_role} role; you are {role}")
        return row, role

    def _require_draft(self, p: Principal) -> None:
        if not p.sees_draft():
            raise HostError(403, "forbidden", "this link sees published versions only, not the draft")

    def _visible_version(self, conn: sqlite3.Connection, p: Principal, rid: str, number: int | None) -> int:
        """The published version number this caller may read."""
        latest = conn.execute("SELECT MAX(number) FROM versions WHERE report_id = ?", (rid,)).fetchone()[0]
        if latest is None:
            raise _not_found(f"a published version of {rid}")
        if p.sees_draft():
            wanted = latest if number is None else number
        elif p.scope == "latest":
            wanted = latest
            if number not in (None, latest):
                raise HostError(403, "forbidden", "this link sees the latest published version only")
        else:
            wanted = int(p.scope or 0)
            if number not in (None, wanted):
                raise HostError(403, "forbidden", f"this link sees version {wanted} only")
        if not conn.execute(
            "SELECT 1 FROM versions WHERE report_id = ? AND number = ?", (rid, wanted)
        ).fetchone():
            raise _not_found(f"version {wanted} of {rid}")
        return wanted

    def _log(self, conn: sqlite3.Connection, rid: str, author: dict, action: str, before: Any = None,
             after: Any = None) -> None:
        conn.execute(
            "INSERT INTO activity (report_id, author, action, before, after, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            (rid, dumps(author), action, None if before is None else dumps(before),
             None if after is None else dumps(after), now()),
        )

    # -- evidence records ---------------------------------------------------------------

    def _record(self, conn: sqlite3.Connection, rid: str, eid: str, version: int) -> Record | None:
        key = (rid, eid, version)
        if key in self._records:
            self._records.move_to_end(key)
            return self._records[key]
        row = conn.execute(
            "SELECT doc FROM evidence WHERE report_id = ? AND id = ? AND version = ?", (rid, eid, version)
        ).fetchone()
        if row is None:
            return None
        record = Record.from_json(json.loads(row["doc"]))
        self._records[key] = record  # evidence is immutable, so caching is safe
        if len(self._records) > 512:
            self._records.popitem(last=False)
        return record

    def _subject(self, conn: sqlite3.Connection, rid: str, content: dict) -> Subject:
        records = {}
        for eid, ver in export.pins_of(content) | _chart_refs(content):
            record = self._record(conn, rid, eid, ver)
            if record is not None:
                records[(eid, ver)] = record
        latest = {
            r["id"]: r["v"]
            for r in conn.execute("SELECT id, MAX(version) AS v FROM evidence WHERE report_id = ? GROUP BY id", (rid,))
        }
        return Subject(content, records, latest)

    def _validate(self, conn: sqlite3.Connection, rid: str, draft: dict) -> tuple[dict, dict]:
        """(content with statuses filled in, {counts, findings})."""
        content = _content(draft)
        problems = bundle_mod.schema_problems(content, "report.schema.json", "draft")
        if problems:
            raise HostError(422, "invalid_draft", "the edit leaves the draft malformed", problems)
        result = validate(self._subject(conn, rid, content))
        return result.apply(content), {
            "counts": result.counts(),
            "findings": [f.to_json() for f in result.findings],
        }

    def _revalidate(self, conn: sqlite3.Connection, rid: str, draft: dict) -> dict:
        """Refresh the stored statuses after something other than the draft changed.

        The revision does not move: the draft's content is the same.
        """
        content, validation = self._validate(conn, rid, draft)
        draft = {"title": draft["title"], **content}
        conn.execute("UPDATE reports SET draft = ?, draft_validation = ? WHERE id = ?",
                     (dumps(draft), dumps(validation), rid))
        return draft

    # -- reports -------------------------------------------------------------------------

    def create_report(self, p: Principal, title: str, owner: str | None = None) -> dict:
        if p.kind != "agent_key":
            raise HostError(403, "forbidden", "only an agent key can create reports")
        if not isinstance(title, str) or not title.strip():
            raise HostError(422, "invalid", "title must be a non-empty string")
        rid = new_id("rpt")
        draft = ops_mod.empty_draft(title)
        empty = {"counts": {"errors": 0, "warnings": 0, "info": 0}, "findings": []}
        admin_secret = new_secret("bvl")
        with self.db.write() as conn:
            conn.execute(
                "INSERT INTO reports VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                (rid, title, now(), dumps(p.author()), p.credential_id, dumps(draft), dumps(empty)),
            )
            conn.execute(
                "INSERT INTO credentials (id, secret_hash, kind, name, author_kind, report_id, role, disclosure,"
                " scope, created_at, created_by) VALUES (?, ?, 'link', ?, 'human', ?, 'admin', 'full', 'draft', ?, ?)",
                (new_id("lnk"), hash_secret(admin_secret), owner or "owner", rid, now(), p.credential_id),
            )
            self._log(conn, rid, p.author(), "create_report", None, {"title": title})
        return {
            "id": rid,
            "title": title,
            "role": "editor",
            "admin_link": admin_secret,
            "note": "admin_link is shown once; give it to the report's owner. It manages share links.",
        }

    def me(self, p: Principal) -> dict:
        """Who the caller is. Never includes the secret."""
        out = {"kind": p.kind, "name": p.name, "author_kind": p.author_kind, "disclosure": p.disclosure}
        if p.kind == "link":
            out.update(report_id=p.report_id, role=p.role, scope=p.scope)
        return out

    def list_reports(self, p: Principal) -> list[dict]:
        with self.db.read() as conn:
            if p.kind == "link":
                rows = conn.execute("SELECT * FROM reports WHERE id = ?", (p.report_id,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM reports ORDER BY created_at").fetchall()
            out = []
            for r in rows:
                latest = conn.execute("SELECT MAX(number) FROM versions WHERE report_id = ?", (r["id"],)).fetchone()[0]
                out.append({
                    "id": r["id"],
                    "title": r["title"],
                    "role": p.role_on(r["id"], r["creator_key"]),
                    "revision": r["revision"] if p.sees_draft() else None,
                    "latest_version": latest,
                })
        return out

    def get_report(self, p: Principal, rid: str, version: int | None = None, render: bool = False) -> dict:
        """The draft (suggester and up), or a published version.

        With `render`, text blocks carry sanitized `html` and each claim number
        lists the `cells` it cites: what the web view needs.
        """
        with self.db.read() as conn:
            row, role = self._report_row(conn, p, rid, "reader")
            versions = [
                dict(v) for v in conn.execute(
                    "SELECT number, published_at, published_by FROM versions WHERE report_id = ? ORDER BY number",
                    (rid,),
                )
            ]
            for v in versions:
                v["published_by"] = loads(v["published_by"])
            if version is None and p.sees_draft():
                if RANK[role] < RANK["suggester"]:
                    raise HostError(403, "forbidden", "the draft is visible to suggesters and up")
                draft = loads(row["draft"])
                evidence = [
                    {"id": e["id"], "latest_version": e["v"]}
                    for e in conn.execute(
                        "SELECT id, MAX(version) AS v FROM evidence WHERE report_id = ? GROUP BY id ORDER BY id",
                        (rid,),
                    )
                ]
                return {
                    "id": rid,
                    "kind": "draft",
                    "title": row["title"],
                    "revision": row["revision"],
                    "role": role,
                    "disclosure": p.disclosure,
                    **(self._render(conn, rid, _content(draft)) if render else _content(draft)),
                    "validation": loads(row["draft_validation"]),
                    "evidence": evidence,
                    "versions": versions,
                    "next_ids": {"block": ops_mod.next_block_id(draft), "claim": ops_mod.next_claim_id(draft)},
                }
            number = self._visible_version(conn, p, rid, version)
            v = conn.execute("SELECT * FROM versions WHERE report_id = ? AND number = ?", (rid, number)).fetchone()
            return {
                "id": rid,
                "kind": "version",
                "title": v["title"],
                "version": number,
                "role": role,
                "published_at": v["published_at"],
                "published_by": loads(v["published_by"]),
                "override": loads(v["override"]),
                "disclosure": p.disclosure,
                **_withhold_values(
                    self._render(conn, rid, loads(v["content"])) if render else loads(v["content"]), p.disclosure),
                "validation": loads(v["validation"]),
                "versions": versions if p.sees_draft() else [x for x in versions if x["number"] == number],
            }

    def _render(self, conn: sqlite3.Connection, rid: str, content: dict) -> dict:
        content = copy.deepcopy(content)
        claim_ids = frozenset(c["id"] for c in content["claims"])
        for block in content["blocks"]:
            if block["type"] == "text":
                block["html"] = to_html(block["text"], claim_ids)
        for claim in content["claims"]:
            pinned = {}
            for pin in claim["evidence"]:
                record = self._record(conn, rid, pin["id"], pin["version"])
                if record is not None:
                    pinned[pin["id"]] = record
            for n in claim["numbers"]:
                n["cells"] = [
                    {"evidence": eid, "version": ver, "key": list(key), "column": column}
                    for (eid, ver), key, column in number_cells(n, pinned)
                ]
        return content

    # -- evidence -------------------------------------------------------------------------

    def add_evidence(self, p: Principal, rid: str, payload: dict) -> dict:
        """Append a run; it creates a version when the data changed (SPEC §3.4)."""
        if not isinstance(payload, dict):
            raise HostError(422, "invalid_evidence", "evidence must be an object")
        extra = set(payload) - EVIDENCE_FIELDS
        if extra:
            raise HostError(422, "invalid_evidence", f"unknown field(s): {', '.join(sorted(extra))}",
                            {"allowed": sorted(EVIDENCE_FIELDS)})
        missing = {"id", "source", "sql", "key", "columns", "rows"} - set(payload)
        if missing:
            raise HostError(422, "invalid_evidence", f"missing field(s): {', '.join(sorted(missing))}")
        eid = payload["id"]
        if not isinstance(eid, str) or not SLUG.fullmatch(eid):
            raise HostError(422, "invalid_evidence",
                            "id must be 3-64 lowercase letters, digits, and hyphens, starting with a letter")
        rows = payload["rows"]
        if not isinstance(rows, list):
            raise HostError(422, "invalid_evidence", "rows must be a list of rows")
        size = len(dumps(rows).encode())
        if len(rows) > self.settings.max_rows or size > self.settings.max_bytes:
            raise HostError(
                413, "evidence_too_large",
                f"result has {len(rows):,} rows ({size:,} bytes); the cap is {self.settings.max_rows:,} rows and "
                f"{self.settings.max_bytes:,} bytes. Aggregate, or submit a sample with partial: true and "
                "source_row_count, and state that in assumptions",
            )

        doc = {
            "id": eid,
            "version": 1,
            "source": payload["source"],
            "sql": payload["sql"],
            "params": payload.get("params", {}),
            "run_at": payload.get("run_at") or now(),
            "provenance": "agent-reported",
            "key": payload["key"],
            "columns": payload["columns"],
            "rows": rows,
            "row_count": len(rows),
            "data_hash": "sha256:" + "0" * 64,
            "assumptions": payload.get("assumptions", []),
        }
        if "partial" in payload:
            doc["partial"] = payload["partial"]
        if "source_row_count" in payload:
            doc["source_row_count"] = payload["source_row_count"]
        doc["disclosure"] = {"level": "full", "withheld_rows": 0}
        problems = bundle_mod.schema_problems(doc, "evidence.schema.json", "evidence")
        if problems:
            raise HostError(422, "invalid_evidence", "evidence does not fit the evidence schema", problems)
        try:
            record = Record.from_json(doc)
        except EvidenceError as e:
            raise HostError(422, "invalid_evidence", str(e)) from e
        doc["data_hash"] = record.compute_hash()

        with self.db.write() as conn:
            self._report_row(conn, p, rid, "suggester")
            prev_row = conn.execute(
                "SELECT doc FROM evidence WHERE report_id = ? AND id = ? ORDER BY version DESC LIMIT 1", (rid, eid)
            ).fetchone()
            prev = loads(prev_row["doc"]) if prev_row else None
            if prev and payload.get("create_only"):
                raise HostError(409, "evidence_exists",
                                f"{eid} already exists (v{prev['version']}); choose another id or drop create_only")
            flags = _run_flags(prev, doc) if prev else []
            if prev and prev["data_hash"] == doc["data_hash"]:
                outcome, version, diff = "same_result", prev["version"], None
            else:
                outcome = "new_version" if prev else "created"
                version = prev["version"] + 1 if prev else 1
                doc["version"] = version
                conn.execute(
                    "INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (rid, eid, version, dumps(doc), doc["data_hash"], now(), dumps(p.author())),
                )
                diff = evidence_diff(prev, doc) if prev else None
            conn.execute(
                "INSERT INTO evidence_runs (report_id, evidence_id, version, outcome, flags, source, sql, params,"
                " run_at, received_at, author) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rid, eid, version, outcome, dumps(flags), doc["source"], doc["sql"], dumps(doc["params"]),
                 doc["run_at"], now(), dumps(p.author())),
            )
            self._log(conn, rid, p.author(), "add_evidence", None,
                      {"id": eid, "version": version, "outcome": outcome, "flags": flags})
            draft = loads(conn.execute("SELECT draft FROM reports WHERE id = ?", (rid,)).fetchone()["draft"])
            if outcome != "same_result":
                draft = self._revalidate(conn, rid, draft)  # pins may now be stale (SPEC §3.3)

        out: dict[str, Any] = {
            "id": eid, "version": version, "outcome": outcome, "flags": flags,
            "data_hash": doc["data_hash"], "row_count": doc["row_count"],
        }
        if outcome == "same_result":
            out["note"] = f"the data matches v{version}; no new version was created"
        if diff is not None:
            out["diff"] = diff
            stale = _pinned_older(draft, eid, version)
            if stale:
                out["stale"] = stale
                out["note"] = (f"{len(stale)} claim(s) or chart(s) pin older versions of {eid}; "
                               f"repin them with the repin op to adopt v{version}")
        return out

    def get_evidence(self, p: Principal, rid: str, eid: str, version: int | None = None) -> dict:
        with self.db.read() as conn:
            self._report_row(conn, p, rid, "reader")
            if p.sees_draft():
                row = conn.execute(
                    "SELECT doc FROM evidence WHERE report_id = ? AND id = ?"
                    + (" AND version = ?" if version else "") + " ORDER BY version DESC LIMIT 1",
                    (rid, eid, version) if version else (rid, eid),
                ).fetchone()
                if row is None:
                    raise _not_found(f"evidence {eid}" + (f" v{version}" if version else ""))
                doc = loads(row["doc"])
                runs = [
                    {**dict(r), "flags": loads(r["flags"]), "params": loads(r["params"]), "author": loads(r["author"])}
                    for r in conn.execute(
                        "SELECT version, outcome, flags, source, sql, params, run_at, received_at, author"
                        " FROM evidence_runs WHERE report_id = ? AND evidence_id = ? ORDER BY seq",
                        (rid, eid),
                    )
                ]
                versions = [r[0] for r in conn.execute(
                    "SELECT version FROM evidence WHERE report_id = ? AND id = ? ORDER BY version", (rid, eid))]
                return {"evidence": doc, "row_keys": _row_keys(doc), "versions": versions, "runs": runs}
            # A link reads evidence through the version it can see, at its disclosure level.
            number = self._visible_version(conn, p, rid, None)
            content = loads(conn.execute(
                "SELECT content FROM versions WHERE report_id = ? AND number = ?", (rid, number)).fetchone()["content"])
            pinned = {v for i, v in export.pins_of(content) if i == eid}
            if not pinned or (version is not None and version not in pinned):
                raise _not_found(f"evidence {eid} in version {number}")
            ver = version or max(pinned)
            docs = self._docs(conn, rid, content)
            if (eid, ver) not in docs:  # pinned over pin.unknown
                raise _not_found(f"evidence {eid} v{ver}")
        doc = restrict(content, docs, p.disclosure)[(eid, ver)]
        return {"evidence": doc, "row_keys": _row_keys(doc)}

    def diff_evidence(self, p: Principal, rid: str, eid: str, old: int, new: int) -> dict:
        self._require_draft(p)
        with self.db.read() as conn:
            self._report_row(conn, p, rid, "suggester")
            docs = {}
            for v in (old, new):
                row = conn.execute("SELECT doc FROM evidence WHERE report_id = ? AND id = ? AND version = ?",
                                   (rid, eid, v)).fetchone()
                if row is None:
                    raise _not_found(f"evidence {eid} v{v}")
                docs[v] = loads(row["doc"])
        return evidence_diff(docs[old], docs[new])

    def _docs(self, conn: sqlite3.Connection, rid: str, content: dict) -> dict[tuple[str, int], dict]:
        """The pinned evidence that exists. A pin that doesn't resolve is skipped: the validator
        reports it as pin.unknown, and a publish may override that (§3.1)."""
        out = {}
        for eid, ver in export.pins_of(content):
            row = conn.execute("SELECT doc FROM evidence WHERE report_id = ? AND id = ? AND version = ?",
                               (rid, eid, ver)).fetchone()
            if row is not None:
                out[(eid, ver)] = loads(row["doc"])
        return out

    def _export_docs(self, conn: sqlite3.Connection, rid: str, content: dict) -> dict[tuple[str, int], dict]:
        """Every pinned document: a bundle must list each version its report pins (spec/bundle rule 2)."""
        docs = self._docs(conn, rid, content)
        missing = sorted(export.pins_of(content) - set(docs))
        if missing:
            eid, ver = missing[0]
            raise HostError(409, "missing_evidence", f"{eid} v{ver} is pinned but does not exist")
        return docs

    # -- edits ------------------------------------------------------------------------------

    def _apply(self, conn: sqlite3.Connection, row: sqlite3.Row, ops: list, author: dict,
               log_prefix: str = "") -> dict:
        """Apply ops to the draft, validate, and store. Shared by edits and accepted suggestions."""
        rid = row["id"]
        draft = loads(row["draft"])
        before_validation = loads(row["draft_validation"])
        try:
            applied = ops_mod.apply(draft, ops)
        except ops_mod.OpError as e:
            raise HostError(422, "invalid_op", str(e), {"op": e.index}) from e
        content, validation = self._validate(conn, rid, applied.draft)
        _reject_dropped_spans(draft, before_validation, applied.draft, validation)

        new_draft = {"title": applied.draft["title"], **content}
        revision = row["revision"] + 1
        conn.execute(
            "UPDATE reports SET title = ?, revision = ?, draft = ?, draft_validation = ? WHERE id = ?",
            (new_draft["title"], revision, dumps(new_draft), dumps(validation), rid),
        )
        for change in applied.changes:
            self._log(conn, rid, author, log_prefix + change.action, change.before, change.after)
        return {
            "revision": revision,
            "validation": validation,
            "changes": _number_changes(draft, new_draft),
        }

    def edit(self, p: Principal, rid: str, ops: list, base_revision: int | None = None) -> dict:
        """Change the draft directly (editor and up)."""
        self._require_draft(p)
        with self.db.write() as conn:
            row, role = self._report_row(conn, p, rid, "suggester")
            if RANK[role] < RANK["editor"]:
                raise HostError(403, "forbidden",
                                "suggesters propose changes with suggest_edit; editors apply them")
            _check_revision(row, base_revision)
            return self._apply(conn, row, ops, p.author())

    def preview(self, p: Principal, rid: str, ops: list | None = None) -> dict:
        """Validate the draft, or the draft with `ops` applied, without storing anything."""
        self._require_draft(p)
        with self.db.read() as conn:
            row, _ = self._report_row(conn, p, rid, "suggester")
            draft = loads(row["draft"])
            if not ops:
                _, validation = self._validate(conn, rid, draft)
                return {"revision": row["revision"], "validation": validation}
            try:
                applied = ops_mod.apply(draft, ops)
            except ops_mod.OpError as e:
                raise HostError(422, "invalid_op", str(e), {"op": e.index}) from e
            content, validation = self._validate(conn, rid, applied.draft)
            return {
                "revision": row["revision"],
                "validation": validation,
                "changes": _number_changes(draft, {**content, "title": applied.draft["title"]}),
                "diff": _diff(draft, {**content, "title": applied.draft["title"]}),
            }

    # -- suggestions ------------------------------------------------------------------------

    def suggest(self, p: Principal, rid: str, ops: list, message: str = "") -> dict:
        preview = self.preview(p, rid, ops)  # validates the ops now (SPEC §6)
        sid = new_id("sug")
        with self.db.write() as conn:
            row, _ = self._report_row(conn, p, rid, "suggester")
            conn.execute(
                "INSERT INTO suggestions (id, report_id, author, message, ops, base_revision, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 'open', ?)",
                (sid, rid, dumps(p.author()), message or "", dumps(ops), row["revision"], now()),
            )
            self._log(conn, rid, p.author(), "suggest", None, {"id": sid, "message": message, "ops": ops})
        return {"id": sid, "status": "open", **preview}

    def list_suggestions(self, p: Principal, rid: str, status: str | None = "open") -> list[dict]:
        self._require_draft(p)
        with self.db.read() as conn:
            self._report_row(conn, p, rid, "suggester")
            q = "SELECT * FROM suggestions WHERE report_id = ?" + (" AND status = ?" if status else "")
            rows = conn.execute(q + " ORDER BY created_at", (rid, status) if status else (rid,)).fetchall()
        return [_suggestion(r) for r in rows]

    def get_suggestion(self, p: Principal, rid: str, sid: str) -> dict:
        """The suggestion, with its diff and validation against the current draft."""
        with self.db.read() as conn:
            self._report_row(conn, p, rid, "suggester")
            row = conn.execute("SELECT * FROM suggestions WHERE id = ? AND report_id = ?", (sid, rid)).fetchone()
        if row is None:
            raise _not_found(f"suggestion {sid}")
        out = _suggestion(row)
        if row["status"] == "open":
            try:
                out["preview"] = self.preview(p, rid, loads(row["ops"]))
            except HostError as e:
                out["preview"] = {"error": e.to_json()}
        return out

    def resolve_suggestion(self, p: Principal, rid: str, sid: str, accept: bool, note: str = "") -> dict:
        self._require_draft(p)
        with self.db.write() as conn:
            report, role = self._report_row(conn, p, rid, "suggester")
            if RANK[role] < RANK["editor"]:
                raise HostError(403, "forbidden", "accepting or rejecting a suggestion needs the editor role")
            row = conn.execute("SELECT * FROM suggestions WHERE id = ? AND report_id = ?", (sid, rid)).fetchone()
            if row is None:
                raise _not_found(f"suggestion {sid}")
            if row["status"] != "open":
                raise HostError(409, "not_open", f"suggestion {sid} is already {row['status']}")
            result: dict[str, Any] = {}
            if accept:
                # Validated again on accept; a failing claim is allowed through (SPEC §5).
                result = self._apply(conn, report, loads(row["ops"]), loads(row["author"]))
            status = "accepted" if accept else "rejected"
            conn.execute(
                "UPDATE suggestions SET status = ?, resolved_at = ?, resolved_by = ?, note = ? WHERE id = ?",
                (status, now(), dumps(p.author()), note, sid),
            )
            action = "accept_suggestion" if accept else "reject_suggestion"
            self._log(conn, rid, p.author(), action, None, {"id": sid, "note": note})
        return {"id": sid, "status": status, **result}

    # -- publishing -------------------------------------------------------------------------

    def publish(self, p: Principal, rid: str, override_reason: str | None = None,
                base_revision: int | None = None) -> dict:
        self._require_draft(p)
        with self.db.write() as conn:
            row, role = self._report_row(conn, p, rid, "suggester")
            if RANK[role] < RANK["editor"]:
                raise HostError(403, "forbidden", "publishing needs the editor role")
            _check_revision(row, base_revision)
            draft = loads(row["draft"])
            content, validation = self._validate(conn, rid, draft)  # evidence may have moved on
            errors = validation["counts"]["errors"]
            override = None
            if errors:
                if not override_reason:
                    raise HostError(409, "validation_errors",
                                    f"the draft has {errors} validation error(s); fix them or publish with an "
                                    "override reason", validation["findings"])
                override = {"reason": override_reason, "errors_overridden": errors}
            number = (conn.execute("SELECT MAX(number) FROM versions WHERE report_id = ?", (rid,)).fetchone()[0]
                      or 0) + 1
            published_at = now()
            conn.execute(
                "INSERT INTO versions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (rid, number, row["title"], published_at, dumps(p.author()),
                 None if override is None else dumps(override), dumps(content), dumps(validation)),
            )
            conn.execute(
                "UPDATE reports SET draft = ?, draft_validation = ? WHERE id = ?",
                (dumps({"title": row["title"], **content}), dumps(validation), rid),
            )  # the draft carries the statuses it was published with
            self._log(conn, rid, p.author(), "publish", None,
                      {"version": number, "counts": validation["counts"], "override": override})
        return {"version": number, "published_at": published_at, "override": override, **validation}

    def export_files(self, p: Principal, rid: str, version: int | None = None) -> tuple[str, dict[str, bytes]]:
        """Bundle files for a published version, at the caller's disclosure level."""
        with self.db.read() as conn:
            self._report_row(conn, p, rid, "reader")
            number = self._visible_version(conn, p, rid, version)
            v = conn.execute("SELECT * FROM versions WHERE report_id = ? AND number = ?", (rid, number)).fetchone()
            content = loads(v["content"])
            files = export.bundle_files(
                report={"id": rid, "title": v["title"]},
                version={
                    "number": number,
                    "published_at": v["published_at"],
                    "published_by": loads(v["published_by"]),
                    "override": loads(v["override"]),
                },
                validation=loads(v["validation"])["counts"],
                content=content,
                docs=self._export_docs(conn, rid, content),
                level=p.disclosure,
            )
        return f"{rid}-v{number}-{p.disclosure}", files

    def export_bundle(self, p: Principal, rid: str, version: int | None = None) -> tuple[str, bytes]:
        """The bundle as a zip: canonical JSON, plus report.md and report.html derived from it."""
        name, files = self.export_files(p, rid, version)
        try:
            files = export.with_derived(files)  # also proves the bundle loads
        except RenderError as e:
            raise HostError(500, "render_failed", "the bundle could not be rendered", str(e)) from e
        return f"{name}.zip", export.zip_bytes(files, name)

    def export_html(self, p: Principal, rid: str, version: int | None = None) -> tuple[str, bytes]:
        name, files = self.export_files(p, rid, version)
        cached = self.settings.export_dir / f"{name}.html"
        if not cached.exists():  # published versions never change
            with tempfile.TemporaryDirectory() as tmp:
                b = export.write_dir(Path(tmp), files)
                cached.parent.mkdir(parents=True, exist_ok=True)
                try:
                    render_html(b, cached)
                except RenderError as e:
                    raise HostError(500, "render_failed", "the HTML could not be rendered", str(e)) from e
        return f"{name}.html", cached.read_bytes()

    def export_pdf(self, p: Principal, rid: str, version: int | None = None) -> tuple[str, bytes]:
        name, files = self.export_files(p, rid, version)
        cached = self.settings.export_dir / f"{name}.pdf"
        if not cached.exists():  # a published version never changes, so neither does its PDF
            with tempfile.TemporaryDirectory() as tmp:
                b = export.write_dir(Path(tmp), files)
                cached.parent.mkdir(parents=True, exist_ok=True)
                try:
                    render_pdf(b, cached)
                except RenderError as e:
                    raise HostError(500, "render_failed", "the PDF could not be rendered", str(e)) from e
        return f"{name}.pdf", cached.read_bytes()

    # -- comments and activity ------------------------------------------------------------

    def add_comment(self, p: Principal, rid: str, body: str, target: dict | None = None,
                    version: int | None = None, display_name: str | None = None) -> dict:
        if not isinstance(body, str) or not body.strip():
            raise HostError(422, "invalid", "a comment needs a body")
        target = target or {}
        if set(target) - set(TARGET_KINDS) or len(target) > 1:
            raise HostError(422, "invalid", 'target is {"block": id}, {"claim": id}, or {}')
        cid = new_id("cmt")
        with self.db.write() as conn:
            self._report_row(conn, p, rid, "commenter")
            if not p.sees_draft():
                version = self._visible_version(conn, p, rid, version)
            elif version is not None:
                version = self._visible_version(conn, p, rid, version)
            author = _author(p, display_name)
            conn.execute(
                "INSERT INTO comments VALUES (?, ?, ?, ?, ?, ?, ?)",
                (cid, rid, dumps(target), version, body, dumps(author), now()),
            )
            self._log(conn, rid, author, "comment", None,
                      {"id": cid, "target": target, "version": version, "body": body})
        return {"id": cid, "target": target, "version": version}

    def list_comments(self, p: Principal, rid: str) -> list[dict]:
        with self.db.read() as conn:
            self._report_row(conn, p, rid, "reader")
            rows = conn.execute("SELECT * FROM comments WHERE report_id = ? ORDER BY created_at", (rid,)).fetchall()
            visible = None if p.sees_draft() else self._visible_version(conn, p, rid, None)
        return [
            {"id": r["id"], "target": loads(r["target"]), "version": r["version"], "body": r["body"],
             "author": loads(r["author"]), "created_at": r["created_at"]}
            for r in rows if visible is None or r["version"] == visible
        ]

    def activity(self, p: Principal, rid: str, after: int = 0, limit: int = 100) -> list[dict]:
        self._require_draft(p)
        with self.db.read() as conn:
            self._report_row(conn, p, rid, "editor")
            rows = conn.execute(
                "SELECT * FROM activity WHERE report_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                (rid, after, min(max(limit, 1), 1000)),
            ).fetchall()
        return [
            {"seq": r["seq"], "author": loads(r["author"]), "action": r["action"], "before": loads(r["before"]),
             "after": loads(r["after"]), "timestamp": r["timestamp"]}
            for r in rows
        ]

    # -- challenges (SPEC §8) ------------------------------------------------------------

    def file_challenge(self, p: Principal, rid: str, target: dict, body: str, cells: list | None = None,
                       version: int | None = None, display_name: str | None = None) -> dict:
        """Challenge a claim or block of a published version (commenter and up)."""
        if not isinstance(body, str) or not body.strip():
            raise HostError(422, "invalid", "a challenge needs text saying what is in doubt")
        cells = cells or []
        if not isinstance(cells, list) or len(cells) > 50:
            raise HostError(422, "invalid", "cells is a list of at most 50 cells")
        cid = new_id("chl")
        with self.db.write() as conn:
            self._report_row(conn, p, rid, "commenter")
            number = self._visible_version(conn, p, rid, version)
            content = self._version_content(conn, rid, number)
            _check_target(content, target)
            visible = self._visible_records(conn, rid, content, p.disclosure)
            clean_cells = [_check_cell(c, visible) for c in cells]
            author = _author(p, display_name)
            stamp = now()
            conn.execute(
                "INSERT INTO challenges VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)",
                (cid, rid, number, dumps(target), dumps(clean_cells), body, dumps(author), p.credential_id,
                 stamp, stamp),
            )
            self._log(conn, rid, author, "file_challenge", None,
                      {"id": cid, "version": number, "target": target, "cells": clean_cells, "body": body})
        return self.get_challenge(p, rid, cid)

    def list_challenges(self, p: Principal, rid: str, status: str | None = None,
                        version: int | None = None) -> list[dict]:
        """Challenges as seen from one version. Earlier ones whose target is still there are carried over."""
        with self.db.read() as conn:
            role = self._challenge_reader(conn, p, rid)
            viewing = None
            if not p.sees_draft() or version is not None:
                viewing = self._visible_version(conn, p, rid, version)
            q = "SELECT * FROM challenges WHERE report_id = ?" + (" AND status = ?" if status else "")
            rows = conn.execute(q + " ORDER BY created_at", (rid, status) if status else (rid,)).fetchall()
            return [c for c in (self._challenge(conn, p, role, r, viewing) for r in rows) if c is not None]

    def get_challenge(self, p: Principal, rid: str, cid: str) -> dict:
        with self.db.read() as conn:
            role = self._challenge_reader(conn, p, rid)
            row = conn.execute("SELECT * FROM challenges WHERE id = ? AND report_id = ?", (cid, rid)).fetchone()
            viewing = None if p.sees_draft() else self._visible_version(conn, p, rid, None)
            out = self._challenge(conn, p, role, row, viewing) if row else None
        if out is None:
            raise _not_found(f"challenge {cid}")
        return out

    def list_open_challenges(self, p: Principal, rid: str | None = None) -> list[dict]:
        """Open challenges on one report, or on every report this caller can see (pull, SPEC §8)."""
        rids = [rid] if rid else [r["id"] for r in self.list_reports(p)]
        out = []
        for r in rids:
            try:
                out += [{"report_id": r, **c} for c in self.list_challenges(p, r, status="open")]
            except HostError as e:
                if rid or e.status not in (403, 404):
                    raise
        return out

    def reply_to_challenge(self, p: Principal, rid: str, cid: str, body: str, ops: list | None = None,
                           message: str = "", display_name: str | None = None) -> dict:
        """Answer with a comment, or with a suggestion that goes through review (SPEC §6)."""
        if not isinstance(body, str) or not body.strip():
            raise HostError(422, "invalid", "a reply needs text")
        with self.db.read() as conn:
            role = self._challenge_reader(conn, p, rid)
            row = conn.execute("SELECT * FROM challenges WHERE id = ? AND report_id = ?", (cid, rid)).fetchone()
        if row is None:
            raise _not_found(f"challenge {cid}")
        if row["status"] not in ("open", "answered"):
            raise HostError(409, "closed", f"challenge {cid} is {row['status']}; reopen it to reply")
        suggestion_id = None
        if ops:
            if RANK[role] < RANK["suggester"]:
                raise HostError(403, "forbidden", "proposing an edit needs the suggester role")
            suggestion_id = self.suggest(p, rid, ops, message or f"In reply to challenge {cid}")["id"]
        author = _author(p, display_name)
        with self.db.write() as conn:
            row = conn.execute("SELECT * FROM challenges WHERE id = ?", (cid,)).fetchone()
            filer = row["credential_id"] == p.credential_id
            new_status = row["status"]
            if row["status"] == "open" and not filer:
                new_status = "answered"
            elif row["status"] == "answered" and filer:
                new_status = "open"  # the challenger is not satisfied yet
            stamp = now()
            conn.execute(
                "INSERT INTO challenge_events (challenge_id, kind, author, credential_id, body, suggestion_id,"
                " from_status, to_status, created_at) VALUES (?, 'reply', ?, ?, ?, ?, ?, ?, ?)",
                (cid, dumps(author), p.credential_id, body, suggestion_id, row["status"], new_status, stamp),
            )
            conn.execute("UPDATE challenges SET status = ?, updated_at = ? WHERE id = ?", (new_status, stamp, cid))
            self._log(conn, rid, author, "reply_to_challenge", None,
                      {"id": cid, "body": body, "suggestion": suggestion_id, "status": new_status})
        return self.get_challenge(p, rid, cid)

    def set_challenge_status(self, p: Principal, rid: str, cid: str, status: str, note: str = "",
                             display_name: str | None = None) -> dict:
        """resolve (a human commenter and up), dismiss (a human editor and up), or reopen (the filer, or a
        human editor and up)."""
        if status not in ("resolved", "dismissed", "open"):
            raise HostError(422, "invalid", "status is resolved, dismissed, or open (reopen)")
        author = _author(p, display_name)
        with self.db.write() as conn:
            role = self._challenge_reader(conn, p, rid)
            row = conn.execute("SELECT * FROM challenges WHERE id = ? AND report_id = ?", (cid, rid)).fetchone()
            if row is None:
                raise _not_found(f"challenge {cid}")
            allowed = _challenge_permissions(p, role, row)
            action = {"resolved": "resolve", "dismissed": "dismiss", "open": "reopen"}[status]
            if not allowed[f"can_{action}"]:
                raise HostError(403, "forbidden", _why_not(action, p, row))
            stamp = now()
            conn.execute(
                "INSERT INTO challenge_events (challenge_id, kind, author, credential_id, body, from_status,"
                " to_status, created_at) VALUES (?, 'status', ?, ?, ?, ?, ?, ?)",
                (cid, dumps(author), p.credential_id, note or None, row["status"], status, stamp),
            )
            conn.execute("UPDATE challenges SET status = ?, updated_at = ? WHERE id = ?", (status, stamp, cid))
            self._log(conn, rid, author, f"{action}_challenge", {"status": row["status"]},
                      {"id": cid, "status": status, "note": note})
        return self.get_challenge(p, rid, cid)

    def _challenge_reader(self, conn: sqlite3.Connection, p: Principal, rid: str) -> str:
        """Challenges are for the people in the discussion: commenters and up, and agents."""
        _, role = self._report_row(conn, p, rid, "reader")
        if RANK[role] < RANK["commenter"]:
            raise HostError(403, "forbidden", "challenges are visible to commenters and up")
        return role

    def _version_content(self, conn: sqlite3.Connection, rid: str, number: int) -> dict:
        return loads(conn.execute("SELECT content FROM versions WHERE report_id = ? AND number = ?",
                                  (rid, number)).fetchone()["content"])

    def _visible_records(self, conn: sqlite3.Connection, rid: str, content: dict, level: str) -> dict:
        """Evidence of a version as this disclosure level shows it, as Records."""
        docs = restrict(content, self._docs(conn, rid, content), level)
        return {ident: Record.from_json(doc) for ident, doc in docs.items()}

    def _challenge(self, conn: sqlite3.Connection, p: Principal, role: str, row: sqlite3.Row,
                   viewing: int | None) -> dict | None:
        target = loads(row["target"])
        out = {
            "id": row["id"],
            "version": row["version"],
            "target": target,
            "body": row["body"],
            "status": row["status"],
            "author": loads(row["author"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "mine": row["credential_id"] == p.credential_id,
        }
        if viewing is not None:
            if row["version"] > viewing:
                return None
            if row["version"] < viewing:
                then = _target_in(self._version_content(conn, row["report_id"], row["version"]), target)
                now_ = _target_in(self._version_content(conn, row["report_id"], viewing), target)
                if now_ is None:
                    return None  # the challenged claim or block is gone from this version
                out["carried_over"] = True
                out["target_changed"] = _strip_statuses(then or {}) != _strip_statuses(now_)
        # Highlighted cells are shown only where this caller's disclosure level shows them.
        cells = loads(row["cells"])
        if cells and p.disclosure != "full":
            content = self._version_content(conn, row["report_id"], row["version"])
            visible = self._visible_records(conn, row["report_id"], content, p.disclosure)
            cells = [c for c in cells if _cell_visible(c, visible)]
        out["cells"] = cells
        out["thread"] = [
            {"kind": e["kind"], "author": loads(e["author"]), "body": e["body"], "suggestion_id": e["suggestion_id"],
             "from_status": e["from_status"], "to_status": e["to_status"], "created_at": e["created_at"]}
            for e in conn.execute("SELECT * FROM challenge_events WHERE challenge_id = ? ORDER BY seq", (row["id"],))
        ]
        out.update(_challenge_permissions(p, role, row))
        return out

    # -- share links ------------------------------------------------------------------------

    def create_link(self, p: Principal, rid: str, role: str, name: str, *, disclosure: str = "full",
                    scope: str | int = "latest", author_kind: str = "human", expires_at: str | None = None) -> dict:
        if role not in ROLES:
            raise HostError(422, "invalid", f"role is one of {', '.join(ROLES)}")
        if disclosure not in DISCLOSURES:
            raise HostError(422, "invalid", f"disclosure is one of {', '.join(DISCLOSURES)}")
        if author_kind not in ("human", "agent"):
            raise HostError(422, "invalid", "author_kind is human or agent")
        if not isinstance(name, str) or not name.strip():
            raise HostError(422, "invalid", "a link needs a name (whose link it is)")
        if expires_at is not None and not (isinstance(expires_at, str) and TIMESTAMP.fullmatch(expires_at)):
            # Expiry is compared as text against now(), so it must be in the same UTC form.
            raise HostError(422, "invalid", "expires_at is a UTC time like 2026-12-31T23:59:59Z")
        if RANK[role] >= RANK["suggester"]:
            scope = "draft"  # suggesters and up work on the draft
        elif scope == "draft":
            raise HostError(422, "invalid", "only suggester, editor, and admin links see the draft")
        with self.db.write() as conn:
            self._report_row(conn, p, rid, "admin")
            if scope not in ("draft", "latest"):
                try:
                    scope = str(int(scope))
                except (TypeError, ValueError) as e:
                    raise HostError(422, "invalid", 'scope is "latest" or a version number') from e
                if not conn.execute("SELECT 1 FROM versions WHERE report_id = ? AND number = ?",
                                    (rid, int(scope))).fetchone():
                    raise _not_found(f"version {scope}")
            secret = new_secret("bvl")
            lid = new_id("lnk")
            conn.execute(
                "INSERT INTO credentials (id, secret_hash, kind, name, author_kind, report_id, role, disclosure,"
                " scope, expires_at, created_at, created_by) VALUES (?, ?, 'link', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (lid, hash_secret(secret), name, author_kind, rid, role, disclosure, str(scope), expires_at, now(),
                 p.credential_id),
            )
            self._log(conn, rid, p.author(), "create_link", None,
                      {"id": lid, "name": name, "role": role, "disclosure": disclosure, "scope": scope})
        return {"id": lid, "secret": secret, "role": role, "disclosure": disclosure, "scope": scope,
                "expires_at": expires_at, "note": "the secret is shown once"}

    def list_links(self, p: Principal, rid: str) -> list[dict]:
        with self.db.read() as conn:
            self._report_row(conn, p, rid, "admin")
            rows = conn.execute(
                "SELECT id, name, author_kind, role, disclosure, scope, expires_at, revoked_at, created_at"
                " FROM credentials WHERE report_id = ? ORDER BY created_at", (rid,)).fetchall()
        return [{**dict(r), "current": r["id"] == p.credential_id} for r in rows]

    def revoke_link(self, p: Principal, rid: str, link_id: str) -> dict:
        if link_id == p.credential_id:
            raise HostError(409, "own_link", "you cannot revoke the link you are using; use another admin link")
        with self.db.write() as conn:
            self._report_row(conn, p, rid, "admin")
            cur = conn.execute(
                "UPDATE credentials SET revoked_at = ? WHERE id = ? AND report_id = ? AND revoked_at IS NULL",
                (now(), link_id, rid))
            if cur.rowcount == 0:
                raise _not_found(f"active link {link_id}")
            self._log(conn, rid, p.author(), "revoke_link", None, {"id": link_id})
        return {"id": link_id, "revoked": True}


# -- helpers ------------------------------------------------------------------------------


def _content(draft: dict) -> dict:
    return {"blocks": draft["blocks"], "claims": draft["claims"], "non_data": draft["non_data"]}


def _chart_refs(content: dict) -> set[tuple[str, int]]:
    """Evidence named inside chart specs, which may differ from the block's pins."""
    out = set()
    for block in content["blocks"]:
        if block["type"] == "chart":
            for _, data in data_values(block["spec"]):
                ref = data.get("evidence") if isinstance(data, dict) else None
                if isinstance(ref, dict) and isinstance(ref.get("id"), str) and isinstance(ref.get("version"), int):
                    out.add((ref["id"], ref["version"]))
    return out


def _author(p: Principal, display_name: str | None) -> dict:
    """A link may be shared by several people; each gives a name, recorded unverified (SPEC §7)."""
    author = p.author()
    name = " ".join(str(display_name or "").split())[:80]
    if p.kind == "link" and name:
        author = {**author, "name": name, "link": p.name}
    return author


def _target_in(content: dict, target: dict) -> dict | None:
    (kind, tid), = target.items()
    items = content["claims"] if kind == "claim" else content["blocks"]
    return next((i for i in items if i["id"] == tid), None)


def _check_target(content: dict, target: Any) -> None:
    if not isinstance(target, dict) or len(target) != 1 or next(iter(target)) not in TARGET_KINDS:
        raise HostError(422, "invalid", 'target is {"claim": id} or {"block": id}')
    if _target_in(content, target) is None:
        raise _not_found(f"{next(iter(target))} {next(iter(target.values()))} in this version")


def _cell_visible(cell: dict, records: dict) -> bool:
    record = records.get((cell["evidence"], cell["version"]))
    return (record is not None and cell["column"] in record.column_names and record.rows is not None
            and record.row(tuple(cell["key"])) is not None)


def _check_cell(cell: Any, records: dict) -> dict:
    """A highlighted cell, keyed by canonical text (as `row_keys` gives it), visible to the filer."""
    if not isinstance(cell, dict) or set(cell) != {"evidence", "version", "key", "column"}:
        raise HostError(422, "invalid", "a cell is {evidence, version, key, column}")
    if not isinstance(cell["key"], list) or not all(k is None or isinstance(k, str) for k in cell["key"]):
        raise HostError(422, "invalid", "a cell's key is its canonical text values, as row_keys gives them")
    clean = {"evidence": cell["evidence"], "version": cell["version"], "key": cell["key"], "column": cell["column"]}
    if not _cell_visible(clean, records):
        raise HostError(422, "invalid", f"no visible cell {cell['column']} at {cell['key']} in "
                                        f"{cell['evidence']} v{cell['version']}")
    return clean


def _challenge_permissions(p: Principal, role: str, row: sqlite3.Row) -> dict:
    human = p.author_kind == "human"
    live = row["status"] in ("open", "answered")
    filer = row["credential_id"] == p.credential_id
    return {
        "can_reply": live,
        "can_resolve": live and human,
        "can_dismiss": live and human and RANK[role] >= RANK["editor"],
        "can_reopen": row["status"] != "open" and (filer or (human and RANK[role] >= RANK["editor"])),
    }


def _why_not(action: str, p: Principal, row: sqlite3.Row) -> str:
    if row["status"] in ("resolved", "dismissed") and action != "reopen":
        return f"the challenge is already {row['status']}"
    if action == "reopen" and row["status"] == "open":
        return "the challenge is already open"
    if p.author_kind != "human" and action in ("resolve", "dismiss"):
        return f"only a person can {action} a challenge; an agent answers it"
    if action == "dismiss":
        return "dismissing a challenge needs the editor role"
    return "only the person who filed it, or an editor, can reopen a challenge"


def _withhold_values(content: dict, level: str) -> dict:
    """At metadata disclosure, computed values are data too; expressions stay (SPEC §7)."""
    if level != "metadata":
        return content
    content = copy.deepcopy(content)
    for claim in content["claims"]:
        for n in claim["numbers"]:
            if n["tier"] == "computed":
                n["value"] = None
    return content


def _row_keys(doc: dict) -> list[list[str | None]] | None:
    """Each row's key in canonical text, aligned with `rows`, for matching cited cells."""
    if doc.get("rows") is None:
        return None
    names = [c["name"] for c in doc["columns"]]
    types = dict((c["name"], c["type"]) for c in doc["columns"])
    return [
        [values.from_json(types[k], row[names.index(k)]).canonical() for k in doc["key"]]
        for row in doc["rows"]
    ]


def _check_revision(row: sqlite3.Row, base_revision: int | None) -> None:
    if base_revision is not None and base_revision != row["revision"]:
        raise HostError(409, "stale_revision",
                        f"the draft is at revision {row['revision']}, not {base_revision}; re-read it and retry")


def _reject_dropped_spans(before: dict, before_validation: dict, after: dict, validation: dict) -> None:
    """A write may not remove the span of a claim that still exists (SPEC §3.2)."""
    spanless_before = {f.get("claim") for f in before_validation["findings"] if f["code"] == "claim.no_span"}
    had_span = {c["id"] for c in before["claims"]} - spanless_before
    dropped = sorted(
        f["claim"] for f in validation["findings"] if f["code"] == "claim.no_span" and f.get("claim") in had_span
    )
    if dropped:
        raise HostError(
            422, "span_dropped",
            f"the edit removes the span of live claim(s) {', '.join(dropped)}; keep the {{#cN}} span, or unlink "
            "the claim in the same edit",
            {"claims": dropped},
        )


def _number_changes(before: dict, after: dict) -> list[dict]:
    """Claims and numbers whose status or computed value changed."""
    old = {c["id"]: c for c in before["claims"]}
    out = []
    for claim in after["claims"]:
        prev = old.get(claim["id"])
        if prev is None:
            out.append({"claim": claim["id"], "added": True, "status": claim["status"]})
            continue
        if prev["status"] != claim["status"]:
            out.append({"claim": claim["id"], "status": {"before": prev["status"], "after": claim["status"]}})
        for i, n in enumerate(claim["numbers"]):
            p = prev["numbers"][i] if i < len(prev["numbers"]) else None
            fields = ("status", "value")
            if p is None or any(p.get(f) != n.get(f) for f in fields) or p.get("text") != n.get("text"):
                out.append({
                    "claim": claim["id"], "number": i, "text": n["text"],
                    "before": None if p is None else {f: p.get(f) for f in fields},
                    "after": {f: n.get(f) for f in fields},
                })
    for cid in old.keys() - {c["id"] for c in after["claims"]}:
        out.append({"claim": cid, "removed": True})
    return out


def _diff(before: dict, after: dict) -> dict:
    """Blocks and claims that a set of ops adds, changes, or removes."""
    def index(items: list[dict]) -> dict[str, dict]:
        return {i["id"]: i for i in items}

    out: dict[str, Any] = {}
    if before["title"] != after["title"]:
        out["title"] = {"before": before["title"], "after": after["title"]}
    for kind in ("blocks", "claims"):
        a, b = index(before[kind]), index(after[kind])
        strip = (lambda c: {k: v for k, v in c.items() if k != "status"}) if kind == "claims" else (lambda x: x)
        changes = []
        for key in list(a) + [k for k in b if k not in a]:
            old, new = a.get(key), b.get(key)
            if old is not None and new is not None and _strip_statuses(strip(old)) == _strip_statuses(strip(new)):
                continue
            changes.append({"id": key, "before": old, "after": new})
        if changes:
            out[kind] = changes
    if before["non_data"] != after["non_data"]:
        out["non_data"] = {"before": before["non_data"], "after": after["non_data"]}
    return out


def _strip_statuses(claim: dict) -> dict:
    c = copy.deepcopy(claim)
    for n in c.get("numbers", []):
        for k in ("status", "value", "message"):
            n.pop(k, None)
    return c


def _suggestion(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "author": loads(row["author"]),
        "message": row["message"],
        "ops": loads(row["ops"]),
        "base_revision": row["base_revision"],
        "status": row["status"],
        "created_at": row["created_at"],
        "resolved_at": row["resolved_at"],
        "resolved_by": loads(row["resolved_by"]),
        "note": row["note"],
    }


def _run_flags(prev: dict, doc: dict) -> list[str]:
    flags = []
    if prev["sql"] != doc["sql"]:
        flags.append("query text changed")
    if prev["source"] != doc["source"]:
        flags.append("source changed")
    if prev["params"] != doc["params"]:
        flags.append("params changed")
    return flags


def _pinned_older(draft: dict, eid: str, version: int) -> list[dict]:
    out = []
    for claim in draft["claims"]:
        for pin in claim["evidence"]:
            if pin["id"] == eid and pin["version"] < version:
                out.append({"claim": claim["id"], "version": pin["version"]})
    for block in draft["blocks"]:
        for pin in block.get("evidence", []) if block["type"] == "chart" else []:
            if pin["id"] == eid and pin["version"] < version:
                out.append({"block": block["id"], "version": pin["version"]})
    return out


def evidence_diff(old: dict, new: dict, limit: int = 100) -> dict:
    """What changed between two versions of one evidence id."""
    a, b = Record.from_json(old), Record.from_json(new)
    a_cols, b_cols = dict(a.columns), dict(b.columns)
    out: dict[str, Any] = {
        "from": old["version"],
        "to": new["version"],
        "query_changed": old["sql"] != new["sql"],
        "source_changed": old["source"] != new["source"],
        "params_changed": old["params"] != new["params"],
        "columns": {
            "added": [c for c in b_cols if c not in a_cols],
            "removed": [c for c in a_cols if c not in b_cols],
            "retyped": [c for c in a_cols if c in b_cols and a_cols[c] != b_cols[c]],
        },
        "key_changed": a.key != b.key,
    }
    if out["key_changed"]:
        out["note"] = "the key changed, so rows are not compared"
        return out

    def keyed(r: Record) -> dict:
        return {tuple(row[i].canonical() for i in r.key_indexes): row for row in r.rows or []}

    ra, rb = keyed(a), keyed(b)
    out["rows_added"] = [list(k) for k in rb if k not in ra][:limit]
    out["rows_removed"] = [list(k) for k in ra if k not in rb][:limit]
    common = [c for c in a.column_names if c in b_cols and a_cols[c] == b_cols[c]]
    changed = []
    total = 0
    for key, row_a in ra.items():
        row_b = rb.get(key)
        if row_b is None:
            continue
        for col in common:
            va = row_a[a.column_names.index(col)].canonical()
            vb = row_b[b.column_names.index(col)].canonical()
            if va != vb:
                total += 1
                if len(changed) < limit:
                    changed.append({"key": list(key), "column": col, "before": va, "after": vb})
    out["cells_changed"] = changed
    out["cells_changed_total"] = total
    out["row_counts"] = {"before": len(ra), "after": len(rb)}
    return out
