"""Unit tests for pdf2epub_pro.llmdiff.renderer.

The Playwright path is exercised only when ``playwright.sync_api`` and a
working browser binary are installed — both are skipped by default in CI.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from PIL import Image

from pdf2epub_pro.llmdiff.chunker import Chunk, sample_chunks
from pdf2epub_pro.llmdiff.renderer import render_epub_chunk, render_pdf_page


def _assert_valid_png(p: Path) -> Image.Image:
    assert p.exists() and p.stat().st_size > 0
    img = Image.open(p)
    img.load()
    return img


def test_render_pdf_page_writes_png(tiny_pdf: Path, tmp_path: Path):
    out = render_pdf_page(tiny_pdf, page_idx=0, out_path=tmp_path / "p0.png",
                          dpi=72)
    img = _assert_valid_png(out)
    # 612 x 792 at 72 dpi = 612 x 792
    assert img.size == (612, 792)


def test_render_pdf_page_higher_dpi_scales_image(
    tiny_pdf: Path, tmp_path: Path
):
    out = render_pdf_page(tiny_pdf, page_idx=1, out_path=tmp_path / "p1.png",
                          dpi=144)
    img = _assert_valid_png(out)
    # 144 dpi → image roughly 2x in each dimension.
    assert img.size == (1224, 1584)


def test_render_pdf_page_out_of_range_raises(tiny_pdf: Path, tmp_path: Path):
    with pytest.raises(IndexError):
        render_pdf_page(tiny_pdf, page_idx=999, out_path=tmp_path / "x.png")


def test_render_epub_chunk_fallback_path(
    tiny_pair: tuple[Path, Path], tmp_path: Path, monkeypatch
):
    """Force the text-only PIL fallback by pretending Playwright is gone."""
    # Pretend playwright.sync_api is not importable. We patch the
    # internal helper to skip the launch attempt entirely so the test is
    # deterministic even on machines where Playwright IS installed.
    from pdf2epub_pro.llmdiff import renderer
    monkeypatch.setattr(renderer, "_try_playwright_render",
                        lambda *a, **kw: False)

    pdf, epub = tiny_pair
    chunks = sample_chunks(pdf, epub, n=2)
    out = render_epub_chunk(epub, chunks[0], tmp_path / "e0.png")
    img = _assert_valid_png(out)
    assert img.size == (816, 1056)


def test_render_epub_chunk_handles_unaligned_chunk(
    tiny_epub: Path, tmp_path: Path, monkeypatch
):
    """Chunk with empty epub_file → still emits a placeholder PNG."""
    from pdf2epub_pro.llmdiff import renderer
    monkeypatch.setattr(renderer, "_try_playwright_render",
                        lambda *a, **kw: False)

    chunk = Chunk(pdf_page=3, epub_file="", epub_para_range=(0, 0),
                  anchor_text="")
    out = render_epub_chunk(tiny_epub, chunk, tmp_path / "missing.png")
    _assert_valid_png(out)


@pytest.mark.skipif(
    importlib.util.find_spec("playwright") is None,
    reason="Playwright not installed; skipping headless render test.",
)
def test_render_epub_chunk_playwright_optional(
    tiny_pair: tuple[Path, Path], tmp_path: Path
):  # pragma: no cover - only runs when Playwright is installed
    """If Playwright IS installed, the renderer should still produce a PNG.

    Even if Playwright's browser binary is missing it should silently
    fall through to the PIL fallback — this test only asserts that the
    output file is a readable PNG, not which renderer produced it."""
    pdf, epub = tiny_pair
    chunks = sample_chunks(pdf, epub, n=2)
    out = render_epub_chunk(epub, chunks[0], tmp_path / "pw.png")
    _assert_valid_png(out)
