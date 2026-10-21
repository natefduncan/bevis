import json
import shutil
from pathlib import Path

import pytest

from bevis import bundle
from bevis.disclosure import restrict

EXAMPLE = Path(__file__).parents[1] / "examples" / "revenue-review"


class BundleDir:
    """A writable copy of the example bundle."""

    def __init__(self, root: Path):
        self.root = root

    def read(self, name: str) -> dict:
        return json.loads((self.root / name).read_text())

    def write(self, name: str, doc: dict) -> None:
        (self.root / name).write_text(json.dumps(doc, indent=2))

    def edit(self, name: str, fn) -> None:
        doc = self.read(name)
        fn(doc)
        self.write(name, doc)

    def load(self):
        return bundle.load(self.root)

    def restrict(self, level: str) -> None:
        """Rewrite the bundle as the host would export it at `level`."""
        b = self.load()
        docs = {ident: r.raw for ident, r in b.evidence.items()}
        for (eid, ver), doc in restrict(b.report, docs, level).items():
            self.write(f"evidence/{eid}.v{ver}.json", doc)
        self.edit("manifest.json", lambda m: m.update(disclosure=level))


@pytest.fixture
def example(tmp_path) -> BundleDir:
    dst = tmp_path / "bundle"
    shutil.copytree(EXAMPLE, dst, ignore=shutil.ignore_patterns("report.pdf"))
    return BundleDir(dst)
