"""Regression tests for the chunked split stage's image-reference handling.

Both bugs here dropped every diagram from the EPUB *silently* — the audit
count doesn't move when an `![](…)` fails to become an `<img>`, so only an
explicit test catches a regression.  See CLAUDE.md "dropped image references".
"""
from markdown_it import MarkdownIt

import re

import pdf2epub_pro.split as split
from pdf2epub_pro.split import (
    _IMG_PATH_RE,
    absorb_artifacts,
    safe_artifacts_dirname,
)


def _chunked_parser():
    # Mirror md2epub_chunked._render_to_body's MarkdownIt config exactly so
    # these tests assert against the same rendering the synthesizer uses.
    md = MarkdownIt("commonmark",
                    {"typographer": True, "html": False, "linkify": False})
    md.enable(["table", "strikethrough", "smartquotes", "replacements"])
    return md


def test_absorb_rewrites_image_ref_when_source_path_contains_spaces(tmp_path):
    # REGRESSION: _IMG_PATH_RE used `[^)\s]`, so a Docling image path with a
    # space ("C:\\Users\\First Last\\...\\image_000000_<hash>.png") never
    # matched.  absorb_artifacts still *moved* the PNG into the cache but left
    # the markdown ref pointing at the now-deleted temp path, so the diagram
    # vanished from the EPUB on any machine with a space in its temp/home path
    # (i.e. almost every Windows user).
    chunk_out = tmp_path / "out dir with space"
    art = chunk_out / "chunk_artifacts"
    art.mkdir(parents=True)
    png = art / "image_000000_deadbeef.png"
    png.write_bytes(b"\x89PNG\r\n")

    global_art = tmp_path / "Book_artifacts"  # space-free: isolate bug #1
    out = absorb_artifacts(f"![Image]({png})\n", chunk_out, "chunk",
                           global_art, 1)

    assert str(png) not in out, "old temp path was left dangling in the ref"
    assert "Book_artifacts/c0001_image_000000_deadbeef.png" in out
    assert (global_art / "c0001_image_000000_deadbeef.png").exists()


def test_img_path_re_matches_windows_path_with_spaces():
    line = (r"![Image](C:\Users\First Last\AppData\Local\Temp\docling_x"
            r"\out_00300\chunk_artifacts\image_000000_deadbeef.png)")
    assert _IMG_PATH_RE.search(line) is not None


def test_safe_artifacts_dirname_strips_spaces():
    # REGRESSION: the artifacts dir was named "<stem>_artifacts"; for a book
    # whose filename has spaces the rewritten ref "Distributed Systems 4th
    # Edition_artifacts/..." is not a valid CommonMark image destination.
    name = safe_artifacts_dirname("Distributed Systems 4th Edition")
    assert " " not in name
    assert name == "Distributed_Systems_4th_Edition_artifacts"


def test_safe_artifacts_dirname_non_ascii_falls_back():
    # All-non-ASCII stem collapses to a safe default rather than an empty /
    # non-ASCII ref that could break link resolution.
    assert safe_artifacts_dirname("分散式系統") == "doc_artifacts"


def test_sanitized_ref_renders_to_img_tag():
    # The artifact-level property that actually matters: a ref built from the
    # sanitized dir name survives to an <img> under the chunked synthesizer's
    # own parser.  A bare-spaces ref renders as plain text (no <img>).
    md = _chunked_parser()
    name = safe_artifacts_dirname("Distributed Systems 4th Edition")
    html = md.render(f"![Image]({name}/c0001_image_000000_deadbeef.png)")
    assert "<img" in html

    spaced = md.render(
        "![Image](Distributed Systems 4th Edition_artifacts/"
        "c0001_image_000000_deadbeef.png)")
    assert "<img" not in spaced  # documents the failure the fix prevents


def test_split_pdf_to_md_emits_space_free_refs_for_spaced_stem(tmp_path, monkeypatch):
    # REGRESSION (wiring): split_pdf_to_md must name the global artifacts dir —
    # and therefore every image ref — from safe_artifacts_dirname(stem), not
    # `stem + "_artifacts"`.  For a spaced filename the latter yields refs that
    # never render to <img>.  Stub Docling so we exercise the orchestration
    # (chunk loop + absorb) without a real PDF.  Reverting either fix line
    # (the `[^)\r\n]` regex OR the sanitized dir name) reintroduces a space
    # into the ref and fails this test.
    out_md = tmp_path / "Distributed Systems 4th Edition.md"

    def fake_split_pdf(src, chunk_size, work_dir):
        return [(work_dir / "chunk_0.pdf", 0, 1)], 1

    def fake_run_docling(chunk_pdf, out_dir, with_images,
                         enrich_formula=False):
        art = out_dir / "c_artifacts"
        art.mkdir()
        png = art / "image_000000_deadbeef.png"
        png.write_bytes(b"\x89PNG\r\n")
        md_path = out_dir / "c.md"
        # Docling emits an absolute path — under pytest's tmp it contains the
        # user's home dir, which routinely has a space (exercises bug #1 too).
        md_path.write_text(f"![Image]({png})\n", encoding="utf-8")
        return md_path

    monkeypatch.setattr(split, "split_pdf", fake_split_pdf)
    monkeypatch.setattr(split, "run_docling", fake_run_docling)

    split.split_pdf_to_md(tmp_path / "in.pdf", out_md)

    text = out_md.read_text(encoding="utf-8")
    assert "image_000000_deadbeef.png" in text, "image ref was dropped entirely"
    ref_line = next(l for l in text.splitlines() if "image_000000" in l)
    dest = re.search(r"\(([^)]*image_000000_deadbeef\.png)\)", ref_line)
    assert dest and " " not in dest.group(1), f"ref still has spaces: {ref_line!r}"


def test_docling_cmd_formula_toggle(monkeypatch, tmp_path):
    # REGRESSION: --math must flip docling's formula enrichment; without
    # it the 103 formulas of a math textbook stay "formula-not-decoded".
    monkeypatch.setattr(split, "docling_path", lambda: "docling")
    off = split._docling_cmd(tmp_path / "c.pdf", tmp_path, True,
                             enrich_formula=False)
    on = split._docling_cmd(tmp_path / "c.pdf", tmp_path, True,
                            enrich_formula=True)
    assert "--no-enrich-formula" in off and "--enrich-formula" not in off
    assert "--enrich-formula" in on and "--no-enrich-formula" not in on
