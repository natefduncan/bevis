"""Checks on Vega-Lite chart specs (SPEC §3.6)."""

from __future__ import annotations

import json
from typing import Any, Iterator

MAX_SPEC_BYTES = 64 * 1024  # the size cap on one serialized spec

# Encoding properties that merge or create rows.
FORBIDDEN_ENCODING = ("aggregate", "bin", "timeUnit", "impute")


def data_values(spec: Any, path: str = "spec") -> Iterator[tuple[str, Any]]:
    """Every `data` value in a spec, with its location."""
    if isinstance(spec, dict):
        for k, v in spec.items():
            if k == "data":
                yield f"{path}.data", v
            else:
                yield from data_values(v, f"{path}.{k}")
    elif isinstance(spec, list):
        for i, v in enumerate(spec):
            yield from data_values(v, f"{path}[{i}]")


def fetches(spec: Any, path: str = "spec") -> Iterator[str]:
    """Places a spec could make the renderer fetch something.

    vl-convert blocks data URLs when told to, but it still loads image marks,
    so they are refused here.
    """
    if isinstance(spec, dict):
        for k, v in spec.items():
            if k == "url":
                yield f"{path}.url"
            elif k == "mark" and (v == "image" or (isinstance(v, dict) and v.get("type") == "image")):
                yield f"{path}.mark (image marks load external files)"
            else:
                yield from fetches(v, f"{path}.{k}")
    elif isinstance(spec, list):
        for i, v in enumerate(spec):
            yield from fetches(v, f"{path}[{i}]")


def data_problems(spec: dict, pins: list[dict]) -> list[str]:
    """Chart data must be pinned evidence, and nothing may be fetched."""
    out = []
    if "datasets" in spec:
        out.append("spec.datasets: charts may not set datasets")
    for loc in fetches(spec):
        out.append(f"{loc} is not allowed; charts draw only from evidence")
    pinned = {(p["id"], p["version"]) for p in pins}
    found = False
    for loc, data in data_values(spec):
        ref = data.get("evidence") if isinstance(data, dict) and set(data) == {"evidence"} else None
        if not isinstance(ref, dict) or set(ref) != {"id", "version"}:
            out.append(f"{loc} must be {{\"evidence\": {{\"id\", \"version\"}}}}")
            continue
        found = True
        if (ref["id"], ref["version"]) not in pinned:
            out.append(f"{loc} uses {ref['id']} v{ref['version']}, which the block does not pin")
    if not found:
        out.append("chart has no evidence data")
    return out


def _encoding_problems(node: Any, path: str) -> Iterator[str]:
    if isinstance(node, dict):
        for k, v in node.items():
            if k in FORBIDDEN_ENCODING and v not in (None, False):
                yield f"{path}.{k}: encodings may not set {k}, which merges or creates rows"
            else:
                yield from _encoding_problems(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _encoding_problems(v, f"{path}[{i}]")


def strictness_problems(spec: Any, path: str = "spec") -> Iterator[tuple[str, str]]:
    """(code, message) for transforms and encodings that break the one-mark-per-row rule."""
    if path == "spec":
        size = len(json.dumps(spec, separators=(",", ":")))
        if size > MAX_SPEC_BYTES:
            yield "chart.size", f"spec is {size:,} bytes; the cap is {MAX_SPEC_BYTES:,}"
    if isinstance(spec, dict):
        for k, v in spec.items():
            where = f"{path}.{k}"
            if k == "transform" and isinstance(v, list):
                for i, t in enumerate(v):
                    if not (isinstance(t, dict) and set(t) == {"filter"}):
                        name = next(iter(t), "?") if isinstance(t, dict) else "?"
                        yield "chart.transform", f"{where}[{i}]: only filter is allowed, not {name}"
            elif k == "encoding":
                for msg in _encoding_problems(v, where):
                    yield "chart.encoding", msg
            elif k != "data":
                yield from strictness_problems(v, where)
    elif isinstance(spec, list):
        for i, v in enumerate(spec):
            yield from strictness_problems(v, f"{path}[{i}]")
