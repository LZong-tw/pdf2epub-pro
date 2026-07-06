"""Image-crop fallback for formulas pandoc cannot render as MathML.

Docling's formula OCR sometimes emits LaTeX that pandoc's math reader
rejects — mismatched environments, stray braces, or plain mis-classified
prose/code (a PHP ``$_SERVER[...]`` or an LDAP filter read as a formula).
Such a block would otherwise ship as raw TeX text.

Instead we crop the *original typeset region* straight out of the source
PDF and embed it as a high-DPI image: the author's own rendering, which
bypasses OCR entirely and is therefore correct even when the transcription
was not.  The crop geometry comes from docling's layout bboxes; this module
holds the geometry + rendering primitives and the pandoc-can-render oracle.
The md-rewrite orchestration that ties them together lives in the pipeline.
"""

import re
import subprocess
from pathlib import Path

import pypdfium2 as pdfium

_FENCE_RE = re.compile(r"^\s*(```|~~~)")
# Docling emits every display formula on a single line as $$...$$.  On a prose
# line we must also skip INLINE code spans, which may legitimately carry `$$`
# (a shell PID, a `$$VAR$$` placeholder): a segment is either an inline code
# span (group 1, left verbatim) or a display-math block (group 2), mirroring
# tidy.escape_prose_dollars so neither gets cropped nor consumes a formula
# index.
_DISPLAY_MATH_RE = re.compile(r"\$\$(.+?)\$\$")
_SEGMENT_RE = re.compile(r"(`+[^`]*`+)|(\$\$.+?\$\$)")


def _safe_alt(body):
    """A markdown-inert alt string for an OCR formula body.

    Collapse whitespace and drop characters that would either break the
    ``![alt](url)`` syntax (a trailing ``\\`` escapes the closing ``]``; a
    literal ``]`` closes the alt early) or be parsed by pandoc as markdown
    (``*`` emphasis, `` ` `` code).  Brackets are shown as parens; everything
    else (letters, digits, math operators, ``_``) is kept so the alt stays
    readable.  Never empty.
    """
    alt = re.sub(r"\s+", " ", body).strip()
    alt = alt.replace("\\", " ").replace("`", " ").replace("*", " ")
    alt = alt.replace("[", "(").replace("]", ")")
    return re.sub(r"\s+", " ", alt).strip() or "formula"


# -- docling JSON -> ordered formula bboxes ---------------------------------
def _resolve_ref(child, doc):
    # A child link is {"$ref": "#/texts/5"} -> doc["texts"][5].
    _, kind, idx = child["$ref"].split("/")
    return doc[kind][int(idx)]


def _walk_formulas(node, doc, out):
    # Depth-first over body.children, mirroring the order docling's markdown
    # serializer emits items — so the Nth formula here is the Nth $$...$$.
    for child in node.get("children", []):
        if "$ref" not in child:
            continue
        item = _resolve_ref(child, doc)
        if item.get("label") == "formula":
            out.append(item)
        _walk_formulas(item, doc, out)


def harvest_formula_bboxes(doc, page_offset=0):
    """Formula bboxes from a docling ``DoclingDocument`` dict, in reading order.

    The order matches docling's markdown output, so record ``i`` lines up with
    the ``i``-th ``$$...$$`` in the emitted markdown.  ``page_offset`` is the
    chunk's 0-based start page in the ORIGINAL pdf; docling numbers pages 1..N
    within the chunk, so the returned 1-based ``page_no`` is shifted to index
    the whole document.  Each record carries the bbox, its ``coord_origin``,
    the page's point size (for normalization), and docling's ``orig`` OCR text
    (used only to sanity-check alignment).
    """
    items = []
    _walk_formulas(doc.get("body", {}), doc, items)
    pages = doc.get("pages", {})
    out = []
    for it in items:
        # Docling serializes a formula as $$text$$ ONLY when its decoded text
        # is non-empty; an undecodable formula emits a "formula-not-decoded"
        # comment and NO $$, yet still carries a bbox in the JSON.  Skipping
        # empty-text items keeps this list 1:1 with the $$ blocks in the md, so
        # positional alignment in apply_formula_image_fallback never drifts.
        if not (it.get("text") or "").strip():
            continue
        prov = (it.get("prov") or [{}])[0]
        bbox = prov.get("bbox")
        if not bbox:
            continue
        local_page = prov.get("page_no", 1)
        size = pages.get(str(local_page), {}).get("size", {})
        out.append(
            {
                "page_no": local_page + page_offset,
                "bbox": {k: bbox[k] for k in ("l", "t", "r", "b")},
                "coord_origin": bbox.get("coord_origin", "BOTTOMLEFT"),
                "page_w": size.get("width"),
                "page_h": size.get("height"),
                "orig": it.get("orig", ""),
            }
        )
    return out


# -- detector: the oracle for "would this ship as raw TeX?" -----------------
def pandoc_can_mathml(body: str, *, timeout: float = 30.0) -> bool:
    """True iff pandoc turns ``$$body$$`` into a MathML ``<math>`` node.

    This is exactly the set we must NOT replace: anything that renders is
    left as math.  A body pandoc chokes on (returns without ``<math>``, or
    warns "Could not convert") is the fallback set.  If pandoc cannot be run
    at all we return True — better to leave a formula as (possibly ugly) TeX
    than to crop something we could not verify needed cropping.
    """
    src = f"$${body.strip()}$$\n"
    try:
        r = subprocess.run(
            [
                "pandoc",
                "--from",
                "markdown+tex_math_dollars",
                "--to",
                "html5",
                "--mathml",
            ],
            input=src,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    return "<math" in r.stdout and "Could not convert" not in (r.stderr or "")


# -- pure geometry: docling bbox -> pixel crop box --------------------------
def bbox_pixel_box(
    bbox, page_w, page_h, img_w, img_h, *, origin="BOTTOMLEFT", pad_x=0.05, pad_y=0.12
):
    """Map a docling formula ``bbox`` to a ``(left, top, right, bottom)`` pixel
    box inside a rendered raster of ``img_w`` x ``img_h``.

    The math is done in normalized fractions of the docling page space, so it
    is immune to any absolute-size mismatch between docling's page units and
    the renderer's.  Docling's default ``BOTTOMLEFT`` origin has y growing
    upward, so the box's *top* edge is the larger y (``t``); ``TOPLEFT`` has y
    growing downward.  ``pad_x`` / ``pad_y`` widen the box by a fraction of its
    own size — vertical padding is larger because docling's ``t`` tends to sit
    a hair below a fraction's numerator and would otherwise clip it.
    """
    l, t, r, b = bbox["l"], bbox["t"], bbox["r"], bbox["b"]
    if origin == "BOTTOMLEFT":
        top_f = (page_h - t) / page_h
        bot_f = (page_h - b) / page_h
    else:  # TOPLEFT
        top_f = t / page_h
        bot_f = b / page_h
    if bot_f < top_f:  # guard swapped/odd edges
        top_f, bot_f = bot_f, top_f
    left_f, right_f = l / page_w, r / page_w
    if right_f < left_f:
        left_f, right_f = right_f, left_f
    dw = (right_f - left_f) * pad_x
    dh = (bot_f - top_f) * pad_y

    def _clamp(v, hi):
        return min(hi, max(0, round(v)))

    # Clamp BOTH edges of each axis into [0, size].  Because left_f<=right_f
    # and top_f<=bot_f after the swap guards and clamping is monotonic, the
    # result is never inverted — a fully off-page bbox collapses to a
    # zero-area box (caught by the caller) instead of a negative-size one that
    # would make PIL.crop().save() raise and abort the whole conversion.
    left = _clamp((left_f - dw) * img_w, img_w)
    top = _clamp((top_f - dh) * img_h, img_h)
    right = _clamp((right_f + dw) * img_w, img_w)
    bottom = _clamp((bot_f + dh) * img_h, img_h)
    return (left, top, right, bottom)


# -- rendering primitives ---------------------------------------------------
def render_pdf_page(pdf, page_no, *, dpi=300):
    """Render a 1-based ``page_no`` of ``pdf`` to a PIL image at ``dpi``.

    ``pdf`` may be a path (opened and closed here) or an already-open
    ``pypdfium2.PdfDocument`` (left open, so the caller can render several
    pages without re-parsing the file).
    """
    own = not isinstance(pdf, pdfium.PdfDocument)
    doc = pdfium.PdfDocument(str(pdf)) if own else pdf
    try:
        return doc[page_no - 1].render(scale=dpi / 72.0).to_pil()
    finally:
        if own:
            doc.close()


def crop_region(image, box):
    """Crop ``box`` (left, top, right, bottom) out of a PIL ``image``."""
    return image.crop(box)


def crop_formula(
    pdf,
    page_no,
    bbox,
    out_png,
    *,
    page_w,
    page_h,
    origin="BOTTOMLEFT",
    dpi=300,
    pad_x=0.05,
    pad_y=0.12,
):
    """Render ``page_no`` of ``pdf`` and crop ``bbox`` to ``out_png``.

    ``page_w`` / ``page_h`` are the bbox's page space (docling JSON
    ``pages[].size``).  Returns the saved ``Path``.
    """
    image = render_pdf_page(pdf, page_no, dpi=dpi)
    box = bbox_pixel_box(
        bbox,
        page_w,
        page_h,
        image.size[0],
        image.size[1],
        origin=origin,
        pad_x=pad_x,
        pad_y=pad_y,
    )
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    crop_region(image, box).save(out_png)
    return out_png


# -- md rewrite: swap un-renderable display formulas for PDF image crops -----
def _fence_mask(lines):
    mask = [False] * len(lines)
    in_fence = False
    for i, line in enumerate(lines):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            mask[i] = True  # the fence marker line itself is "code"
            continue
        mask[i] = in_fence
    return mask


def apply_formula_image_fallback(
    md_text,
    pdf_path,
    formula_boxes,
    media_dir,
    *,
    media_ref_prefix,
    dpi=300,
    name_prefix="formula",
    can_render=None,
):
    """Rewrite display formulas pandoc cannot MathML-render into PDF crops.

    Walks the ``$$...$$`` blocks in reading order; the ``i``-th block is taken
    to correspond to ``formula_boxes[i]`` — docling lists formulas in the
    markdown in the same order as in the JSON the boxes came from.  A block
    ``can_render`` accepts is left as math; one it rejects is cropped from
    ``pdf_path`` at ``dpi`` into ``media_dir`` and replaced with an
    ``![<tex>](<media_ref_prefix>/<png>)`` reference (a sanitized copy of the
    TeX is kept as alt text).  Code-aware: ``$$`` inside a code FENCE or an
    inline code span is ignored and does NOT consume a formula index, so
    alignment survives code that happens to contain ``$$``.  A formula with no
    matching bbox, or whose bbox maps to a degenerate/off-page crop, is left
    untouched rather than mis-cropped or crashed on.  Returns
    ``(new_md, n_cropped)``.
    """
    can_render = can_render or pandoc_can_mathml
    lines = md_text.split("\n")
    mask = _fence_mask(lines)
    media_dir = Path(media_dir)
    state = {"idx": 0, "n": 0, "pdf": None, "page_no": None, "page_img": None}
    render_cache = {}

    def _can_render(body):
        # Memoize the (subprocess-backed) oracle: one pandoc spawn per DISTINCT
        # body instead of one per formula occurrence.
        if body not in render_cache:
            render_cache[body] = can_render(body)
        return render_cache[body]

    def _page(page_no):
        # Formulas are visited in reading order (non-decreasing page_no), so a
        # single-slot cache dedupes same-page crops without ever holding more
        # than one full-DPI raster in memory (a math-heavy book can hit the
        # fallback on hundreds of pages).
        if state["page_no"] != page_no:
            if state["pdf"] is None:
                state["pdf"] = pdfium.PdfDocument(str(pdf_path))
            state["page_img"] = render_pdf_page(state["pdf"], page_no, dpi=dpi)
            state["page_no"] = page_no
        return state["page_img"]

    def seg_repl(m):
        if m.group(1) is not None:  # inline code span: verbatim, no index used
            return m.group(0)
        i = state["idx"]
        state["idx"] += 1
        body = m.group(2)[2:-2]  # strip the $$ delimiters
        if _can_render(body):
            return m.group(0)
        if i >= len(formula_boxes):
            return m.group(0)
        rec = formula_boxes[i]
        bbox = rec.get("bbox")
        if not bbox or not rec.get("page_w") or not rec.get("page_h"):
            return m.group(0)
        page = _page(rec["page_no"])
        box = bbox_pixel_box(
            bbox,
            rec["page_w"],
            rec["page_h"],
            page.size[0],
            page.size[1],
            origin=rec.get("coord_origin", "BOTTOMLEFT"),
        )
        if box[2] <= box[0] or box[3] <= box[1]:  # degenerate/off-page crop
            return m.group(0)  # leave as text; never hand PIL an empty box
        media_dir.mkdir(parents=True, exist_ok=True)
        png_name = f"{name_prefix}_{i:04d}.png"
        crop_region(page, box).save(media_dir / png_name)
        state["n"] += 1
        return f"![{_safe_alt(body)}]({media_ref_prefix}/{png_name})"

    try:
        out = [
            line if mask[i] else _SEGMENT_RE.sub(seg_repl, line)
            for i, line in enumerate(lines)
        ]
    finally:
        if state["pdf"] is not None:
            state["pdf"].close()
    return "\n".join(out), state["n"]
