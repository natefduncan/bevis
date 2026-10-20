"""Read a bundle directory and check it (spec/bundle/README.md)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any, Iterator

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from .evidence import EvidenceError, LookupFailed, Record

# Wheels carry the schemas inside the package; a source checkout reads spec/.
_HERE = Path(__file__).resolve().parent
SCHEMA_DIR = next(
    p for p in (_HERE / "spec" / "bundle", _HERE.parents[1] / "spec" / "bundle") if p.is_dir()
)


class BundleError(Exception):
    """The bundle is unreadable or breaks a schema or integrity rule."""

    def __init__(self, problems: list[str]):
        super().__init__("invalid bundle:\n  " + "\n  ".join(problems))
        self.problems = problems


@cache
def _validator(name: str) -> Draft202012Validator:
    registry: Registry = Registry()
    schemas = {}
    for path in SCHEMA_DIR.glob("*.schema.json"):
        schema = json.loads(path.read_text())
        schemas[path.name] = schema
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
    return Draft202012Validator(schemas[name], registry=registry)


def _schema_problems(doc: Any, schema: str, where: str) -> list[str]:
    errors = sorted(_validator(schema).iter_errors(doc), key=lambda e: list(e.absolute_path))
    out = []
    for e in errors:
        path = "/".join(str(p) for p in e.absolute_path)
        out.append(f"{where}{'#/' + path if path else ''}: {e.message}")
    return out


@dataclass
class Bundle:
    root: Path
    manifest: dict
    report: dict
    evidence: dict[tuple[str, int], Record] = field(default_factory=dict)

    @property
    def disclosure(self) -> str:
        return self.manifest["disclosure"]

    @property
    def title(self) -> str:
        return self.manifest["report"]["title"]

    def record(self, evidence_id: str, version: int) -> Record:
        return self.evidence[(evidence_id, version)]

    def pinned(self, pins: list[dict]) -> dict[str, Record]:
        """Records for a claim's or chart's pins, keyed by evidence id."""
        return {p["id"]: self.record(p["id"], p["version"]) for p in pins}

    def claims_by_id(self) -> dict[str, dict]:
        return {c["id"]: c for c in self.report["claims"]}


def _load_json(path: Path, problems: list[str]) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        problems.append(f"{path.name}: missing")
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        problems.append(f"{path.name}: not valid JSON ({e})")
    return None


def _chart_data(spec: Any, path: str = "spec") -> Iterator[tuple[str, Any]]:
    """Every `data` value in a Vega-Lite spec, with its location."""
    if isinstance(spec, dict):
        for k, v in spec.items():
            if k == "data":
                yield f"{path}.data", v
            else:
                yield from _chart_data(v, f"{path}.{k}")
    elif isinstance(spec, list):
        for i, v in enumerate(spec):
            yield from _chart_data(v, f"{path}[{i}]")


def _chart_fetches(spec: Any, path: str = "spec") -> Iterator[str]:
    """Places a spec could make the renderer fetch something.

    vl-convert blocks data URLs when told to, but it still loads image marks,
    so the reader refuses them here.
    """
    if isinstance(spec, dict):
        for k, v in spec.items():
            if k == "url":
                yield f"{path}.url"
            elif k == "mark" and (v == "image" or (isinstance(v, dict) and v.get("type") == "image")):
                yield f"{path}.mark (image marks load external files)"
            else:
                yield from _chart_fetches(v, f"{path}.{k}")
    elif isinstance(spec, list):
        for i, v in enumerate(spec):
            yield from _chart_fetches(v, f"{path}[{i}]")


def load(root: str | Path) -> Bundle:
    root = Path(root)
    problems: list[str] = []
    if not root.is_dir():
        raise BundleError([f"{root}: not a directory"])

    manifest = _load_json(root / "manifest.json", problems)
    report = _load_json(root / "report.json", problems)
    if manifest is not None:
        problems += _schema_problems(manifest, "manifest.schema.json", "manifest.json")
    if report is not None:
        problems += _schema_problems(report, "report.schema.json", "report.json")
    if problems:
        raise BundleError(problems)
    assert manifest is not None and report is not None

    bundle = Bundle(root, manifest, report)
    level = manifest["disclosure"]

    # Rule 1, 3-8: evidence files.
    for entry in manifest["evidence"]:
        ident = (entry["id"], entry["version"])
        where = entry["file"]
        if ident in bundle.evidence:
            problems.append(f"manifest.json: evidence {ident[0]} v{ident[1]} is listed twice")
            continue
        if where != f"evidence/{ident[0]}.v{ident[1]}.json":
            problems.append(f"manifest.json: {where} does not match {ident[0]} v{ident[1]}")
            continue
        doc = _load_json(root / where, problems)
        if doc is None:
            continue
        schema_problems = _schema_problems(doc, "evidence.schema.json", where)
        if schema_problems:
            problems += schema_problems
            continue
        if (doc["id"], doc["version"]) != ident:
            problems.append(f"{where}: holds {doc['id']} v{doc['version']}")
            continue
        if doc["disclosure"]["level"] != level:
            problems.append(f"{where}: disclosure {doc['disclosure']['level']} differs from the manifest's {level}")
            continue
        try:
            record = Record.from_json(doc)
        except EvidenceError as e:
            problems.append(f"{where}: {e}")
            continue
        problems += _check_disclosure(where, doc, record, level)
        bundle.evidence[ident] = record

    # Rule 2, 9, 10: references from the report.
    listed = set(bundle.evidence)
    known_ids = {(e["id"], e["version"]) for e in manifest["evidence"]}

    def check_pins(pins: list[dict], where: str) -> None:
        seen: dict[str, int] = {}
        for p in pins:
            ident = (p["id"], p["version"])
            if ident not in known_ids:
                problems.append(f"{where}: pins {p['id']} v{p['version']}, which the bundle does not list")
            if p["id"] in seen and seen[p["id"]] != p["version"]:
                problems.append(f"{where}: pins two versions of {p['id']}")
            seen[p["id"]] = p["version"]

    block_ids = set()
    for block in report["blocks"]:
        where = f"report.json block {block['id']}"
        if block["id"] in block_ids:
            problems.append(f"{where}: duplicate block id")
        block_ids.add(block["id"])
        if block["type"] in ("chart", "image"):
            check_pins(block["evidence"], where)
        if block["type"] == "chart":
            if "datasets" in block["spec"]:
                problems.append(f"{where}: chart specs may not set datasets")
            for loc in _chart_fetches(block["spec"]):
                problems.append(f"{where}: {loc} is not allowed; charts draw only from evidence")
            pinned = {(p["id"], p["version"]) for p in block["evidence"]}
            found = False
            for loc, data in _chart_data(block["spec"]):
                ref = data.get("evidence") if isinstance(data, dict) and set(data) == {"evidence"} else None
                if not isinstance(ref, dict) or set(ref) != {"id", "version"}:
                    problems.append(f"{where}: {loc} must be {{\"evidence\": {{\"id\", \"version\"}}}}")
                    continue
                found = True
                if (ref["id"], ref["version"]) not in pinned:
                    problems.append(f"{where}: {loc} uses {ref['id']} v{ref['version']}, which the block does not pin")
            if not found:
                problems.append(f"{where}: chart has no evidence data")
        if block["type"] == "image":
            src = (root / block["src"]).resolve()
            if not src.is_relative_to((root / "assets").resolve()) or not src.is_file():
                problems.append(f"{where}: image {block['src']} is not a file in assets/")

    claim_ids = set()
    for claim in report["claims"]:
        where = f"report.json claim {claim['id']}"
        if claim["id"] in claim_ids:
            problems.append(f"{where}: duplicate claim id")
        claim_ids.add(claim["id"])
        if claim["block_id"] not in block_ids:
            problems.append(f"{where}: block {claim['block_id']} does not exist")
        check_pins(claim["evidence"], where)
        for n in claim["numbers"]:
            if n["tier"] == "direct":
                c = n["cell"]
                ident = (c["evidence"], c["version"])
                if ident not in known_ids:
                    problems.append(f"{where}: {n['text']!r} cites {ident[0]} v{ident[1]}, which the bundle does not list")
                elif ident not in {(p["id"], p["version"]) for p in claim["evidence"]}:
                    problems.append(f"{where}: {n['text']!r} cites {ident[0]} v{ident[1]}, which the claim does not pin")

    if problems:
        raise BundleError(problems)
    assert listed == known_ids
    return bundle


def _check_disclosure(where: str, doc: dict, record: Record, level: str) -> list[str]:
    rows = doc.get("rows")
    withheld = doc["disclosure"]["withheld_rows"]
    if level == "metadata":
        return [f"{where}: rows must be absent at metadata disclosure"] if rows is not None else []
    if rows is None:
        return [f"{where}: rows are required at {level} disclosure"]
    if level == "full":
        out = []
        if len(rows) != doc["row_count"]:
            out.append(f"{where}: row_count is {doc['row_count']} but {len(rows)} rows are present")
        if withheld or doc["disclosure"].get("withheld_columns"):
            out.append(f"{where}: nothing may be withheld at full disclosure")
        if not out and record.compute_hash() != doc["data_hash"]:
            out.append(f"{where}: data_hash does not match the rows (recomputed {record.compute_hash()})")
        return out
    # cited
    if withheld != doc["row_count"] - len(rows):
        return [f"{where}: withheld_rows is {withheld}, expected {doc['row_count'] - len(rows)}"]
    return []


def resolve_direct(bundle: Bundle, cell: dict):
    """The value a `direct` number cites, or raise LookupFailed."""
    record = bundle.record(cell["evidence"], cell["version"])
    return record.cell(record.coerce_json_key(cell["key"]), cell["column"])


__all__ = ["Bundle", "BundleError", "LookupFailed", "load", "resolve_direct"]
