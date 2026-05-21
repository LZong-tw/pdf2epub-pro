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
    ExternalHrefDensityDetector,
    HeadingDepthJumpDetector,
    HeadingTextDuplicationDetector,
    ImageAltEmptyDetector,
    InvalidIdDetector,
    OpfManifestSpineConsistencyDetector,
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
    # H1 → H4 skips 2 levels (H2, H3) → info severity, still surfaced.
    epub = _build_epub(tmp_path, {
        "OEBPS/a.xhtml": _xhtml('<h1>One</h1><h4>Four</h4>'),
    })
    findings = list(HeadingDepthJumpDetector().run(epub))
    assert len(findings) == 1
    assert findings[0].extra["from"] == 1
    assert findings[0].extra["to"] == 4
    assert findings[0].severity == "info"


def test_heading_depth_jump_negative(tmp_path: Path):
    epub = _build_epub(tmp_path, {
        "OEBPS/a.xhtml": _xhtml('<h1>One</h1><h2>Two</h2><h3>Three</h3>'),
    })
    assert list(HeadingDepthJumpDetector().run(epub)) == []


def test_heading_depth_jump_allows_single_skip(tmp_path: Path):
    # REGRESSION: AWS-style structure intentionally goes H1 pillar → H3 BP
    # because tidy demotes H2 to bullets. Single-level skips must NOT fire,
    # or 1358 false positives drown out real defects in WAF.
    epub = _build_epub(tmp_path, {
        "OEBPS/a.xhtml": _xhtml('<h1>Pillar</h1><h3>BP heading</h3>'),
    })
    assert list(HeadingDepthJumpDetector().run(epub)) == []


def test_heading_depth_jump_reports_skipped_count(tmp_path: Path):
    # H2 → H6 skips 3 levels → warn (genuine structural defect).
    epub = _build_epub(tmp_path, {
        "OEBPS/a.xhtml": _xhtml('<h2>Two</h2><h6>Six</h6>'),
    })
    findings = list(HeadingDepthJumpDetector().run(epub))
    assert len(findings) == 1
    assert findings[0].extra["skipped"] == 3
    assert "skips 3 levels" in findings[0].message
    assert findings[0].severity == "warn"


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


# -- 21. Image alt empty ---------------------------------------------------
def test_image_alt_missing_positive(tmp_path: Path):
    epub = _build_epub(tmp_path, {
        "OEBPS/a.xhtml": _xhtml('<p><img src="x.png"/></p>'),
    })
    findings = list(ImageAltEmptyDetector().run(epub))
    assert len(findings) == 1
    assert findings[0].severity == "warn"
    assert "no alt" in findings[0].message
    assert findings[0].extra["alt_missing"] is True


def test_image_alt_empty_string_positive(tmp_path: Path):
    epub = _build_epub(tmp_path, {
        "OEBPS/a.xhtml": _xhtml('<p><img src="x.png" alt=""/></p>'),
    })
    findings = list(ImageAltEmptyDetector().run(epub))
    assert len(findings) == 1
    assert "empty alt" in findings[0].message
    assert findings[0].extra["alt_missing"] is False


def test_image_alt_present_negative(tmp_path: Path):
    epub = _build_epub(tmp_path, {
        "OEBPS/a.xhtml": _xhtml(
            '<p><img src="x.png" alt="A diagram of the pipeline"/></p>'
        ),
    })
    assert list(ImageAltEmptyDetector().run(epub)) == []


def test_image_alt_skips_decorative_role(tmp_path: Path):
    epub = _build_epub(tmp_path, {
        "OEBPS/a.xhtml": _xhtml(
            '<p><img src="bullet.png" role="presentation"/></p>'
        ),
    })
    assert list(ImageAltEmptyDetector().run(epub)) == []


def test_image_alt_skips_icon_src(tmp_path: Path):
    epub = _build_epub(tmp_path, {
        "OEBPS/a.xhtml": _xhtml('<p><img src="icons/check.png"/></p>'),
    })
    assert list(ImageAltEmptyDetector().run(epub)) == []


# -- 22. Heading text duplication ------------------------------------------
def test_heading_text_duplication_positive(tmp_path: Path):
    epub = _build_epub(tmp_path, {
        "OEBPS/a.xhtml": _xhtml('<h2>Foo widget configuration</h2><p>x</p>'),
        "OEBPS/b.xhtml": _xhtml('<h2>Foo widget configuration</h2><p>y</p>'),
    })
    findings = list(HeadingTextDuplicationDetector().run(epub))
    assert len(findings) == 1
    assert findings[0].severity == "info"
    assert "Foo widget configuration" in findings[0].message
    assert set(findings[0].extra["files"]) == {"OEBPS/a.xhtml", "OEBPS/b.xhtml"}
    assert findings[0].extra["level"] == 2


def test_heading_text_duplication_allowlist(tmp_path: Path):
    # "Overview" is allowlisted — must not fire even across 3 files.
    epub = _build_epub(tmp_path, {
        "OEBPS/a.xhtml": _xhtml('<h1>Overview</h1>'),
        "OEBPS/b.xhtml": _xhtml('<h1>Overview</h1>'),
        "OEBPS/c.xhtml": _xhtml('<h1>Overview</h1>'),
    })
    assert list(HeadingTextDuplicationDetector().run(epub)) == []


def test_heading_text_duplication_single_file_negative(tmp_path: Path):
    epub = _build_epub(tmp_path, {
        "OEBPS/a.xhtml": _xhtml('<h2>Unique heading</h2>'),
        "OEBPS/b.xhtml": _xhtml('<h2>Something different</h2>'),
    })
    assert list(HeadingTextDuplicationDetector().run(epub)) == []


# -- 23. External href density ---------------------------------------------
def _many_links(count: int, base: str = "https://example.com/") -> str:
    return "".join(f'<p><a href="{base}{i}">link{i}</a></p>' for i in range(count))


def test_external_href_density_positive(tmp_path: Path):
    epub = _build_epub(tmp_path, {
        "OEBPS/refs.xhtml": _xhtml(_many_links(60)),
    })
    findings = list(ExternalHrefDensityDetector().run(epub))
    assert len(findings) == 1
    assert findings[0].severity == "warn"
    assert findings[0].extra["count"] == 60


def test_external_href_density_negative(tmp_path: Path):
    epub = _build_epub(tmp_path, {
        "OEBPS/chapter.xhtml": _xhtml(_many_links(10)),
    })
    assert list(ExternalHrefDensityDetector().run(epub)) == []


def test_external_href_density_ignores_internal(tmp_path: Path):
    # 60 internal anchors — must NOT trigger an external-density warning.
    body = "".join(f'<p><a href="other.xhtml#sec{i}">x</a></p>' for i in range(60))
    epub = _build_epub(tmp_path, {
        "OEBPS/refs.xhtml": _xhtml(body),
    })
    assert list(ExternalHrefDensityDetector().run(epub)) == []


# -- 24. OPF manifest / spine consistency ----------------------------------
def _build_opf_epub(
    tmp_path: Path,
    manifest_items: list[tuple[str, str, str | None]],
    spine_idrefs: list[str],
    files: dict[str, str],
    opf_dir: str = "OEBPS",
    name: str = "book.epub",
) -> Path:
    """Build an EPUB with a hand-crafted OPF in ``opf_dir/content.opf``.

    ``manifest_items``: list of ``(id, href, properties_or_None)``.  href
    is relative to ``opf_dir``.  ``spine_idrefs`` populates ``<spine>``.
    ``files`` adds raw zip members verbatim.
    """
    out = tmp_path / name
    opf_path = f"{opf_dir}/content.opf" if opf_dir else "content.opf"
    container = (
        '<?xml version="1.0"?>\n'
        '<container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        f'  <rootfiles><rootfile full-path="{opf_path}" '
        'media-type="application/oebps-package+xml"/></rootfiles>\n'
        '</container>\n'
    )

    def _item_xml(item_id, href, props):
        attrs = f'id="{item_id}" href="{href}" media-type="application/xhtml+xml"'
        if props:
            attrs += f' properties="{props}"'
        return f"    <item {attrs}/>"

    manifest_xml = "\n".join(_item_xml(i, h, p) for i, h, p in manifest_items)
    spine_xml = "\n".join(f'    <itemref idref="{i}"/>' for i in spine_idrefs)
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        'unique-identifier="bookid">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        '    <dc:identifier id="bookid">x</dc:identifier>\n'
        '    <dc:title>T</dc:title>\n'
        '    <dc:language>en</dc:language>\n'
        '  </metadata>\n'
        '  <manifest>\n'
        f'{manifest_xml}\n'
        '  </manifest>\n'
        '  <spine>\n'
        f'{spine_xml}\n'
        '  </spine>\n'
        '</package>\n'
    )

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(zipfile.ZipInfo("mimetype"), _MIMETYPE, zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", container)
        zf.writestr(opf_path, opf)
        for member_path, content in files.items():
            zf.writestr(member_path, content)
    return out


def test_opf_consistency_broken_spine_idref(tmp_path: Path):
    # spine references "ghost" which is not in manifest.
    epub = _build_opf_epub(
        tmp_path,
        manifest_items=[("c1", "c1.xhtml", None)],
        spine_idrefs=["c1", "ghost"],
        files={"OEBPS/c1.xhtml": _xhtml("<p>hi</p>")},
    )
    findings = list(OpfManifestSpineConsistencyDetector().run(epub))
    kinds = [f.extra.get("kind") for f in findings]
    assert "broken_spine" in kinds
    assert any("ghost" in f.message for f in findings if f.extra.get("kind") == "broken_spine")


def test_opf_consistency_missing_manifest_target(tmp_path: Path):
    # manifest lists missing.xhtml but it's not in the zip.
    epub = _build_opf_epub(
        tmp_path,
        manifest_items=[
            ("c1", "c1.xhtml", None),
            ("c2", "missing.xhtml", None),
        ],
        spine_idrefs=["c1", "c2"],
        files={"OEBPS/c1.xhtml": _xhtml("<p>hi</p>")},
    )
    findings = list(OpfManifestSpineConsistencyDetector().run(epub))
    kinds = [f.extra.get("kind") for f in findings]
    assert "missing_manifest_target" in kinds
    assert any(
        "missing.xhtml" in f.message
        for f in findings if f.extra.get("kind") == "missing_manifest_target"
    )


def test_opf_consistency_orphan_zip_file(tmp_path: Path):
    # extras.xhtml is in the zip but not in the manifest.
    epub = _build_opf_epub(
        tmp_path,
        manifest_items=[("c1", "c1.xhtml", None)],
        spine_idrefs=["c1"],
        files={
            "OEBPS/c1.xhtml": _xhtml("<p>hi</p>"),
            "OEBPS/extras.xhtml": _xhtml("<p>orphan</p>"),
        },
    )
    findings = list(OpfManifestSpineConsistencyDetector().run(epub))
    kinds = [f.extra.get("kind") for f in findings]
    assert "orphan_file" in kinds
    assert any("extras.xhtml" in f.message for f in findings if f.extra.get("kind") == "orphan_file")


def test_opf_consistency_clean_epub_negative(tmp_path: Path):
    # All three invariants satisfied — should report nothing.
    epub = _build_opf_epub(
        tmp_path,
        manifest_items=[
            ("nav", "nav.xhtml", "nav"),
            ("c1", "c1.xhtml", None),
        ],
        spine_idrefs=["c1"],
        files={
            "OEBPS/nav.xhtml": _xhtml("<nav><ol><li><a href='c1.xhtml'>1</a></li></ol></nav>"),
            "OEBPS/c1.xhtml": _xhtml("<p>hi</p>"),
        },
    )
    assert list(OpfManifestSpineConsistencyDetector().run(epub)) == []


def test_opf_consistency_nav_property_exempts_orphan(tmp_path: Path):
    # nav.xhtml is in manifest with properties="nav" — it's NOT a spine
    # item (no itemref) but should still NOT be flagged as orphan.
    epub = _build_opf_epub(
        tmp_path,
        manifest_items=[
            ("nav", "nav.xhtml", "nav"),
            ("c1", "c1.xhtml", None),
        ],
        spine_idrefs=["c1"],
        files={
            "OEBPS/nav.xhtml": _xhtml("<nav/>"),
            "OEBPS/c1.xhtml": _xhtml("<p>hi</p>"),
        },
    )
    findings = list(OpfManifestSpineConsistencyDetector().run(epub))
    # Should not flag nav.xhtml as orphan (it's in the manifest, so this
    # is already covered) — but specifically guard against any flag.
    assert findings == []
