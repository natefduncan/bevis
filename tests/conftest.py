import json
import shutil
from pathlib import Path

import pytest

from bevis import bundle
from bevis.cited import citations

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
        """Rewrite the bundle as the host would export it at `level`.

        Charts count as citing every row they draw, and the columns they encode.
        """
        original = self.load()
        cites = citations(original)
        self.edit("manifest.json", lambda m: m.update(disclosure=level))
        for (eid, ver), c in cites.items():
            name = f"evidence/{eid}.v{ver}.json"
            doc = self.read(name)
            doc["disclosure"]["level"] = level
            if level == "metadata":
                del doc["rows"]
            elif level == "cited":
                names = [col["name"] for col in doc["columns"]]
                keep = [n for n in names if n in set(doc["key"]) | c.claim_columns | c.chart_columns]
                key_idx = [names.index(k) for k in doc["key"]]
                record = original.record(eid, ver)
                rows = []
                for raw, typed in zip(doc["rows"], record.rows):
                    key = tuple(typed[i].canonical() for i in key_idx)
                    if c.charted or key in c.cells:
                        rows.append([raw[names.index(n)] for n in keep])
                doc["columns"] = [col for col in doc["columns"] if col["name"] in keep]
                doc["rows"] = rows
                doc["disclosure"]["withheld_rows"] = doc["row_count"] - len(rows)
                doc["disclosure"]["withheld_columns"] = [n for n in names if n not in keep]
            self.write(name, doc)


@pytest.fixture
def example(tmp_path) -> BundleDir:
    dst = tmp_path / "bundle"
    shutil.copytree(EXAMPLE, dst, ignore=shutil.ignore_patterns("report.pdf"))
    return BundleDir(dst)
