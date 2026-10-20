"""Command line: `bevis export pdf <bundle-dir>`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import bundle as bundle_mod
from .pdf import RenderError, render_pdf


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bevis")
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="export a bundle").add_subparsers(dest="format", required=True)
    pdf = export.add_parser("pdf", help="render a bundle to PDF")
    pdf.add_argument("bundle", type=Path, help="bundle directory")
    pdf.add_argument("-o", "--output", type=Path, help="output file (default: <bundle>/report.pdf)")
    args = parser.parse_args(argv)

    try:
        b = bundle_mod.load(args.bundle)
        out = args.output or args.bundle / "report.pdf"
        render_pdf(b, out)
    except bundle_mod.BundleError as e:
        print(f"bevis: {e}", file=sys.stderr)
        return 1
    except RenderError as e:
        print(f"bevis: {e}", file=sys.stderr)
        return 1
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
