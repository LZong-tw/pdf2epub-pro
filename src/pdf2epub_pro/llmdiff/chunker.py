"""Pick PDF pages and locate their counterpart chunk inside the EPUB.

For each sampled page we pull a distinctive 8–12 word phrase from the PDF text
layer and search for it across every XHTML document inside the EPUB. The
returned :class:`Chunk` carries enough information for :mod:`.renderer` to
re-locate the section later: the PDF page index, the EPUB filename, a
``(start, end)`` paragraph range, and the literal phrase we anchored on.

The aligner is intentionally simple. It is *good enough* to put both renderers
on the same section of text; the LLM does the heavy lifting of comparing the
actual content. When alignment fails (no phrase matched), we still emit a
chunk with ``epub_file=""`` so the caller can decide whether to drop it or
surface it as a finding.
"""
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium
from lxml import etree, html as lxml_html

# Heuristic: characters that, when split on, give us "words" for anchor
# selection. Keep letters, digits, and a handful of stable punctuation.
_WORD_RE = re.compile(r"\S+")

# Phrases of this length tend to be unique enough to locate exactly one
# paragraph in the EPUB. Too short → false matches; too long → headers
# and figure captions exceed the limit and we miss the page.
_ANCHOR_MIN_WORDS = 8
_ANCHOR_MAX_WORDS = 12

# Skip lines that are pure boilerplate (page numbers, single tokens, etc.)
# when picking an anchor — they are too short and rarely unique.
_BOILERPLATE_LINE_RE = re.compile(r"^\s*(?:\d{1,4}|[A-Z][.)]|\W)\s*$")


@dataclass
class Chunk:
    """One aligned chunk: a PDF page paired with an EPUB section."""

    pdf_page: int
    """0-indexed PDF page number this chunk was sampled from."""

    epub_file: str
    """Name of the XHTML file inside the EPUB that contains the anchor
    phrase. Empty string when alignment failed."""

    epub_para_range: tuple[int, int]
    """``(start_para_idx, end_para_idx)`` block indices inside the XHTML
    file. End is **exclusive**, matching Python slice conventions. The
    range covers the paragraph holding the anchor phrase plus a small
    context window so the rendered image shows surrounding text."""

    anchor_text: str
    """Literal 8–12 word phrase that was found in both the PDF and the
    EPUB. Useful for debugging mis-alignments."""


def _pdf_page_text(pdf_path: Path, page_idx: int) -> str:
    """Return the raw text layer for one PDF page, or an empty string."""
    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        if page_idx >= len(doc):
            return ""
        page = doc[page_idx]
        try:
            tp = page.get_textpage()
            try:
                return tp.get_text_bounded() or ""
            finally:
                tp.close()
        finally:
            page.close()
    finally:
        doc.close()


def _evenly_spaced_indices(total: int, n: int) -> list[int]:
    """Return ``n`` page indices including 0 and ``total-1`` when possible.

    For ``n <= 2`` we just return endpoints; for ``n > total`` we return
    every page once."""
    if total <= 0 or n <= 0:
        return []
    if n >= total:
        return list(range(total))
    if n == 1:
        return [0]
    if n == 2:
        return [0, total - 1]
    # First, last, and N-2 evenly spaced interior pages.
    interior = n - 2
    step = (total - 1) / (interior + 1)
    picks = {0, total - 1}
    for i in range(1, interior + 1):
        picks.add(round(i * step))
    out = sorted(picks)
    # If rounding collapsed two adjacent picks, top up from any
    # un-picked index near the middle so we still return ``n`` chunks.
    if len(out) < n:
        remaining = [i for i in range(total) if i not in picks]
        for extra in remaining:
            out.append(extra)
            out.sort()
            if len(out) == n:
                break
    return out[:n]


def _pick_anchor(page_text: str) -> str:
    """Find the longest run of consecutive non-empty words on the page
    capped at ``_ANCHOR_MAX_WORDS`` and at least ``_ANCHOR_MIN_WORDS``.

    Returns ``""`` when the page has no suitable run (e.g. images-only
    page, scanned PDF without OCR)."""
    if not page_text:
        return ""
    best: list[str] = []
    for raw_line in page_text.splitlines():
        line = raw_line.strip()
        if not line or _BOILERPLATE_LINE_RE.match(line):
            continue
        words = _WORD_RE.findall(line)
        if len(words) < _ANCHOR_MIN_WORDS:
            # If the line is short, try concatenating with the running
            # candidate so we still get an anchor for caption-heavy pages.
            if len(best) < _ANCHOR_MAX_WORDS:
                best.extend(words)
                if len(best) >= _ANCHOR_MAX_WORDS:
                    best = best[:_ANCHOR_MAX_WORDS]
                    break
            continue
        best = words[:_ANCHOR_MAX_WORDS]
        break
    if len(best) < _ANCHOR_MIN_WORDS:
        return ""
    return " ".join(best)


def _epub_xhtml_members(epub_path: Path) -> list[str]:
    """List the in-order XHTML/HTML members of an EPUB archive."""
    with zipfile.ZipFile(epub_path, "r") as zf:
        names = [
            n for n in zf.namelist()
            if n.lower().endswith((".xhtml", ".html", ".htm"))
        ]
    # OEBPS reading-order is governed by the OPF spine. For a *sampling*
    # tool the simpler lexical sort is fine — every file gets searched.
    names.sort()
    return names


def _read_xhtml(epub_path: Path, member: str) -> str:
    with zipfile.ZipFile(epub_path, "r") as zf:
        with zf.open(member) as fh:
            return fh.read().decode("utf-8", errors="replace")


def _block_texts(xhtml_src: str) -> list[str]:
    """Flatten an XHTML document into a list of block-level text strings.

    Each ``<p>``, ``<li>``, ``<h1>``–``<h6>``, ``<td>``, ``<figcaption>``,
    and ``<blockquote>`` element contributes one entry. Order is preserved
    so the returned indices line up with the paragraphs as a human reads
    them.

    lxml refuses Unicode input that carries an ``<?xml ... encoding=...?>``
    declaration, so we always feed it raw UTF-8 bytes.
    """
    src_bytes = xhtml_src.encode("utf-8") if isinstance(xhtml_src, str) else xhtml_src
    try:
        root = lxml_html.fromstring(src_bytes)
    except (etree.XMLSyntaxError, etree.ParserError, ValueError):
        # Hard fallback: regex through visible text.
        return [s.strip() for s in re.findall(r">([^<]+)<", xhtml_src) if s.strip()]
    if root is None:
        return []
    blocks: list[str] = []
    for el in root.iter():
        tag = el.tag if isinstance(el.tag, str) else ""
        if tag in {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6",
                   "td", "figcaption", "blockquote"}:
            text = " ".join(el.text_content().split())
            if text:
                blocks.append(text)
    return blocks


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _find_anchor(blocks: list[str], anchor: str) -> int:
    """Return the index of the first block containing ``anchor`` (case- and
    whitespace-insensitive), or -1."""
    if not anchor:
        return -1
    needle = _normalize(anchor)
    if not needle:
        return -1
    for i, block in enumerate(blocks):
        if needle in _normalize(block):
            return i
    # Fall back to shrinking the anchor from the right — a phrase can be
    # split across blocks by a column break or footnote.
    words = anchor.split()
    for cut in range(len(words) - 1, _ANCHOR_MIN_WORDS - 1, -1):
        shorter = " ".join(words[:cut])
        if not shorter:
            break
        n2 = _normalize(shorter)
        for i, block in enumerate(blocks):
            if n2 in _normalize(block):
                return i
    return -1


def _locate_in_epub(epub_path: Path, anchor: str,
                    context: int = 2) -> tuple[str, tuple[int, int]]:
    """Search every XHTML in the EPUB and return ``(filename, range)``.

    ``range`` is a ``(start, end)`` paragraph index pair centered on the
    matched block with ``context`` blocks on either side (clamped).

    Returns ``("", (0, 0))`` when no file contains the anchor."""
    if not anchor:
        return "", (0, 0)
    for member in _epub_xhtml_members(epub_path):
        try:
            src = _read_xhtml(epub_path, member)
        except (KeyError, OSError):
            continue
        blocks = _block_texts(src)
        idx = _find_anchor(blocks, anchor)
        if idx >= 0:
            start = max(0, idx - context)
            end = min(len(blocks), idx + context + 1)
            return member, (start, end)
    return "", (0, 0)


def sample_chunks(pdf_path: str | Path, epub_path: str | Path,
                  n: int = 5) -> list[Chunk]:
    """Pick ``n`` PDF pages and align each with an EPUB section.

    Pages chosen: first, last, and ``n-2`` evenly-spaced interior pages.
    An 8–12 word anchor phrase is pulled from each page's text layer
    and located inside the EPUB's XHTML members.
    """
    pdf_p = Path(pdf_path)
    epub_p = Path(epub_path)
    doc = pdfium.PdfDocument(str(pdf_p))
    total_pages = len(doc)
    doc.close()
    indices = _evenly_spaced_indices(total_pages, n)
    out: list[Chunk] = []
    for page_idx in indices:
        text = _pdf_page_text(pdf_p, page_idx)
        anchor = _pick_anchor(text)
        member, rng = _locate_in_epub(epub_p, anchor)
        out.append(Chunk(
            pdf_page=page_idx,
            epub_file=member,
            epub_para_range=rng,
            anchor_text=anchor,
        ))
    return out
