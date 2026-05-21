"""Per-detector tests for EPUB-level defect detectors.

We build minimal EPUBs on the fly with stdlib ``zipfile``.  Each EPUB has the
mandatory ``mimetype`` + ``META-INF/container.xml`` plus one or two xhtml
files crafted to trigger (or not) the detector under test.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from pdf2epub_pro.audit.detectors_epub import (
    BrokenInternalAnchorDetector,
    DuplicateIdDetector,
    EmptySpineItemDetector,
    HeadingDepthJumpDetector,
    InvalidIdDetector,
    RelativeHrefSkeletonDetector,
)


_MIMETYPE = "application/epub+zip"
_CONTAINER_XML = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""


def _xhtml(body: str, title: str = "Doc") -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
        f'<title>{title}</title></head><body>{body}</body></html>'
    )


def _build_epub(tmp_path: Path, members: dict[str, str], name: str = "book.epub") -> Path:
    out = tmp_path / name
    # ``mimetype`` must be the first entry and stored uncompressed; the
    # detector code doesn't actually require that (it only reads xhtml), but
    # we follow the spec so anything else that opens these fakes is happy.
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(zipfile.ZipInfo("mimetype"), _MIMETYPE, zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", _CONTAINER_XML)
        for member_path, content in members.items():
            zf.writestr(member_path, content)
    return out


# -- 15. Invalid IDs --------------------------------------------------------
def test_invalid_id_positive_starts_with_digit(tmp_path: Path):
    epub = _build_epub(tmp_path, {
        "OEBPS/a.xhtml": _xhtml('<h1 id="3pillars">Pillars</h1><p>x</p>'),
    })
    findings = list(InvalidIdDetector().run(epub))
    assert len(findings) == 1
    assert findings[0].severity == "error"
    assert "3pillars" in findings[0].message


def test_invalid_id_positive_contains_space(tmp_path: Path):
    epub = _build_epub(tmp_path, {
        "OEBPS/a.xhtml": _xhtml('<h1 id="my heading">Heading</h1>'),
    })
    findings = list(InvalidIdDetector().run(epub))
    assert any("my heading" in f.message for f in findings)


def test_invalid_id_negative(tmp_path: Path):
    epub = _build_epub(tmp_path, {
        "OEBPS/a.xhtml": _xhtml('<h1 id="real-pillars">Pillars</h1>'),
    })
    assert list(InvalidIdDetector().run(epub)) == []


# -- 16. Duplicate ID ------------------------------------------------------
def test_duplicate_id_within_file(tmp_path: Path):
    epub = _build_epub(tmp_path, {
        "OEBPS/a.xhtml": _xhtml('<h1 id="dup">A</h1><h2 id="dup">B</h2>'),
    })
    findings = list(DuplicateIdDetector().run(epub))
    assert len(findings) == 1
    assert findings[0].severity == "error"


def test_duplicate_id_across_files(tmp_path: Path):
    epub = _build_epub(tmp_path, {
        "OEBPS/a.xhtml": _xhtml('<h1 id="shared">A</h1>'),
        "OEBPS/b.xhtml": _xhtml('<h2 id="shared">B</h2>'),
    })
    findings = list(DuplicateIdDetector().run(epub))
    assert len(findings) == 1
    assert "shared" in findings[0].message


def test_duplicate_id_negative(tmp_path: Path):
    epub = _build_epub(tmp_path, {
        "OEBPS/a.xhtml": _xhtml('<h1 id="one">A</h1>'),
        "OEBPS/b.xhtml": _xhtml('<h2 id="two">B</h2>'),
    })
    assert list(DuplicateIdDetector().run(epub)) == []


# -- 17. Broken internal anchor --------------------------------------------
def test_broken_internal_anchor_positive(tmp_path: Path):
    # b.xhtml has id="real", but a.xhtml links to #ghost.
    epub = _build_epub(tmp_path, {
        "OEBPS/a.xhtml": _xhtml('<p><a href="b.xhtml#ghost">link</a></p>'),
        "OEBPS/b.xhtml": _xhtml('<h1 id="real">Real</h1>'),
    })
    findings = list(BrokenInternalAnchorDetector().run(epub))
    assert any("ghost" in f.message for f in findings)


def test_broken_internal_anchor_negative_resolves(tmp_path: Path):
    epub = _build_epub(tmp_path, {
        "OEBPS/a.xhtml": _xhtml('<p><a href="b.xhtml#real">link</a></p>'),
        "OEBPS/b.xhtml": _xhtml('<h1 id="real">Real</h1>'),
    })
    assert list(BrokenInternalAnchorDetector().run(epub)) == []


def test_broken_internal_anchor_ignores_external_url(tmp_path: Path):
    epub = _build_epub(tmp_path, {
        "OEBPS/a.xhtml": _xhtml(
            '<p><a href="https://example.com/page#anywhere">link</a></p>'),
    })
    assert list(BrokenInternalAnchorDetector().run(epub)) == []


# -- 18. Relative href skeleton --------------------------------------------
def test_relative_href_skeleton_positive(tmp_path: Path):
    epub = _build_epub(tmp_path, {
        "OEBPS/a.xhtml": _xhtml('<p><a href="./other.html">x</a></p>'),
    })
    findings = list(RelativeHrefSkeletonDetector().run(epub))
    assert len(findings) == 1


def test_relative_href_skeleton_negative_xhtml(tmp_path: Path):
    epub = _build_epub(tmp_path, {
        "OEBPS/a.xhtml": _xhtml('<p><a href="other.xhtml">x</a></p>'),
    })
    assert list(RelativeHrefSkeletonDetector().run(epub)) == []


def test_relative_href_skeleton_negative_absolute(tmp_path: Path):
    epub = _build_epub(tmp_path, {
        "OEBPS/a.xhtml": _xhtml('<p><a href="https://docs.aws.amazon.com/page.html">x</a></p>'),
    })
    assert list(RelativeHrefSkeletonDetector().run(epub)) == []


# -- 19. Heading depth jump ------------------------------------------------
def test_heading_depth_jump_positive(tmp_path: Path):
    epub = _build_epub(tmp_path, {
        "OEBPS/a.xhtml": _xhtml('<h1>One</h1><h4>Four</h4>'),
    })
    findings = list(HeadingDepthJumpDetector().run(epub))
    assert len(findings) == 1
    assert findings[0].extra["from"] == 1
    assert findings[0].extra["to"] == 4


def test_heading_depth_jump_negative(tmp_path: Path):
    epub = _build_epub(tmp_path, {
        "OEBPS/a.xhtml": _xhtml('<h1>One</h1><h2>Two</h2><h3>Three</h3>'),
    })
    assert list(HeadingDepthJumpDetector().run(epub)) == []


# -- 20. Empty spine item --------------------------------------------------
def test_empty_spine_item_positive(tmp_path: Path):
    epub = _build_epub(tmp_path, {
        "OEBPS/chapter1.xhtml": _xhtml(""),
    })
    findings = list(EmptySpineItemDetector().run(epub))
    assert len(findings) == 1


def test_empty_spine_item_negative_has_text(tmp_path: Path):
    epub = _build_epub(tmp_path, {
        "OEBPS/chapter1.xhtml": _xhtml("<p>Hello world.</p>"),
    })
    assert list(EmptySpineItemDetector().run(epub)) == []


def test_empty_spine_item_skips_known_meta_files(tmp_path: Path):
    # cover.xhtml / nav.xhtml are expected to be light on body text — skip.
    epub = _build_epub(tmp_path, {
        "OEBPS/cover.xhtml": _xhtml(""),
        "OEBPS/nav.xhtml": _xhtml(""),
    })
    assert list(EmptySpineItemDetector().run(epub)) == []
