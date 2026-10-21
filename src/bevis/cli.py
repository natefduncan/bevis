"""Command line: validate and export bundles, run the host, and manage agent keys."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import bundle as bundle_mod
from .derived import render_html, render_markdown
from .markdown import MarkdownError
from .pdf import RenderError, render_pdf
from .validate import Result, Subject, validate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bevis")
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="export a bundle").add_subparsers(dest="format", required=True)
    for fmt, what in (("pdf", "PDF"), ("md", "GitHub Markdown, with charts in charts/"),
                      ("html", "one self-contained HTML file")):
        sub = export.add_parser(fmt, help=f"render a bundle as {what}")
        sub.add_argument("bundle", type=Path, help="bundle directory")
        sub.add_argument("-o", "--output", type=Path, help=f"output file (default: <bundle>/report.{fmt})")
    check = commands.add_parser("validate", help="validate a bundle's claims against its evidence")
    check.add_argument("bundle", type=Path, help="bundle directory")
    check.add_argument("--json", action="store_true", help="print findings as JSON")
    serve = commands.add_parser("serve", help="run the host (REST at /api, MCP at /mcp)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    backup = commands.add_parser("backup", help="copy the host's database consistently, even while it runs")
    backup.add_argument("dest", type=Path, help="file to write, e.g. backups/bevis-2026-10-21.db")
    keys = commands.add_parser("keys", help="manage agent keys").add_subparsers(dest="action", required=True)
    create = keys.add_parser("create", help="create an agent key and print it once")
    create.add_argument("name", help="who the agent is, e.g. claude-analyst")
    keys.add_parser("list", help="list agent keys")
    revoke = keys.add_parser("revoke", help="revoke an agent key")
    revoke.add_argument("id")
    args = parser.parse_args(argv)

    if args.command == "validate":
        return _validate(args)
    if args.command == "serve":
        return _serve(args)
    if args.command == "keys":
        return _keys(args)
    if args.command == "backup":
        return _backup(args)
    try:
        b = bundle_mod.load(args.bundle)
        out = args.output or args.bundle / f"report.{args.format}"
        {"pdf": render_pdf, "md": render_markdown, "html": render_html}[args.format](b, out)
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


def _serve(args) -> int:
    import uvicorn

    from .host.api import create_app
    from .host.config import Settings

    settings = Settings.from_env()
    print(f"bevis: data in {settings.data_dir.resolve()}", file=sys.stderr)
    uvicorn.run(create_app(settings), host=args.host, port=args.port)
    return 0


def _backup(args) -> int:
    from .host.config import Settings
    from .host.db import Database

    settings = Settings.from_env()
    if not settings.db_path.exists():
        print(f"bevis: no database at {settings.db_path}", file=sys.stderr)
        return 1
    Database(settings.db_path).backup(args.dest)
    print(args.dest)
    return 0


def _keys(args) -> int:
    from .host.config import Settings
    from .host.service import HostError, Service

    service = Service(Settings.from_env())
    if args.action == "create":
        key = service.create_agent_key(args.name)
        print(f"{key['id']}  {key['name']}")
        print(key["secret"])
        print("bevis: the key is shown once; store it where the agent reads its secrets", file=sys.stderr)
    elif args.action == "list":
        for k in service.list_agent_keys():
            print(f"{k['id']}  {k['name']}  created {k['created_at']}" + (f"  revoked {k['revoked_at']}"
                                                                           if k["revoked_at"] else ""))
    else:
        try:
            service.revoke_agent_key(args.id)
        except HostError as e:
            print(f"bevis: {e.message}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
