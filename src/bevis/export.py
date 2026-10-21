"""Write a published version as a bundle (SPEC §10)."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from . import bundle
from .disclosure import restrict

Ident = tuple[str, int]


def pins_of(content: dict) -> set[Ident]:
    """Every evidence version a report's claims, cells, and charts point at."""
    out: set[Ident] = set()
    for claim in content["claims"]:
        out |= {(p["id"], p["version"]) for p in claim["evidence"]}
        for n in claim["numbers"]:
            if n["tier"] == "direct":
                out.add((n["cell"]["evidence"], n["cell"]["version"]))
    for block in content["blocks"]:
        if block["type"] in ("chart", "image"):
            out |= {(p["id"], p["version"]) for p in block["evidence"]}
    return out


def bundle_files(
    *,
    report: dict,  # {"id", "title"}
    version: dict,  # {"number", "published_at", "published_by", "override"}
    validation: dict,  # {"errors", "warnings", "info"}
    content: dict,  # {"blocks", "claims", "non_data"}
    docs: dict[Ident, dict],  # full-disclosure evidence documents for every pin
    level: str,
) -> dict[str, bytes]:
    def encode(doc: dict) -> bytes:
        return (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode()

    restricted = restrict(content, docs, level)
    entries = sorted(restricted)
    manifest = {
        "schema_version": 1,
        "report": report,
        "version": version,
        "disclosure": level,
        "validation": validation,
        "evidence": [{"id": i, "version": v, "file": f"evidence/{i}.v{v}.json"} for i, v in entries],
    }
    files = {"manifest.json": encode(manifest), "report.json": encode(content)}
    for i, v in entries:
        files[f"evidence/{i}.v{v}.json"] = encode(restricted[(i, v)])
    return files


def write_dir(root: Path, files: dict[str, bytes]) -> bundle.Bundle:
    """Write the files and read them back. A bundle that does not load is a bug."""
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return bundle.load(root)


def zip_bytes(files: dict[str, bytes], prefix: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(files):
            z.writestr(f"{prefix}/{name}", files[name])
    return buf.getvalue()
