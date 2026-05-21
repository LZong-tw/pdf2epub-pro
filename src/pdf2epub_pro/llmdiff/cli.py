"""``pdf2epub-llmdiff`` — sample-based visual/semantic LLM diff CLI.

Usage::

    python -m pdf2epub_pro.llmdiff <pdf> <epub> --n=5 --out=findings.md \\
        [--dry-run] [--model=claude-haiku-4-5-20251001]

The tool samples N pages (first, last, N-2 interior), aligns each with the
corresponding EPUB section, renders both to PNG, and asks an LLM to flag any
defects. Findings are aggregated into a markdown report.

Exit status is **always 0** when the run completes — this is a review tool,
not a gate. Inspect the report and decide whether to act.

No ``ANTHROPIC_API_KEY`` set (or ``--dry-run`` passed) means the dry-run
stub is used and the report is written with an empty findings list. This
is the intended way to validate the prompt/request structure without
paying for inference.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .chunker import Chunk, sample_chunks
from .differ import (
    DEFAULT_MODEL,
    DEFAULT_SYSTEM_PROMPT,
    Finding,
    diff_chunk,
    dry_run_llm,
)


def _make_anthropic_call_fn(api_key: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Build a real LLM caller using the optional ``anthropic`` SDK.

    Imported lazily so ``anthropic`` stays an optional dependency: users
    that only want the dry-run scaffold never need to install it.
    """
    try:
        anthropic = importlib.import_module("anthropic")
    except ImportError as exc:  # pragma: no cover - exercised when SDK absent
        raise SystemExit(
            "anthropic SDK not installed. Run `pip install anthropic` "
            "or pass --dry-run to use the offline stub."
        ) from exc

    client = anthropic.Anthropic(api_key=api_key)

    def call(req: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover - needs key
        msg = client.messages.create(**req)
        # SDK objects expose ``model_dump`` (pydantic v2) — fall back to dict().
        if hasattr(msg, "model_dump"):
            return msg.model_dump()
        return dict(msg)

    return call


def _render_chunk_section(chunk: Chunk, findings: list[Finding]) -> str:
    out: list[str] = []
    out.append(f"## PDF page {chunk.pdf_page} → `{chunk.epub_file or '<unaligned>'}`")
    out.append("")
    if chunk.anchor_text:
        out.append(f"_anchor: `{chunk.anchor_text}`_")
    else:
        out.append("_anchor: (no suitable phrase found on this page)_")
    start, end = chunk.epub_para_range
    if chunk.epub_file:
        out.append(f"_EPUB paragraph range: [{start}, {end})_")
    out.append("")
    if not findings:
        out.append("- _no findings_")
        out.append("")
        return "\n".join(out)
    for f in findings:
        out.append(f"- **{f.severity.upper()}** `{f.type}` — {f.description}")
    out.append("")
    return "\n".join(out)


def _write_report(out_path: Path, pdf_path: Path, epub_path: Path,
                  model: str, dry_run: bool,
                  chunks_findings: list[tuple[Chunk, list[Finding]]]) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    total_findings = sum(len(f) for _, f in chunks_findings)
    by_sev: dict[str, int] = {}
    for _, fs in chunks_findings:
        for f in fs:
            by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
    lines: list[str] = [
        f"# pdf2epub-llmdiff report",
        "",
        f"- **generated:** {now}",
        f"- **pdf:** `{pdf_path}`",
        f"- **epub:** `{epub_path}`",
        f"- **model:** `{model}`",
        f"- **mode:** {'dry-run (no API call)' if dry_run else 'live'}",
        f"- **chunks sampled:** {len(chunks_findings)}",
        f"- **total findings:** {total_findings}"
        + (f" ({', '.join(f'{k}={v}' for k, v in sorted(by_sev.items()))})"
           if by_sev else ""),
        "",
    ]
    for chunk, findings in chunks_findings:
        lines.append(_render_chunk_section(chunk, findings))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pdf2epub-llmdiff",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("pdf", help="Source PDF.")
    p.add_argument("epub", help="EPUB produced by the pipeline.")
    p.add_argument("-n", "--n", type=int, default=5,
                   help="Pages to sample (default 5).")
    p.add_argument("--out", default="llmdiff-findings.md",
                   help="Markdown report destination.")
    p.add_argument("--dry-run", action="store_true",
                   help="Skip the LLM call; emit a structural report only.")
    p.add_argument("--model", default=DEFAULT_MODEL,
                   help=f"Anthropic model id (default {DEFAULT_MODEL}).")
    p.add_argument("--render-dir", default=None,
                   help="Where to cache PNG renders (default: alongside the PDF).")
    p.add_argument("--dpi", type=int, default=150,
                   help="PDF render DPI (default 150).")
    p.add_argument("--system-prompt-file", default=None,
                   help="Path to a file whose contents replace the default "
                        "system prompt. Useful for prompt iteration.")
    p.add_argument("--dump-request", action="store_true",
                   help="Print the first chunk's request body as JSON "
                        "(base64 image payloads truncated) and exit.")
    return p


def _truncate_request_for_dump(req: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``req`` with base64 image data shortened for printing."""
    out = json.loads(json.dumps(req))  # deep copy via json round-trip
    for msg in out.get("messages", []):
        for block in msg.get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "image":
                src = block.get("source", {})
                data = src.get("data", "")
                if isinstance(data, str) and len(data) > 64:
                    src["data"] = data[:32] + f"...<truncated {len(data)} bytes>"
    return out


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    pdf_path = Path(args.pdf).resolve()
    epub_path = Path(args.epub).resolve()
    out_path = Path(args.out).resolve()

    if not pdf_path.exists():
        print(f"error: PDF not found: {pdf_path}", file=sys.stderr)
        return 2
    if not epub_path.exists():
        print(f"error: EPUB not found: {epub_path}", file=sys.stderr)
        return 2

    system_prompt: str | None = None
    if args.system_prompt_file:
        system_prompt = Path(args.system_prompt_file).read_text(encoding="utf-8")

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    dry_run = args.dry_run or not api_key
    if dry_run:
        llm_call_fn: Callable[[dict[str, Any]], dict[str, Any]] = dry_run_llm
    else:
        llm_call_fn = _make_anthropic_call_fn(api_key)

    chunks = sample_chunks(pdf_path, epub_path, n=args.n)
    if not chunks:
        print("error: no chunks could be sampled (empty PDF?).", file=sys.stderr)
        return 2

    if args.dump_request:
        # Useful for prompt-engineering iterations: render & build one
        # request body, dump it, do nothing else.
        from .differ import build_request
        from .renderer import render_epub_chunk, render_pdf_page
        rdir = Path(args.render_dir or (pdf_path.parent / ".llmdiff-renders"))
        rdir.mkdir(parents=True, exist_ok=True)
        first = chunks[0]
        pdf_img = rdir / f"pdf_p{first.pdf_page:04d}.png"
        epub_img = rdir / f"epub_p{first.pdf_page:04d}.png"
        render_pdf_page(pdf_path, first.pdf_page, pdf_img, dpi=args.dpi)
        render_epub_chunk(epub_path, first, epub_img)
        req = build_request(pdf_img, epub_img, model=args.model,
                            system_prompt=system_prompt)
        print(json.dumps(_truncate_request_for_dump(req), indent=2))
        return 0

    results: list[tuple[Chunk, list[Finding]]] = []
    for chunk in chunks:
        findings = diff_chunk(
            chunk, pdf_path, epub_path, llm_call_fn,
            model=args.model,
            system_prompt=system_prompt,
            render_dir=args.render_dir,
            dpi=args.dpi,
        )
        results.append((chunk, findings))

    _write_report(out_path, pdf_path, epub_path, args.model,
                  dry_run, results)
    total = sum(len(f) for _, f in results)
    mode = "dry-run" if dry_run else "live"
    print(f"[llmdiff] {mode}: {len(chunks)} chunks, {total} findings → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
