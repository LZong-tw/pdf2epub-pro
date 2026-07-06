"""Regression tests for the formula image-crop fallback primitives.

These cover the geometry and the pandoc oracle in isolation; the md-rewrite
orchestration that consumes them is tested separately once wired in.
"""

import pypdfium2 as pdfium
from PIL import Image

from pdf2epub_pro.formula_fallback import (
    apply_formula_image_fallback,
    bbox_pixel_box,
    crop_formula,
    crop_region,
    harvest_formula_bboxes,
    pandoc_can_mathml,
    render_pdf_page,
)


def _box(page=1, orig="x"):
    return {
        "page_no": page,
        "bbox": {"l": 10, "t": 80, "r": 50, "b": 40},
        "coord_origin": "BOTTOMLEFT",
        "page_w": 200,
        "page_h": 100,
        "orig": orig,
    }


def _doc(formulas):
    """Build a minimal docling-shaped document.  ``formulas`` is a list of
    (page_no, orig) — each becomes a formula text item; a leading paragraph
    (non-formula) is added so the harvester must skip it."""
    texts = [{"label": "text", "prov": [{"page_no": 1, "bbox": {}}]}]
    children = [{"$ref": "#/texts/0"}]
    for i, (page, orig) in enumerate(formulas, start=1):
        texts.append(
            {
                "label": "formula",
                "orig": orig,
                "text": orig,  # non-empty -> docling would emit $$orig$$
                "prov": [
                    {
                        "page_no": page,
                        "bbox": {
                            "l": 10,
                            "t": 80,
                            "r": 50,
                            "b": 60,
                            "coord_origin": "BOTTOMLEFT",
                        },
                    }
                ],
            }
        )
        children.append({"$ref": f"#/texts/{i}"})
    return {
        "body": {"children": children},
        "texts": texts,
        "pages": {
            str(p): {"size": {"width": 100, "height": 200}}
            for p in {pg for pg, _ in formulas}
        },
    }


# ------------------------------------------------------------- pandoc oracle
def test_pandoc_can_mathml_accepts_plain_formula():
    assert pandoc_can_mathml(r"x^2 + y^2 = z^2") is True


def test_pandoc_can_mathml_rejects_unbalanced_braces():
    # REGRESSION: docling emitted `...\frac{TN+FP+FN}{...} }` with a stray
    # closing brace; pandoc dumps it as raw TeX instead of MathML.  That is
    # exactly the block the fallback must catch, so the oracle must say False.
    assert pandoc_can_mathml(r"\frac{TN + FP + FN}{TP + TN + FP + FN} }") is False


# ---------------------------------------------------------- bbox pixel geometry
def test_bbox_pixel_box_bottomleft():
    # BOTTOMLEFT: y grows up, so `t` (larger) is the TOP edge.
    bb = {"l": 10, "t": 180, "r": 50, "b": 160}
    box = bbox_pixel_box(bb, 100, 200, 100, 200, origin="BOTTOMLEFT", pad_x=0, pad_y=0)
    assert box == (10, 20, 50, 40)


def test_bbox_pixel_box_topleft():
    # TOPLEFT: y grows down; `t` is the smaller (top) edge already.
    bb = {"l": 10, "t": 20, "r": 50, "b": 40}
    box = bbox_pixel_box(bb, 100, 200, 100, 200, origin="TOPLEFT", pad_x=0, pad_y=0)
    assert box == (10, 20, 50, 40)


def test_bbox_pixel_box_scales_to_raster():
    # Fractions are page-space independent: a 300-dpi raster is 4.1666x the
    # 72-dpi page, and the box scales with it.
    bb = {"l": 0, "t": 100, "r": 50, "b": 0}
    box = bbox_pixel_box(bb, 100, 100, 400, 400, origin="BOTTOMLEFT", pad_x=0, pad_y=0)
    assert box == (0, 0, 200, 400)


def test_bbox_pixel_box_padding_clamps_to_image():
    # A box hugging the edges cannot pad past the raster bounds.
    bb = {"l": 0, "t": 100, "r": 100, "b": 0}
    box = bbox_pixel_box(
        bb, 100, 100, 100, 100, origin="BOTTOMLEFT", pad_x=0.5, pad_y=0.5
    )
    assert box == (0, 0, 100, 100)


# ------------------------------------------------------- render + crop plumbing
def _mini_pdf(tmp_path, w=200, h=100):
    doc = pdfium.PdfDocument.new()
    doc.new_page(w, h)
    p = tmp_path / "mini.pdf"
    doc.save(str(p))
    doc.close()
    return p


def test_render_pdf_page_dimensions(tmp_path):
    pdf = _mini_pdf(tmp_path, 200, 100)
    img = render_pdf_page(pdf, 1, dpi=72)  # scale 1.0 -> 1pt == 1px
    assert img.size == (200, 100)


def test_crop_region_returns_box_sized_image(tmp_path):
    pdf = _mini_pdf(tmp_path, 200, 100)
    img = render_pdf_page(pdf, 1, dpi=72)
    out = crop_region(img, (20, 20, 120, 60))
    assert out.size == (100, 40)


def test_crop_formula_writes_expected_png(tmp_path):
    pdf = _mini_pdf(tmp_path, 200, 100)
    bb = {"l": 20, "t": 80, "r": 120, "b": 40}  # BOTTOMLEFT
    out = crop_formula(
        pdf,
        1,
        bb,
        tmp_path / "media" / "f001.png",
        page_w=200,
        page_h=100,
        dpi=72,
        pad_x=0,
        pad_y=0,
    )
    assert out.exists()
    with Image.open(out) as im:
        assert im.size == (100, 40)  # matches bbox_pixel_box((20,20,120,60))


# --------------------------------------------------- docling bbox harvesting
def test_harvest_formula_bboxes_skips_prose_and_applies_offset():
    doc = _doc([(2, "E = mc^2")])
    out = harvest_formula_bboxes(doc, page_offset=20)
    assert len(out) == 1  # the leading paragraph item is skipped
    r = out[0]
    assert r["page_no"] == 22  # local page 2 shifted by the chunk's start
    assert r["bbox"] == {"l": 10, "t": 80, "r": 50, "b": 60}
    assert r["coord_origin"] == "BOTTOMLEFT"
    assert (r["page_w"], r["page_h"]) == (100, 200)
    assert r["orig"] == "E = mc^2"


def test_harvest_formula_bboxes_preserves_reading_order():
    # REGRESSION: record i must line up with the i-th $$...$$ in docling's md,
    # else the crop of formula A lands on formula B.  Order must be preserved.
    doc = _doc([(1, "A"), (1, "B"), (3, "C")])
    out = harvest_formula_bboxes(doc)
    assert [r["orig"] for r in out] == ["A", "B", "C"]
    assert [r["page_no"] for r in out] == [1, 1, 3]


def test_harvest_formula_bboxes_walks_nested_groups():
    # A formula reached only through a group's children must still be found.
    doc = {
        "body": {"children": [{"$ref": "#/groups/0"}]},
        "groups": [{"children": [{"$ref": "#/texts/0"}]}],
        "texts": [
            {
                "label": "formula",
                "orig": "nested",
                "text": "nested",
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {
                            "l": 1,
                            "t": 9,
                            "r": 5,
                            "b": 6,
                            "coord_origin": "BOTTOMLEFT",
                        },
                    }
                ],
            }
        ],
        "pages": {"1": {"size": {"width": 10, "height": 10}}},
    }
    out = harvest_formula_bboxes(doc)
    assert len(out) == 1 and out[0]["orig"] == "nested"


# ------------------------------------------------ md rewrite (image fallback)
def test_apply_formula_image_fallback_replaces_only_unrenderable(tmp_path):
    # A renderable formula stays as math; an un-renderable one becomes an
    # image crop whose alt text preserves the TeX and whose index picks the
    # matching bbox (formula_0001 -> boxes[1]).
    pdf = _mini_pdf(tmp_path, 200, 100)
    boxes = [_box(orig="good"), _box(orig="bad")]
    md = "text\n\n$$GOOD_A$$\n\n$$BAD_B$$\n"
    art = tmp_path / "art"
    out, n = apply_formula_image_fallback(
        md,
        pdf,
        boxes,
        art,
        media_ref_prefix="art",
        dpi=72,
        can_render=lambda b: "GOOD" in b,
    )
    assert n == 1
    assert "$$GOOD_A$$" in out
    assert "$$BAD_B$$" not in out
    assert "![BAD_B](art/formula_0001.png)" in out
    assert (art / "formula_0001.png").exists()


def test_apply_formula_image_fallback_is_fence_aware(tmp_path):
    # REGRESSION: a `$$` inside a code fence must not be cropped AND must not
    # consume a formula index — otherwise the real formula would grab the
    # wrong bbox (boxes[1], which doesn't exist) and be left un-cropped.
    pdf = _mini_pdf(tmp_path, 200, 100)
    fence = chr(96) * 3
    md = f"{fence}\n$$NOT_MATH$$\n{fence}\n\n$$REAL$$\n"
    out, n = apply_formula_image_fallback(
        md,
        pdf,
        [_box(orig="real")],
        tmp_path / "art",
        media_ref_prefix="art",
        dpi=72,
        can_render=lambda b: False,
    )
    assert "$$NOT_MATH$$" in out  # fenced $$ untouched
    assert "![REAL](art/formula_0000.png)" in out  # real one -> boxes[0]
    assert n == 1


def test_apply_formula_image_fallback_leaves_formula_without_bbox(tmp_path):
    # More failing formulas than harvested bboxes: the surplus is left as math
    # rather than mis-cropped from a wrong page.
    pdf = _mini_pdf(tmp_path, 200, 100)
    md = "$$A$$\n\n$$B$$\n"
    out, n = apply_formula_image_fallback(
        md,
        pdf,
        [_box(orig="a")],
        tmp_path / "art",
        media_ref_prefix="art",
        dpi=72,
        can_render=lambda b: False,
    )
    assert "![A](art/formula_0000.png)" in out
    assert "$$B$$" in out
    assert n == 1


def test_harvest_skips_undecoded_empty_text_formula():
    # REGRESSION: docling emits $$ only for a formula with non-empty decoded
    # text; an undecodable one (empty text) emits a formula-not-decoded comment
    # and NO $$, yet keeps its bbox in the JSON.  Harvesting it would shift the
    # positional $$ -> box mapping onto the wrong regions, so it must be
    # skipped — keeping the harvested list 1:1 with the markdown's $$ blocks.
    doc = _doc([(1, "good")])
    doc["texts"].append(
        {
            "label": "formula",
            "orig": "raw",
            "text": "",  # empty -> docling emits no $$ for this item
            "prov": [
                {
                    "page_no": 1,
                    "bbox": {
                        "l": 1,
                        "t": 9,
                        "r": 5,
                        "b": 6,
                        "coord_origin": "BOTTOMLEFT",
                    },
                }
            ],
        }
    )
    doc["body"]["children"].append({"$ref": f"#/texts/{len(doc['texts']) - 1}"})
    out = harvest_formula_bboxes(doc)
    assert [r["orig"] for r in out] == ["good"]


def test_apply_formula_image_fallback_skips_inline_code_span(tmp_path):
    # REGRESSION: a `$$...$$` inside an INLINE code span (a `$$VAR$$` template
    # placeholder, a shell PID) must not be cropped and must not consume a
    # formula index — else the next real formula grabs the wrong bbox.  tidy is
    # inline-code-aware; the fallback must be too.
    pdf = _mini_pdf(tmp_path, 200, 100)
    tick = chr(96)
    md = f"Use {tick}$$VAR$${tick} then\n\n$$REAL$$\n"
    out, n = apply_formula_image_fallback(
        md,
        pdf,
        [_box(orig="real")],
        tmp_path / "art",
        media_ref_prefix="art",
        dpi=72,
        can_render=lambda b: False,
    )
    assert f"{tick}$$VAR$${tick}" in out  # code span left verbatim
    assert "![REAL](art/formula_0000.png)" in out  # real one -> boxes[0]
    assert n == 1


def test_bbox_pixel_box_offpage_is_empty_not_inverted():
    # REGRESSION: an off-page bbox once produced an INVERTED (right<left) box
    # from an asymmetric clamp, which made PIL.crop().save() raise.  It must
    # collapse to a zero-area, non-inverted box instead.
    bb = {"l": 260, "t": 80, "r": 320, "b": 40}  # entirely right of the page
    left, top, right, bottom = bbox_pixel_box(
        bb, 200, 100, 200, 100, origin="BOTTOMLEFT", pad_x=0, pad_y=0
    )
    assert right >= left and bottom >= top  # never inverted
    assert right - left == 0 or bottom - top == 0  # off-page -> zero area


def test_apply_formula_image_fallback_leaves_degenerate_bbox(tmp_path):
    # REGRESSION: a degenerate/off-page bbox must leave the formula as text,
    # not hand PIL an empty box (which raised and aborted the whole --math run).
    pdf = _mini_pdf(tmp_path, 200, 100)
    bad = _box(orig="x")
    bad["bbox"] = {"l": 260, "t": 80, "r": 320, "b": 40}  # off the right edge
    out, n = apply_formula_image_fallback(
        "$$X$$\n",
        pdf,
        [bad],
        tmp_path / "art",
        media_ref_prefix="art",
        dpi=72,
        can_render=lambda b: False,
    )
    assert "$$X$$" in out  # left as math, never cropped
    assert n == 0


def test_apply_formula_image_fallback_alt_is_markdown_safe(tmp_path):
    # REGRESSION: a body ending in a backslash escaped the closing ] of
    # ![alt](url) (image lost, media path leaked as text); `*` was parsed as
    # emphasis.  The alt must be markdown-inert.
    pdf = _mini_pdf(tmp_path, 200, 100)
    md = "$$a * b \\$$\n"  # body 'a * b \\' -> trailing backslash + asterisk
    out, n = apply_formula_image_fallback(
        md,
        pdf,
        [_box(orig="x")],
        tmp_path / "art",
        media_ref_prefix="art",
        dpi=72,
        can_render=lambda b: False,
    )
    assert n == 1
    assert "](art/formula_0000.png)" in out  # ref intact, ] not escaped away
    ref = out.strip()
    assert ref.startswith("![") and "\\" not in ref and "*" not in ref


def test_apply_formula_image_fallback_memoizes_oracle(tmp_path):
    # The pandoc oracle is subprocess-backed; it must run once per DISTINCT
    # body, not once per occurrence.
    pdf = _mini_pdf(tmp_path, 200, 100)
    calls = []

    def spy(body):
        calls.append(body)
        return True  # all renderable -> nothing cropped

    md = "$$SAME$$\n\n$$SAME$$\n\n$$OTHER$$\n"
    apply_formula_image_fallback(
        md,
        pdf,
        [],
        tmp_path / "art",
        media_ref_prefix="art",
        dpi=72,
        can_render=spy,
    )
    assert calls == ["SAME", "OTHER"]  # SAME probed once, not twice


def test_apply_formula_image_fallback_renders_each_page_once(tmp_path, monkeypatch):
    # REGRESSION: the single-slot page cache must render a page at most once
    # for consecutive same-page crops (and never retain more than one raster).
    import pdf2epub_pro.formula_fallback as ff

    pdf = _mini_pdf(tmp_path, 200, 100)
    calls = []
    real = ff.render_pdf_page

    def counting(doc, page_no, *, dpi=300):
        calls.append(page_no)
        return real(doc, page_no, dpi=dpi)

    monkeypatch.setattr(ff, "render_pdf_page", counting)
    boxes = [_box(page=1, orig="a"), _box(page=1, orig="b")]  # same page
    _, n = ff.apply_formula_image_fallback(
        "$$A$$\n\n$$B$$\n",
        pdf,
        boxes,
        tmp_path / "art",
        media_ref_prefix="art",
        dpi=72,
        can_render=lambda b: False,
    )
    assert n == 2
    assert calls == [1]  # page 1 rendered exactly once for both crops
