"""Command line: `bevis validate <bundle-dir>` and `bevis export pdf <bundle-dir>`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import bundle as bundle_mod
from .markdown import MarkdownError
from .pdf import RenderError, render_pdf
from .validate import Result, Subject, validate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bevis")
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="export a bundle").add_subparsers(dest="format", required=True)
    pdf = export.add_parser("pdf", help="render a bundle to PDF")
    pdf.add_argument("bundle", type=Path, help="bundle directory")
    pdf.add_argument("-o", "--output", type=Path, help="output file (default: <bundle>/report.pdf)")
    check = commands.add_parser("validate", help="validate a bundle's claims against its evidence")
    check.add_argument("bundle", type=Path, help="bundle directory")
    check.add_argument("--json", action="store_true", help="print findings as JSON")
    args = parser.parse_args(argv)

    if args.command == "validate":
        return _validate(args)
    try:
        b = bundle_mod.load(args.bundle)
        out = args.output or args.bundle / "report.pdf"
        render_pdf(b, out)
    except bundle_mod.BundleError as e:
        print(f"bevis: {e}", file=sys.stderr)
        return 1
    except (RenderError, MarkdownError) as e:
        print(f"bevis: {e}", file=sys.stderr)
        return 1
    print(out)
    return 0


def _validate(args) -> int:
    try:
        b = bundle_mod.load(args.bundle)
        result = validate(Subject.from_bundle(b))
    except (bundle_mod.BundleError, MarkdownError) as e:
        print(f"bevis: {e}", file=sys.stderr)
        return 1
    recorded = b.manifest["validation"]
    if args.json:
        print(json.dumps({
            "counts": result.counts(),
            "recorded": recorded,
            "findings": [f.to_json() for f in result.findings],
        }, indent=2, ensure_ascii=False))
    else:
        _print_result(result, recorded)
    return 0 if result.ok else 1


def _print_result(result: Result, recorded: dict) -> None:
    for f in result.findings:
        where = f.where()
        print(f"{f.severity:<8} {f.code:<24} {where + ': ' if where else ''}{f.message}")
    c = result.counts()
    if result.findings:
        print()
    print(f"{c['errors']} errors, {c['warnings']} warnings, {c['info']} info")
    if c != recorded:
        print(
            f"note: the bundle recorded {recorded['errors']} errors, {recorded['warnings']} warnings, "
            f"{recorded['info']} info at publish"
        )


if __name__ == "__main__":
    sys.exit(main())
