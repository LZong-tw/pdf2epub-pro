"""``pdf2epub-audit`` console entry point.

Usage::

    pdf2epub-audit --md path/to/file.md           # default: text output
    pdf2epub-audit --epub book.epub --json
    pdf2epub-audit --md a.md --epub b.epub --md-report

Exit codes:

* ``0`` — no warnings or errors
* ``1`` — at least one warn finding (no errors)
* ``2`` — at least one error finding
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

from .framework import (
    Finding,
    exit_code_for,
    registered_epub_detectors,
    registered_md_detectors,
    run_all_epub,
    run_all_md,
)


def _fmt_text(findings: Iterable[Finding]) -> str:
    out: list[str] = []
    for f in findings:
        loc = f.file or "?"
        if f.line is not None:
            loc = f"{loc}:{f.line}"
        out.append(f"[{f.severity.upper():5}] {f.detector} @ {loc} — {f.message}")
        if f.snippet:
            for snip_line in f.snippet.splitlines() or [f.snippet]:
                out.append(f"    | {snip_line}")
    return "\n".join(out)


def _fmt_json(findings: Iterable[Finding]) -> str:
    return json.dumps([f.to_dict() for f in findings], indent=2, ensure_ascii=False)


def _fmt_md_report(findings: list[Finding]) -> str:
    by_detector: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        by_detector[f.detector].append(f)
    sev_counts = Counter(f.severity for f in findings)
    lines = [
        "# pdf2epub-audit report",
        "",
        f"Total findings: **{len(findings)}** "
        f"(error: {sev_counts.get('error', 0)}, "
        f"warn: {sev_counts.get('warn', 0)}, "
        f"info: {sev_counts.get('info', 0)})",
        "",
    ]
    if not findings:
        lines.append("_No defects detected._")
        return "\n".join(lines) + "\n"
    for detector, group in sorted(by_detector.items()):
        lines.append(f"## `{detector}` — {len(group)} finding(s)")
        lines.append("")
        for f in group:
            loc = f.file or "?"
            if f.line is not None:
                loc = f"{loc}:{f.line}"
            lines.append(f"- **[{f.severity}]** `{loc}` — {f.message}")
            if f.snippet:
                # Use a fenced block so the snippet is verbatim.
                lines.append("  ```")
                for snip_line in f.snippet.splitlines() or [f.snippet]:
                    lines.append(f"  {snip_line}")
                lines.append("  ```")
        lines.append("")
    return "\n".join(lines) + "\n"


def _list_detectors() -> str:
    lines = ["# Markdown detectors"]
    for cls in registered_md_detectors():
        lines.append(f"  - {cls.name}: {cls.description}")
    lines.append("")
    lines.append("# EPUB detectors")
    for cls in registered_epub_detectors():
        lines.append(f"  - {cls.name}: {cls.description}")
    return "\n".join(lines)


def _force_utf8_stdout() -> None:
    """Reconfigure ``sys.stdout`` to UTF-8 so emoji / snippets don't crash on cp950."""
    stream = sys.stdout
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
            return
        except Exception:
            pass
    try:
        sys.stdout = io.TextIOWrapper(stream.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass  # fall back to whatever the host gave us


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdout()
    p = argparse.ArgumentParser(
        prog="pdf2epub-audit",
        description="Run defect detectors against a markdown file and/or an EPUB.",
    )
    p.add_argument("--md", type=Path, help="markdown file to audit")
    p.add_argument("--epub", type=Path, help=".epub file to audit")
    fmt = p.add_mutually_exclusive_group()
    fmt.add_argument("--json", dest="as_json", action="store_true",
                     help="emit findings as JSON")
    fmt.add_argument("--md-report", dest="as_md", action="store_true",
                     help="emit a grouped markdown report")
    p.add_argument("--list-detectors", action="store_true",
                   help="print registered detectors and exit")
    p.add_argument("--only", action="append", default=None, metavar="NAME",
                   help="run only the named detector(s); may repeat")
    args = p.parse_args(argv)

    if args.list_detectors:
        print(_list_detectors())
        return 0

    if not args.md and not args.epub:
        p.error("at least one of --md / --epub is required")

    findings: list[Finding] = []
    if args.md:
        detectors = registered_md_detectors()
        if args.only:
            detectors = [d for d in detectors if d.name in args.only]
        findings.extend(run_all_md(args.md, detectors))
    if args.epub:
        detectors = registered_epub_detectors()
        if args.only:
            detectors = [d for d in detectors if d.name in args.only]
        findings.extend(run_all_epub(args.epub, detectors))

    if args.as_json:
        print(_fmt_json(findings))
    elif args.as_md:
        print(_fmt_md_report(findings))
    else:
        print(_fmt_text(findings) or "No defects detected.")
    return exit_code_for(findings)


if __name__ == "__main__":
    raise SystemExit(main())
