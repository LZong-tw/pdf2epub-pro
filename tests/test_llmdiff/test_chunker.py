"""Unit tests for pdf2epub_pro.llmdiff.chunker."""
from __future__ import annotations

from pathlib import Path

import pytest

from pdf2epub_pro.llmdiff.chunker import (
    Chunk,
    _evenly_spaced_indices,
    _pick_anchor,
    sample_chunks,
)


def test_evenly_spaced_indices_picks_endpoints_and_middles():
    out = _evenly_spaced_indices(total=10, n=5)
    assert out[0] == 0
    assert out[-1] == 9
    assert len(out) == 5
    # interior picks should be sorted, distinct, and inside (0, total-1).
    interior = out[1:-1]
    assert interior == sorted(interior)
    assert all(0 < i < 9 for i in interior)
    assert len(set(out)) == len(out)


def test_evenly_spaced_indices_handles_small_books():
    assert _evenly_spaced_indices(total=2, n=5) == [0, 1]
    assert _evenly_spaced_indices(total=1, n=5) == [0]
    assert _evenly_spaced_indices(total=0, n=5) == []


def test_pick_anchor_prefers_long_lines():
    page = "Section header\n\nThe quick brown fox jumps over the lazy dog every single morning."
    anchor = _pick_anchor(page)
    # 8–12 words from the longest line.
    assert anchor
    assert 8 <= len(anchor.split()) <= 12
    assert "quick" in anchor.lower()


def test_pick_anchor_empty_when_no_content():
    assert _pick_anchor("") == ""
    assert _pick_anchor("1\n2\n3\n") == ""  # all boilerplate-like lines


def test_sample_chunks_aligns_pdf_pages_to_epub_files(tiny_pair: tuple[Path, Path]):
    pdf, epub = tiny_pair
    chunks = sample_chunks(pdf, epub, n=3)
    assert len(chunks) == 3
    assert all(isinstance(c, Chunk) for c in chunks)
    # first chunk = first page of PDF, aligned to the first xhtml file.
    assert chunks[0].pdf_page == 0
    assert chunks[0].epub_file.endswith("chapter1.xhtml")
    # last chunk = last page, aligned to the second xhtml file.
    assert chunks[-1].pdf_page == 5
    assert chunks[-1].epub_file.endswith("chapter2.xhtml")
    # every chunk should have a non-empty anchor and a valid range.
    for c in chunks:
        assert c.anchor_text, f"missing anchor for chunk {c}"
        start, end = c.epub_para_range
        assert 0 <= start <= end


def test_sample_chunks_n_equals_total_returns_every_page(
    tiny_pair: tuple[Path, Path]
):
    pdf, epub = tiny_pair
    chunks = sample_chunks(pdf, epub, n=10)  # more than the 6 pages we have
    # Should return one chunk per page (clamped at total pages).
    pages = [c.pdf_page for c in chunks]
    assert pages == sorted(pages)
    assert pages[0] == 0
    assert pages[-1] == 5
    assert len(chunks) == 6


def test_sample_chunks_unaligned_when_epub_missing_text(
    tiny_pdf: Path, tmp_path: Path
):
    # Build an EPUB that has *none* of the PDF's phrases. The chunker
    # should still return chunks; their epub_file should be empty.
    from .conftest import make_tiny_epub
    bad = make_tiny_epub(
        tmp_path / "wrong.epub",
        [("foo.xhtml", ["completely unrelated marketing copy here only forever"])],
    )
    chunks = sample_chunks(tiny_pdf, bad, n=2)
    assert len(chunks) == 2
    assert all(c.epub_file == "" for c in chunks)
    assert all(c.epub_para_range == (0, 0) for c in chunks)
