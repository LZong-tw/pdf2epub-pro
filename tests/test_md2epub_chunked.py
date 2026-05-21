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

def test_chunk_markdown_ignores_headings_inside_fenced_code():
    # REGRESSION: AWS docs include example playbook templates inside
    # ```fenced``` blocks, where literal `# Heading` lines are EXAMPLE
    # content, not real markdown structure.  A fence-blind splitter
    # treats them as real H1s and miscarves the chunk, cascading into
    # markdown-it interpreting the rest of the fence as code and
    # silently dropping any images underneath.
    md = "\n".join([
        "# Real H1 A",
        "Body A.",
        "",
        "Some intro.",
        "",
        "```",
        "# Fake H1 inside fence",
        "## Fake H2 inside fence",
        "",
        "Example content with ![embedded](img.png).",
        "```",
        "",
        "More body of Real H1 A.",
        "",
        "![real image](real-img.png)",
        "",
        "# Real H1 B",
        "Body B.",
        "",
    ])
    chunks = chunk_markdown(md)
    # Exactly two chunks: one per REAL H1.  The fenced fake `#` lines
    # must not introduce extra chunks.
    assert len(chunks) == 2
    assert chunks[0].startswith("# Real H1 A")
    assert chunks[1].startswith("# Real H1 B")
    # The image in chunk A (outside the fence) and the fence body itself
    # both survive in chunk A.
    assert "![real image](real-img.png)" in chunks[0]
    assert "Fake H1 inside fence" in chunks[0]


def test_extract_heading_ids_skips_fenced_pound_lines():
    md = "\n".join([
        "# A {#first}",
        "",
        "```",
        "# Not a heading",
        "## Also not",
        "```",
        "",
        "## B {#second}",
        "",
    ])
    cleaned, ids = _extract_heading_ids(md)
    # Only the real headings produce id entries.
    assert ids == ["first", "second"]
    # The fenced "headings" stay verbatim.
    assert "# Not a heading" in cleaned
    assert "## Also not" in cleaned


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
def test_body_images_referenced_in_markdown_land_in_epub(tmp_path: Path):
    """REGRESSION: the chunked synthesizer used to render XHTML with
    `<img src="artifacts/foo.png">` but never copy the actual PNG file
    into the workdir nor add it to the OPF manifest.  Calibre's input
    plugin then silently dropped every body image — the WAF benchmark
    shipped with only the explicit `--cover` asset where the baseline
    had 58.

    Build a minimal fixture with two image references (one PNG, one
    JPG) plus their actual files on disk, run the pipeline, and assert
    BOTH images live inside the final EPUB.
    """
    from PIL import Image
    artifacts = tmp_path / "fixture_artifacts"
    artifacts.mkdir()
    png_src = artifacts / "alpha.png"
    jpg_src = artifacts / "bravo.jpg"
    Image.new("RGB", (8, 8), color=(255, 0, 0)).save(png_src, "PNG")
    Image.new("RGB", (8, 8), color=(0, 255, 0)).save(jpg_src, "JPEG")

    md = tmp_path / "with-images.md"
    md.write_text(
        "\n".join([
            "# Chapter One",
            "",
            "First image:",
            "",
            "![Alpha](fixture_artifacts/alpha.png)",
            "",
            "# Chapter Two",
            "",
            "Second image:",
            "",
            "![Bravo](fixture_artifacts/bravo.jpg)",
            "",
        ]),
        encoding="utf-8",
    )

    out = tmp_path / "with-images.epub"
    md2epub_chunked(md, out, title="Image Test", authors="Tester", language="en")
    assert out.exists()

    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        image_members = [n for n in names
                         if n.lower().endswith((".png", ".jpg", ".jpeg"))]
        # Both body images must appear in the EPUB (Calibre may rename
        # the path during conversion, so match on the basename).
        bases = {Path(n).name.lower() for n in image_members}
        assert "alpha.png" in bases, f"alpha.png missing; got {bases!r}"
        assert "bravo.jpg" in bases, f"bravo.jpg missing; got {bases!r}"
        # OPF should list them in the manifest with image media-types.
        opf_name = next(n for n in names if n.endswith(".opf"))
        opf_text = zf.read(opf_name).decode("utf-8", errors="replace")
        assert "image/png" in opf_text
        assert "image/jpeg" in opf_text


@needs_calibre
def test_body_image_inside_fenced_block_is_not_silently_dropped(tmp_path: Path):
    """REGRESSION: the chunker split at every line starting with `# `
    regardless of fence state.  When AWS docs put an example playbook
    template inside a ```fenced``` block — and that template starts
    with a literal `# Playbook Title …` — the chunker treated that as
    a real H1, miscarved the chunk, and markdown-it then wrapped the
    rest of the fence (and any image references it contained) inside
    `<pre><code>`.  The image walker correctly skipped them (they
    weren't `<img>` tags) and the asset never made it into the EPUB.

    Build that exact shape and assert the body image AFTER the fenced
    template still lands in the EPUB.
    """
    from PIL import Image
    artifacts = tmp_path / "fixture_artifacts"
    artifacts.mkdir()
    img_src = artifacts / "post-fence.png"
    Image.new("RGB", (8, 8), color=(0, 0, 255)).save(img_src, "PNG")

    md = tmp_path / "fenced.md"
    md.write_text(
        "\n".join([
            "# Real Chapter",
            "",
            "Some intro.",
            "",
            "```",
            "# Playbook Title ## Playbook Info | A | B | C",
            "## Steps 1. step one",
            "```",
            "",
            "Body resumes here:",
            "",
            "![PostFence](fixture_artifacts/post-fence.png)",
            "",
            "# Next Real Chapter",
            "",
            "Done.",
            "",
        ]),
        encoding="utf-8",
    )

    out = tmp_path / "fenced.epub"
    md2epub_chunked(md, out, title="Fenced Test", authors="Tester", language="en")
    assert out.exists()

    with zipfile.ZipFile(out) as zf:
        bases = {Path(n).name.lower() for n in zf.namelist()
                 if n.lower().endswith((".png", ".jpg", ".jpeg"))}
        assert "post-fence.png" in bases, (
            f"image after a fenced fake-H1 was dropped; got {bases!r}"
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
