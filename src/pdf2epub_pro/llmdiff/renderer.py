"""Render PDF pages and EPUB chunks to PNG images for visual diffing.

Two paths:

* :func:`render_pdf_page` uses ``pypdfium2`` to rasterize a PDF page at a
  caller-supplied DPI (default 150).
* :func:`render_epub_chunk` prefers a headless Chromium via Playwright so
  the EPUB looks like the actual reading experience. When Playwright is
  not importable (or fails to launch its browser binary) we fall back to a
  text-only PIL render that mimics a paginated monospace layout. The
  fallback is intentionally low-fidelity — its job is to give the LLM
  enough lexical content to spot dropped or reordered text even when the
  pixel-perfect path is unavailable.
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path
from textwrap import wrap

import pypdfium2 as pdfium
from PIL import Image, ImageDraw, ImageFont

from .chunker import Chunk, _block_texts, _read_xhtml

# Pixel dimensions for the text-only fallback. Roughly letter @ 96 dpi
# so the resulting PNG is comparable in size to the PDF render.
_FALLBACK_WIDTH = 816
_FALLBACK_HEIGHT = 1056
_FALLBACK_MARGIN = 64
_FALLBACK_LINE_WIDTH = 80  # characters per line at the chosen font

# Public marker used by tests to detect which path was taken.
RENDER_PATH_PLAYWRIGHT = "playwright"
RENDER_PATH_FALLBACK = "fallback"


def render_pdf_page(pdf_path: str | Path, page_idx: int,
                    out_path: str | Path, dpi: int = 150) -> Path:
    """Render one PDF page to PNG at ``dpi``. Returns the output path."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        if page_idx < 0 or page_idx >= len(doc):
            raise IndexError(
                f"page_idx {page_idx} out of range for {pdf_path} "
                f"(has {len(doc)} pages)"
            )
        page = doc[page_idx]
        try:
            # pypdfium2 takes a scale factor (1.0 == 72 dpi).
            scale = dpi / 72.0
            bitmap = page.render(scale=scale)
            pil = bitmap.to_pil()
            pil.save(out, format="PNG")
        finally:
            page.close()
    finally:
        doc.close()
    return out


def _try_playwright_render(html: str, css: str, out_path: Path) -> bool:
    """Try to rasterize ``html`` with Playwright. Return True on success."""
    try:
        playwright_mod = importlib.import_module("playwright.sync_api")
    except ImportError:
        return False
    sync_playwright = getattr(playwright_mod, "sync_playwright", None)
    if sync_playwright is None:
        return False
    try:
        with sync_playwright() as p:  # pragma: no cover - exercised by users with Playwright
            browser = p.chromium.launch()
            try:
                page = browser.new_page(
                    viewport={"width": _FALLBACK_WIDTH,
                              "height": _FALLBACK_HEIGHT}
                )
                # Inline the CSS so the rendered HTML is self-contained.
                full = (
                    "<!doctype html><html><head><meta charset='utf-8'>"
                    f"<style>{css}</style></head><body>{html}</body></html>"
                )
                page.set_content(full, wait_until="load")
                page.screenshot(path=str(out_path), full_page=True)
            finally:
                browser.close()
        return True
    except Exception:  # pragma: no cover - browser missing/locked
        return False


def _fallback_render_text(blocks: list[str], out_path: Path) -> Path:
    """Render block texts as a low-fidelity PNG with PIL."""
    img = Image.new("RGB", (_FALLBACK_WIDTH, _FALLBACK_HEIGHT), "white")
    draw = ImageDraw.Draw(img)
    font: ImageFont.ImageFont
    try:
        font = ImageFont.truetype("DejaVuSansMono.ttf", 14)
    except OSError:
        font = ImageFont.load_default()

    y = _FALLBACK_MARGIN
    line_height = 18
    max_y = _FALLBACK_HEIGHT - _FALLBACK_MARGIN
    for block in blocks:
        for line in wrap(block, width=_FALLBACK_LINE_WIDTH) or [""]:
            if y > max_y:
                break
            draw.text((_FALLBACK_MARGIN, y), line, fill="black", font=font)
            y += line_height
        # Blank separator between paragraphs.
        y += line_height // 2
        if y > max_y:
            break

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG")
    return out_path


def _chunk_html(chunk: Chunk, epub_path: Path) -> tuple[str, list[str]]:
    """Return ``(html_snippet, block_texts)`` for the chunk's paragraph
    range. Returns empty strings when the chunk failed to align."""
    if not chunk.epub_file:
        return "", []
    try:
        src = _read_xhtml(epub_path, chunk.epub_file)
    except (KeyError, OSError):
        return "", []
    blocks = _block_texts(src)
    start, end = chunk.epub_para_range
    if start >= len(blocks):
        return "", []
    selected = blocks[start:end] if end > start else blocks[start:start + 1]
    # Build a minimal HTML snippet using <p> tags — good enough for
    # Playwright; the fallback path ignores HTML and reads ``selected``.
    safe = [
        re.sub(r"[<>&]", lambda m: {"<": "&lt;", ">": "&gt;",
                                    "&": "&amp;"}[m.group(0)], b)
        for b in selected
    ]
    html_body = "\n".join(f"<p>{b}</p>" for b in safe)
    return html_body, selected


_DEFAULT_CSS = (
    "body{font-family:serif;line-height:1.5;margin:48px;color:#222;}"
    "p{margin:0 0 1em 0;}"
)


def render_epub_chunk(epub_path: str | Path, chunk: Chunk,
                      out_path: str | Path,
                      *, prefer_playwright: bool = True) -> Path:
    """Render an EPUB chunk to PNG.

    Tries Playwright first (headless Chromium). Falls back to a text-only
    PIL render when Playwright is not installed *or* fails to launch its
    browser binary.

    Returns the output path. Always writes a file — even when alignment
    failed, in which case the image carries a single "missing" line so
    downstream tooling still has something to send to the LLM.
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    html_body, blocks = _chunk_html(chunk, Path(epub_path))
    if not blocks:
        # Emit an explicit placeholder so the LLM can flag misalignment
        # rather than getting confused by an empty image.
        placeholder = (
            f"[no matching EPUB section for PDF page {chunk.pdf_page}; "
            f"anchor='{chunk.anchor_text or '<none>'}']"
        )
        _fallback_render_text([placeholder], out)
        return out

    if prefer_playwright and _try_playwright_render(html_body, _DEFAULT_CSS, out):
        return out
    return _fallback_render_text(blocks, out)


def _verify_image_writable(path: Path) -> None:
    """Sanity check that we wrote a real PNG — used by tests."""
    with Image.open(path) as im:
        im.verify()


# Re-export for tests that want to know which path the renderer chose.
__all__ = [
    "RENDER_PATH_FALLBACK",
    "RENDER_PATH_PLAYWRIGHT",
    "render_epub_chunk",
    "render_pdf_page",
]
