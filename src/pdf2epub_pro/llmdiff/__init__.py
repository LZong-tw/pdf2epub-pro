"""pdf2epub_pro.llmdiff — L3 visual/semantic diff between an EPUB and its PDF.

This is the most expensive tier in our four-tier defect-coverage strategy
(L0 regex tests → L1 corpus heuristics → L2 audit detectors → **L3 LLM diff**)
and is reserved for milestone reviews only. The harness samples a handful of
pages from the source PDF, locates the corresponding section in the EPUB,
renders both sides to images, and asks an LLM to flag any text that was
dropped, mis-ordered, mis-formatted, mis-labeled, or visually wrong.

Cost estimate (claude-haiku-4-5-20251001, $1/MTok in, $5/MTok out, May 2026):

* one chunk ≈ 2–3 image input tokens + ~500 output tokens ≈ $0.003–$0.008.
* default ``--n=5`` ≈ **$0.02–0.04 per book review pass**.
* aggressive ``--n=20`` ≈ $0.08–0.16 per book.

The :func:`differ.dry_run_llm` stub returns an empty findings list, so the
whole pipeline runs offline end-to-end without an API key — useful in CI and
when validating prompt structure.
"""
from .chunker import Chunk, sample_chunks
from .differ import (
    DEFAULT_SYSTEM_PROMPT,
    Finding,
    build_request,
    diff_chunk,
    dry_run_llm,
)
from .renderer import render_epub_chunk, render_pdf_page

__all__ = [
    "Chunk",
    "DEFAULT_SYSTEM_PROMPT",
    "Finding",
    "build_request",
    "diff_chunk",
    "dry_run_llm",
    "render_epub_chunk",
    "render_pdf_page",
    "sample_chunks",
]
