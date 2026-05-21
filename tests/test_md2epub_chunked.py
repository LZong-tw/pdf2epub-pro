"""Unit + end-to-end tests for the chunked Calibre synthesizer."""
from __future__ import annotations

import os
import shutil
import zipfile
from pathlib import Path

import pytest

from pdf2epub_pro.md2epub_chunked import (
    MAX_CHUNK_BYTES,
    _extract_heading_ids,
    _first_heading_text,
    _make_md_parser,
    _render_to_body,
    chunk_markdown,
    md2epub_chunked,
)


# ---- pure-string units ------------------------------------------------------

def test_chunk_markdown_splits_at_h1_only_when_no_oversize():
    md = "\n".join([
        "# One",
        "Body one.",
        "## Sub",
        "Sub body.",
        "# Two",
        "Body two.",
        "# Three",
        "Body three.",
        "",
    ])
    chunks = chunk_markdown(md)
    assert len(chunks) == 3
    assert chunks[0].startswith("# One")
    assert chunks[1].startswith("# Two")
    assert chunks[2].startswith("# Three")
    assert "## Sub" in chunks[0]


def test_chunk_markdown_subsplits_oversized_h1_at_h2():
    # Build a 600 KB single-H1 chunk with many H2 sections.
    h2_section = "## sec\n" + ("filler " * 200) + "\n"
    big = "# Huge\nintro\n" + (h2_section * 600)
    assert len(big.encode("utf-8")) > MAX_CHUNK_BYTES
    chunks = chunk_markdown(big, max_bytes=200 * 1024)
    assert len(chunks) > 1
    # First slice keeps the H1 (preamble of the huge chapter).
    assert chunks[0].startswith("# Huge") or chunks[0].startswith("## sec")
    # All but the first slice should start at an H2 boundary.
    for c in chunks[1:]:
        assert c.lstrip().startswith("## sec")


def test_chunk_markdown_preserves_preamble_without_h1():
    md = "preamble paragraph.\n\nmore preamble.\n"
    chunks = chunk_markdown(md)
    assert chunks == [md]


def test_extract_heading_ids_strips_trailing_attr_lists():
    md = "\n".join([
        "# Top",
        "body",
        "## With Id {#ref-0001}",
        "body two",
        "## Plain heading",
        "body three",
        "### Deep {#deep.ref}",
        "x",
    ])
    cleaned, ids = _extract_heading_ids(md)
    assert "{#" not in cleaned
    assert ids == [None, "ref-0001", None, "deep.ref"]
    # Plain heading text is untouched.
    assert "## Plain heading" in cleaned


def test_first_heading_text_returns_plain_text():
    chunk = "## My Title {#x-1}\nbody"
    assert _first_heading_text(chunk) == "My Title"


def test_render_attaches_ids_to_headings_in_order():
    md_parser = _make_md_parser()
    cleaned, ids = _extract_heading_ids(
        "## One {#a}\ntext\n\n## Two\ntext\n\n### Three {#c}\nx"
    )
    body = _render_to_body(md_parser, cleaned, ids)
    # First H2 gets id=a, second H2 has no id, then H3 gets id=c.
    assert 'id="a"' in body
    assert 'id="c"' in body
    # No double-injection.
    assert body.count('id="a"') == 1


def test_render_emits_smart_quotes_via_typographer():
    md_parser = _make_md_parser()
    body = _render_to_body(md_parser, 'It\'s "important" -- yes.', [])
    # Typographer should curl quotes and produce an en-dash.
    assert "‘" in body or "’" in body or "“" in body
    assert "–" in body or "—" in body


def test_render_tables_to_html():
    md_parser = _make_md_parser()
    body = _render_to_body(
        md_parser, "| a | b |\n| - | - |\n| 1 | 2 |\n", []
    )
    assert "<table" in body
    assert "<th" in body and "</th>" in body
    assert "<td" in body and "</td>" in body


# ---- integration: produce an actual EPUB ------------------------------------

def _calibre_available() -> bool:
    if os.environ.get("PDF2EPUB_EBOOK_CONVERT"):
        return True
    if shutil.which("ebook-convert"):
        return True
    for base in (
        Path(r"C:\Program Files\Calibre2\ebook-convert.exe"),
        Path(r"C:\Program Files (x86)\Calibre2\ebook-convert.exe"),
        Path("/opt/calibre/ebook-convert"),
        Path("/usr/bin/ebook-convert"),
    ):
        if base.exists():
            return True
    return False


needs_calibre = pytest.mark.skipif(
    not _calibre_available(),
    reason="Calibre's ebook-convert not on PATH; integration test requires it",
)


@needs_calibre
def test_full_pipeline_produces_valid_epub(tmp_path: Path):
    md = tmp_path / "fixture.md"
    md.write_text(
        "\n".join([
            "# Introduction",
            "",
            "Hello world. This is the intro chapter.",
            "",
            "## Sub of intro",
            "",
            "Sub paragraph.",
            "",
            "# Chapter Two",
            "",
            "Body text for chapter two.",
            "",
            "## Section A {#sec-a}",
            "",
            "Paragraph under section A.",
            "",
            "## Section B",
            "",
            "Paragraph under section B.",
            "",
            "# Appendix",
            "",
            "## Ref One {#ref-0001}",
            "",
            "Source body for ref one.",
            "",
            "## Ref Two {#ref-0002}",
            "",
            "Source body for ref two.",
            "",
        ]),
        encoding="utf-8",
    )
    out = tmp_path / "out.epub"
    md2epub_chunked(
        md, out,
        title="Test Book",
        authors="Tester",
        language="en",
    )
    assert out.exists()
    assert out.stat().st_size > 1024  # not an empty file

    with zipfile.ZipFile(out) as zf:
        # mimetype must be the first file in an EPUB.
        names = zf.namelist()
        assert names[0] == "mimetype"
        mimetype = zf.read("mimetype").decode("ascii").strip()
        assert mimetype == "application/epub+zip"
        # container.xml at the canonical location.
        assert "META-INF/container.xml" in names
        # OPF parses as XML.
        opf_name = next(n for n in names if n.endswith(".opf"))
        opf_xml = zf.read(opf_name)
        from xml.etree import ElementTree as ET
        ET.fromstring(opf_xml)
        # At least three content xhtml chunks should be present (one per H1).
        xhtml_chunks = [n for n in names if n.endswith(".xhtml") or n.endswith(".html")]
        assert len(xhtml_chunks) >= 3
        # Nav / TOC presence: EPUB 3 ships a nav.xhtml or a toc.ncx.
        assert any("nav" in n.lower() for n in names) or any(
            n.endswith(".ncx") for n in names
        )


@needs_calibre
def test_ids_round_trip_into_epub(tmp_path: Path):
    md = tmp_path / "fixture.md"
    md.write_text(
        "\n".join([
            "# Top",
            "",
            "Intro.",
            "",
            "## Marked One {#ref-0001}",
            "",
            "First body.",
            "",
            "## Marked Two {#ref-0002}",
            "",
            "Second body.",
            "",
        ]),
        encoding="utf-8",
    )
    out = tmp_path / "out.epub"
    md2epub_chunked(md, out, title="ID test", language="en")
    with zipfile.ZipFile(out) as zf:
        all_xhtml = "\n".join(
            zf.read(n).decode("utf-8", errors="replace")
            for n in zf.namelist()
            if n.endswith(".xhtml") or n.endswith(".html")
        )
    assert 'id="ref-0001"' in all_xhtml
    assert 'id="ref-0002"' in all_xhtml
