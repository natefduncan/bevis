"""The web view's pure helpers, under node's test runner (tests/web)."""

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def test_textdiff_helpers():
    proc = subprocess.run(["node", "--test", str(Path(__file__).parent / "web")], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
