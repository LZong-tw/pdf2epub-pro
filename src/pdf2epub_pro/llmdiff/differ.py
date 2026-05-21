"""Build the Anthropic Messages-API request body and parse the reply.

The differ is intentionally *transport-agnostic*: it accepts a callable
``llm_call_fn(req_body) -> response_dict`` so the caller decides whether to
use the real ``anthropic`` SDK, a fake for tests, or :func:`dry_run_llm`.

Cost notes are repeated in :mod:`pdf2epub_pro.llmdiff` and the README. At
``claude-haiku-4-5-20251001`` ($1/MTok in, $5/MTok out as of May 2026):

* per-chunk: ~$0.003–0.008 (two PNG inputs + ~500 output tokens)
* per-book at ``--n=5``: ~$0.02–0.04
* per-book at ``--n=20``: ~$0.08–0.16
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .chunker import Chunk
from .renderer import render_epub_chunk, render_pdf_page

DEFAULT_MODEL = "claude-haiku-4-5-20251001"

DEFAULT_SYSTEM_PROMPT = (
    "You are reviewing an EPUB conversion against its source PDF. "
    "Compare the two images of the same content section. Identify any text "
    "that was dropped, mis-ordered, mis-formatted, mis-labeled, or visually "
    "wrong in the EPUB version. Reply with strict JSON: "
    '{"findings": [{"severity": "error|warn|info", '
    '"type": "missing_text|reordered|formatting|...", "description": "..."}]}'
)

DEFAULT_MAX_TOKENS = 1024


@dataclass
class Finding:
    """One reviewer-flagged issue."""

    severity: str  # "error" | "warn" | "info"
    type: str
    description: str
    pdf_page: int = -1
    epub_file: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def _encode_png(path: Path) -> str:
    """Read a PNG file and return base64 (no data: prefix)."""
    return base64.standard_b64encode(Path(path).read_bytes()).decode("ascii")


def build_request(pdf_img: str | Path, epub_img: str | Path,
                  model: str = DEFAULT_MODEL,
                  system_prompt: str | None = None,
                  *, max_tokens: int = DEFAULT_MAX_TOKENS) -> dict[str, Any]:
    """Build the Anthropic Messages API request body.

    Encodes both PNGs inline as base64. Mirrors the shape required by the
    SDK's ``messages.create`` (so the caller can ``client.messages.create(**req)``).
    """
    pdf_b64 = _encode_png(Path(pdf_img))
    epub_b64 = _encode_png(Path(epub_img))
    system = system_prompt if system_prompt is not None else DEFAULT_SYSTEM_PROMPT
    return {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text",
                     "text": "PDF page (source of truth):"},
                    {"type": "image",
                     "source": {
                         "type": "base64",
                         "media_type": "image/png",
                         "data": pdf_b64,
                     }},
                    {"type": "text",
                     "text": "EPUB rendering of the same section:"},
                    {"type": "image",
                     "source": {
                         "type": "base64",
                         "media_type": "image/png",
                         "data": epub_b64,
                     }},
                    {"type": "text",
                     "text": (
                         "List any defects in the EPUB version. "
                         "Return ONLY the JSON object described in the "
                         "system prompt — no prose, no markdown fences."
                     )},
                ],
            }
        ],
    }


def dry_run_llm(_req: dict[str, Any]) -> dict[str, Any]:
    """No-op stub: returns an empty findings reply in the API's shape.

    Used by the CLI when ``--dry-run`` is passed or no ``ANTHROPIC_API_KEY``
    is set, and by tests so the harness runs offline."""
    return {
        "id": "dry_run",
        "type": "message",
        "role": "assistant",
        "model": _req.get("model", DEFAULT_MODEL),
        "content": [
            {"type": "text", "text": json.dumps({"findings": []})}
        ],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _extract_text(response: dict[str, Any]) -> str:
    """Concatenate every ``type=='text'`` block from a Messages-API reply."""
    parts: list[str] = []
    for block in response.get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "text":
            txt = block.get("text", "")
            if txt:
                parts.append(txt)
    return "\n".join(parts)


def parse_findings(response: dict[str, Any], chunk: Chunk) -> list[Finding]:
    """Parse the model's reply into :class:`Finding` objects.

    Tolerates: markdown-fenced JSON, leading/trailing prose, missing fields.
    Anything that fails to parse is wrapped as a single ``info`` finding so
    operators see *something* in the report instead of silent failure.
    """
    text = _extract_text(response).strip()
    if not text:
        return []
    candidate = text
    m = _JSON_FENCE_RE.search(text)
    if m:
        candidate = m.group(1)
    # Find the first ``{`` ... matching closing brace; ignore prose.
    start = candidate.find("{")
    if start > 0:
        candidate = candidate[start:]
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return [Finding(
            severity="info",
            type="parse_error",
            description=f"could not parse LLM reply as JSON: {text[:200]!r}",
            pdf_page=chunk.pdf_page,
            epub_file=chunk.epub_file,
        )]
    items = payload.get("findings", []) if isinstance(payload, dict) else []
    out: list[Finding] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        out.append(Finding(
            severity=str(item.get("severity", "info")),
            type=str(item.get("type", "unknown")),
            description=str(item.get("description", "")),
            pdf_page=chunk.pdf_page,
            epub_file=chunk.epub_file,
            raw=item,
        ))
    return out


def diff_chunk(chunk: Chunk, pdf_path: str | Path, epub_path: str | Path,
               llm_call_fn: Callable[[dict[str, Any]], dict[str, Any]],
               *, model: str = DEFAULT_MODEL,
               system_prompt: str | None = None,
               render_dir: str | Path | None = None,
               dpi: int = 150) -> list[Finding]:
    """Render both sides of a chunk, invoke the LLM, parse findings.

    ``llm_call_fn`` is responsible for *all* network access; pass
    :func:`dry_run_llm` to keep the pipeline offline.
    """
    pdf_p = Path(pdf_path)
    epub_p = Path(epub_path)
    if render_dir is not None:
        rdir = Path(render_dir)
    else:
        # Default: sibling cache directory.
        rdir = pdf_p.parent / ".llmdiff-renders"
    rdir.mkdir(parents=True, exist_ok=True)

    pdf_img = rdir / f"pdf_p{chunk.pdf_page:04d}.png"
    epub_img = rdir / f"epub_p{chunk.pdf_page:04d}.png"
    render_pdf_page(pdf_p, chunk.pdf_page, pdf_img, dpi=dpi)
    render_epub_chunk(epub_p, chunk, epub_img)

    req = build_request(pdf_img, epub_img, model=model,
                        system_prompt=system_prompt)
    response = llm_call_fn(req)
    return parse_findings(response, chunk)


__all__ = [
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_MODEL",
    "DEFAULT_SYSTEM_PROMPT",
    "Finding",
    "build_request",
    "diff_chunk",
    "dry_run_llm",
    "parse_findings",
]
