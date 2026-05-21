"""Smoke tests for the pandoc-based EPUB synthesizer.

Skips entirely if pandoc isn't installed — the surrounding pdf2epub-pro
test suite stays portable across environments that only have Calibre.
"""
import shutil
import zipfile
from pathlib import Path

import pytest

from pdf2epub_pro import md2epub_pandoc as mod


def _pandoc_available() -> bool:
    try:
        return bool(mod.pandoc_path())
    except FileNotFoundError:
        return False


pytestmark = pytest.mark.skipif(
    not _pandoc_available(),
    reason="pandoc not on PATH and not in known install locations",
)


# Fixture markdown deliberately exercises every extension the reader
# string in md2epub_pandoc enables: pipe tables, fenced code with attrs,
# header attributes, link attributes, definition list, smart quotes,
# and an abbr definition.
_FIXTURE_MD = """\
# Top-Level Chapter

This is the intro -- with smart "quotes" and an em--dash.

## Section A {#sec-a}

Pipe table:

| Col 1 | Col 2 |
|-------|-------|
| a     | b     |
| c     | d     |

### Sub A.1

A [link with attribute](https://example.com){.external}.

```python {#example1}
def hello():
    return "world"
```

## Definitions

Term 1
:   The definition.

Term 2
:   Another definition.

*[HTML]: Hypertext Markup Language

# Second Chapter

Body of chapter two.

## Section B

More content.
"""


def _make_cover(path: Path) -> None:
    """Generate a tiny placeholder JPEG for the --epub-cover-image flag.

    Pillow is already a hard dependency of the package (see pyproject),
    so importing it here doesn't add a new test prerequisite.
    """
    from PIL import Image
    Image.new("RGB", (16, 16), color=(200, 200, 200)).save(path, "JPEG")


def test_md2epub_pandoc_produces_valid_epub(tmp_path):
    md_in = tmp_path / "fixture.md"
    md_in.write_text(_FIXTURE_MD, encoding="utf-8")
    cover = tmp_path / "cover.jpg"
    _make_cover(cover)
    epub_out = tmp_path / "out.epub"

    result = mod.md2epub_pandoc(
        md_in,
        epub_out,
        title="Smoke Title",
        authors="Alice, Bob",
        language="en",
        publisher="Smoke Press",
        tags="testing, ebooks",
        cover=cover,
        book_producer="pdf2epub-pro-test",
    )

    assert result == epub_out
    assert epub_out.exists(), "pandoc did not write an output file"
    assert epub_out.stat().st_size > 0, "output EPUB is empty"

    # EPUBs are ZIP archives.  Validate structure: container.xml,
    # OPF package file, nav (toc) document, and at least one XHTML
    # body file.  Also confirm cover image is bundled.
    with zipfile.ZipFile(epub_out) as zf:
        names = zf.namelist()
        assert "mimetype" in names
        assert any(n.endswith("container.xml") for n in names), \
            "no container.xml in EPUB"
        opf_candidates = [n for n in names if n.endswith(".opf")]
        assert opf_candidates, "no .opf package file in EPUB"

        opf_text = zf.read(opf_candidates[0]).decode("utf-8", errors="replace")
        assert "Smoke Title" in opf_text
        assert "Alice" in opf_text and "Bob" in opf_text
        assert "Smoke Press" in opf_text
        # Book producer should appear as a contributor with role bkp.
        assert "pdf2epub-pro-test" in opf_text
        assert "bkp" in opf_text, "book-producer role tag missing from OPF"
        # Cover image referenced in OPF.
        assert "cover" in opf_text.lower(), "cover image not referenced in OPF"

        # Nav document presence — pandoc emits it as nav.xhtml in EPUB3.
        nav_candidates = [n for n in names if "nav" in n.lower() and n.endswith(".xhtml")]
        assert nav_candidates, f"no nav doc found among {names!r}"
        nav_text = zf.read(nav_candidates[0]).decode("utf-8", errors="replace")
        # Both H1s should appear in the nav.
        assert "Top-Level Chapter" in nav_text
        assert "Second Chapter" in nav_text
        # H2/H3 should also appear (toc-depth=3).
        assert "Section A" in nav_text
        assert "Sub A.1" in nav_text


def test_md2epub_pandoc_minimal_metadata(tmp_path):
    """Even with no optional metadata, function should produce a valid EPUB."""
    md_in = tmp_path / "tiny.md"
    md_in.write_text("# Hello\n\nA paragraph.\n", encoding="utf-8")
    epub_out = tmp_path / "tiny.epub"

    mod.md2epub_pandoc(md_in, epub_out)
    assert epub_out.exists()

    with zipfile.ZipFile(epub_out) as zf:
        names = zf.namelist()
        assert "mimetype" in names
        assert any(n.endswith(".opf") for n in names)
