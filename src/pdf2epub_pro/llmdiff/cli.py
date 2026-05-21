"""``pdf2epub-llmdiff`` — sample-based visual/semantic LLM diff CLI.

Usage::

    python -m pdf2epub_pro.llmdiff <pdf> <epub> --n=5 --out=findings.md \\
        [--dry-run] [--model=claude-haiku-4-5-20251001] \\
        [--backend {claude_cli,codex_cli,anthropic_api,dry_run}]

The tool samples N pages (first, last, N-2 interior), aligns each with the
corresponding EPUB section, renders both to PNG, and asks an LLM to flag any
defects. Findings are aggregated into a markdown report.

Exit status is **always 0** when the run completes — this is a review tool,
not a gate. Inspect the report and decide whether to act.

Backend selection (preferred → fallback) when ``--backend`` is omitted:

1. ``claude_cli``     — shells out to the local ``claude -p`` CLI if on PATH.
2. ``codex_cli``      — shells out to the local ``codex -p`` CLI if on PATH.
3. ``anthropic_api``  — uses the optional ``anthropic`` SDK with
   ``ANTHROPIC_API_KEY`` from env.
4. ``dry_run``        — offline stub; report has an empty findings list.

The CLI paths are free for users with a Claude Code or Codex CLI
subscription, so they are preferred over the metered API key path.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .chunker import Chunk, sample_chunks
from .differ import (
    DEFAULT_MODEL,
    DEFAULT_SYSTEM_PROMPT,
    Finding,
    build_cli_prompt,
    claude_cli_call_fn,
    codex_cli_call_fn,
    diff_chunk,
    dry_run_llm,
)

# Names accepted by --backend. ``"auto"`` triggers the selection chain.
BACKENDS = ("auto", "claude_cli", "codex_cli", "anthropic_api", "dry_run")


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
        # diff_chunk piggy-backs the CLI prompt onto the same dict; the
        # Anthropic SDK rejects unknown kwargs, so drop it here.
        sdk_req = {k: v for k, v in req.items() if k != "cli_prompt"}
        msg = client.messages.create(**sdk_req)
        # SDK objects expose ``model_dump`` (pydantic v2) — fall back to dict().
        if hasattr(msg, "model_dump"):
            return msg.model_dump()
        return dict(msg)

    return call


def _anthropic_sdk_available() -> bool:
    """Return True when the optional ``anthropic`` SDK can be imported."""
    try:
        importlib.import_module("anthropic")
    except ImportError:
        return False
    return True


def _select_backend(explicit: str, *, dry_run_flag: bool) -> str:
    """Resolve ``--backend`` (possibly ``"auto"``) to a concrete backend name.

    Order when ``explicit == "auto"``:
      1. ``claude_cli``     if ``shutil.which("claude")`` is non-None.
      2. ``codex_cli``      if ``shutil.which("codex")`` is non-None.
      3. ``anthropic_api``  if ``ANTHROPIC_API_KEY`` is set *and* the
         ``anthropic`` package imports.
      4. ``dry_run`` otherwise.

    ``--dry-run`` is an unconditional short-circuit and always wins. It
    is the historical way to force offline mode and we don't break that
    behaviour just because the user has ``claude`` on PATH.
    """
    if dry_run_flag:
        return "dry_run"
    if explicit and explicit != "auto":
        return explicit
    if shutil.which("claude"):
        return "claude_cli"
    if shutil.which("codex"):
        return "codex_cli"
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if api_key and _anthropic_sdk_available():
        return "anthropic_api"
    return "dry_run"


def _build_call_fn(backend: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Map a resolved backend name to the matching ``llm_call_fn``."""
    if backend == "dry_run":
        return dry_run_llm
    if backend == "claude_cli":
        return claude_cli_call_fn
    if backend == "codex_cli":
        return codex_cli_call_fn
    if backend == "anthropic_api":
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise SystemExit(
                "anthropic_api backend requested but ANTHROPIC_API_KEY "
                "is not set. Set the env var or pick a different "
                "--backend."
            )
        return _make_anthropic_call_fn(api_key)
    raise SystemExit(f"unknown backend: {backend!r}")


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
                   help="Print the first chunk's request body and exit. "
                        "For API backends this is the JSON Messages-API "
                        "payload (base64 image data truncated); for CLI "
                        "backends it is the prompt string passed to "
                        "``claude -p`` / ``codex -p``.")
    p.add_argument("--backend", choices=BACKENDS, default="auto",
                   help="Which LLM backend to use. Default ``auto`` prefers "
                        "the local ``claude`` CLI, then ``codex``, then the "
                        "Anthropic API, then the offline dry-run stub. "
                        "``--dry-run`` always wins over ``--backend``.")
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

    backend = _select_backend(args.backend, dry_run_flag=args.dry_run)
    dry_run = backend == "dry_run"

    chunks = sample_chunks(pdf_path, epub_path, n=args.n)
    if not chunks:
        print("error: no chunks could be sampled (empty PDF?).", file=sys.stderr)
        return 2

    if args.dump_request:
        # Prompt-engineering iteration: render & build one request payload,
        # dump it, do nothing else. The dumped shape depends on the
        # backend — JSON for the API path, raw prompt string for the CLIs.
        from .differ import build_request
        from .renderer import render_epub_chunk, render_pdf_page
        rdir = Path(args.render_dir or (pdf_path.parent / ".llmdiff-renders"))
        rdir.mkdir(parents=True, exist_ok=True)
        first = chunks[0]
        pdf_img = rdir / f"pdf_p{first.pdf_page:04d}.png"
        epub_img = rdir / f"epub_p{first.pdf_page:04d}.png"
        render_pdf_page(pdf_path, first.pdf_page, pdf_img, dpi=args.dpi)
        render_epub_chunk(epub_path, first, epub_img)
        if backend in ("claude_cli", "codex_cli"):
            print(build_cli_prompt(pdf_img, epub_img))
        else:
            req = build_request(pdf_img, epub_img, model=args.model,
                                system_prompt=system_prompt)
            print(json.dumps(_truncate_request_for_dump(req), indent=2))
        return 0

    # Build the call function lazily — after ``--dump-request`` has had
    # its chance to short-circuit — so the SDK import only happens when
    # we actually intend to call the API.
    llm_call_fn: Callable[[dict[str, Any]], dict[str, Any]] = (
        _build_call_fn(backend)
    )

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
    mode = "dry-run" if dry_run else f"live ({backend})"
    print(f"[llmdiff] {mode}: {len(chunks)} chunks, {total} findings → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
