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


def test_md2epub_pandoc_ascii_identifiers_strips_unicode_slug(tmp_path):
    """REGRESSION: pandoc default `markdown` reader produces slug IDs
    that preserve Unicode characters (legitimate 'é' or mojibake 'â'),
    which our audit (mirroring Calibre's stricter behavior) flags as
    invalid_id.  The +ascii_identifiers extension is what keeps the
    slugs ASCII-only without breaking internal href consistency.

    Without that flag this test would surface non-ASCII bytes inside
    `<section id="...">` for the Sulamérica heading.
    """
    md_in = tmp_path / "unicode.md"
    md_in.write_text(
        "# Sulamérica Seguros\n\nBody.\n\n## Routeâ 53 Test\n\nMore.\n",
        encoding="utf-8",
    )
    epub_out = tmp_path / "unicode.epub"
    mod.md2epub_pandoc(md_in, epub_out, title="Unicode Test")

    with zipfile.ZipFile(epub_out) as zf:
        # Walk every xhtml file's id="..." attributes
        offenders = []
        for n in zf.namelist():
            if not n.lower().endswith((".xhtml", ".html")):
                continue
            text = zf.read(n).decode("utf-8", errors="replace")
            import re
            for m in re.finditer(r'id="([^"]+)"', text):
                ident = m.group(1)
                if any(ord(c) > 127 for c in ident):
                    offenders.append((n, ident))

    assert offenders == [], (
        "non-ASCII characters present in EPUB id attributes despite "
        f"+ascii_identifiers: {offenders!r}"
    )


def test_dedupe_epub_ids_renames_cross_file_duplicates_and_updates_hrefs(tmp_path):
    """REGRESSION: pandoc's auto_identifiers disambiguation runs at AST
    level but does NOT cover the bodymatter preamble chunk pandoc
    synthesises from --metadata title.  That can leave two
    `<section id="X">` elements in two XHTML files — a real EPUB-spec
    violation.

    `_dedupe_epub_ids` walks the EPUB post-write, picks a collision-
    aware suffix (must not collide with pandoc's own `-1`, `-2`, …
    forms), and updates any href pointing at the renamed (file, id)
    pair.  Build a minimal EPUB exhibiting all three conditions, run
    the function, and assert: only ONE file keeps the original id, the
    other gets a fresh-suffix rename, and the href that targeted the
    file-with-renamed-id is rewritten.
    """
    epub = tmp_path / "dupe.epub"
    # Two xhtml files share id="dup".  Pandoc has already used the
    # `-1` suffix for an unrelated heading in a.xhtml, so the dedupe
    # function must pick `-2` (not `-1`) for the rename.
    a_xhtml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>A</title></head>'
        '<body>'
        '<section id="dup"><h1>First Dup</h1></section>'
        '<section id="dup-1"><h2>Pandoc-Suffixed</h2></section>'
        '<p><a href="b.xhtml#dup">link into b</a></p>'
        '</body></html>'
    )
    b_xhtml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>B</title></head>'
        '<body>'
        '<section id="dup"><h1>Second Dup</h1></section>'
        '</body></html>'
    )
    container = (
        '<?xml version="1.0"?>\n'
        '<container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles>'
        '</container>'
    )
    with zipfile.ZipFile(epub, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(zipfile.ZipInfo("mimetype"),
                    "application/epub+zip", zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("content.opf",
                    '<?xml version="1.0"?><package/>')  # minimal stub
        zf.writestr("a.xhtml", a_xhtml)
        zf.writestr("b.xhtml", b_xhtml)

    renamed = mod._dedupe_epub_ids(epub)
    assert renamed == 1, "expected one cross-file dup rename"

    with zipfile.ZipFile(epub) as zf:
        a_out = zf.read("a.xhtml").decode("utf-8")
        b_out = zf.read("b.xhtml").decode("utf-8")

    # First occurrence (a.xhtml) keeps the original id.
    assert 'id="dup"' in a_out
    # Second occurrence (b.xhtml) is renamed.  Must NOT clobber the
    # pre-existing `-1` form already living in a.xhtml.
    assert 'id="dup"' not in b_out, "b.xhtml still has the original dup id"
    assert 'id="dup-2"' in b_out, (
        "b.xhtml's section id should be renamed to dup-2 to avoid "
        "colliding with the pre-existing dup-1 in a.xhtml"
    )
    # The cross-file href that pointed at b.xhtml#dup must now point
    # at b.xhtml#dup-2 so internal navigation survives.
    assert 'href="b.xhtml#dup-2"' in a_out, (
        "href targeting the renamed (file, id) pair was not updated"
    )
    # The same-file dup-1 (pandoc's own suffix) is untouched.
    assert 'id="dup-1"' in a_out


def test_dedupe_epub_ids_no_op_when_no_duplicates(tmp_path):
    epub = tmp_path / "clean.epub"
    container = (
        '<?xml version="1.0"?>\n'
        '<container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles>'
        '</container>'
    )
    a = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>A</title></head>'
        '<body><section id="alpha"><h1>A</h1></section></body></html>'
    )
    b = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>B</title></head>'
        '<body><section id="bravo"><h1>B</h1></section></body></html>'
    )
    with zipfile.ZipFile(epub, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(zipfile.ZipInfo("mimetype"),
                    "application/epub+zip", zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("content.opf", '<?xml version="1.0"?><package/>')
        zf.writestr("a.xhtml", a)
        zf.writestr("b.xhtml", b)

    assert mod._dedupe_epub_ids(epub) == 0


def test_reader_exts_math_toggle():
    assert "+tex_math_dollars" in mod._reader_exts(math=True)
    assert "-tex_math_dollars" in mod._reader_exts(math=False)
    # never enable the backslash/raw-tex variants -- docling emits $-math
    assert "-raw_tex" in mod._reader_exts(math=True)


def test_math_true_renders_mathml(tmp_path):
    # REGRESSION: docling formula enrichment emits `$$...$$`, but the
    # reader had tex_math_dollars OFF, so the LaTeX shipped as literal
    # text.  With math=True it must become MathML.
    md = tmp_path / "m.md"
    md.write_text(
        "# Ch\n\nThe estimator is\n\n$$n = -\\frac{m}{k}\\ln(1-x)$$\n",
        encoding="utf-8",
    )
    epub = tmp_path / "m.epub"
    mod.md2epub_pandoc(md, epub, title="M", math=True)
    blob = "".join(
        zipfile.ZipFile(epub).read(n).decode("utf-8", "replace")
        for n in zipfile.ZipFile(epub).namelist() if n.endswith(".xhtml")
    )
    assert "<math" in blob
    assert "$$" not in blob


def test_math_false_leaves_dollar_prose_untouched(tmp_path):
    # The default must NOT turn literal prose dollars into math -- a book
    # that says "for $500" is not writing an equation.
    md = tmp_path / "p.md"
    md.write_text("# Ch\n\nAlice paid $500 and Bob paid $600 total.\n",
                  encoding="utf-8")
    epub = tmp_path / "p.epub"
    mod.md2epub_pandoc(md, epub, title="P", math=False)
    blob = "".join(
        zipfile.ZipFile(epub).read(n).decode("utf-8", "replace")
        for n in zipfile.ZipFile(epub).namelist() if n.endswith(".xhtml")
    )
    assert "<math" not in blob
    assert "$500" in blob and "$600" in blob
