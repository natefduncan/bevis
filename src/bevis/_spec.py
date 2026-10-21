"""Locate the normative files under spec/."""

from pathlib import Path

_HERE = Path(__file__).resolve().parent


def spec_dir(name: str) -> Path:
    """Wheels carry spec/ inside the package; a source checkout reads the repo's."""
    for root in (_HERE / "spec", _HERE.parents[1] / "spec"):
        if (root / name).is_dir():
            return root / name
    raise FileNotFoundError(f"spec/{name} not found")
