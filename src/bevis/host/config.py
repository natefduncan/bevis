"""Host settings, read from the environment (SPEC §2: secrets never live in the database)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    max_rows: int = 10_000  # per evidence record (SPEC §3.4)
    max_bytes: int = 5 * 1024 * 1024

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bevis.db"

    @property
    def export_dir(self) -> Path:
        return self.data_dir / "exports"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_dir=Path(os.environ.get("BEVIS_DATA_DIR", "bevis-data")),
            max_rows=int(os.environ.get("BEVIS_MAX_ROWS", 10_000)),
            max_bytes=int(os.environ.get("BEVIS_MAX_BYTES", 5 * 1024 * 1024)),
        )
