"""Markdown -> EPUB via Calibre, with Calibre's slow auto-split bypassed.

Why this exists
---------------
`md2epub.py` shells out to Calibre and hands it one giant markdown file.
Calibre then walks the rendered HTML looking for H1/H2 boundaries and size
limits ("Splitting markup on page breaks and flow limits") and emits dozens
of xhtml chunks.  On the AWS-WAF corpus this stage alone consumes ~30 of
the ~50 minute wall time.

This module renders markdown to XHTML *itself*, splits at H1 (subdividing
oversized H1 chapters at H2 so no chunk exceeds Calibre's flow-size limit),
emits an OPF + spine wrapping the chunks, and hands that OPF to
`ebook-convert`.  Calibre still does TOC assembly, CSS polish, metadata,
cover handling and final EPUB packaging -- the parts it is fast at -- but
the splitting stage has nothing to do because the input is already split.

Two routes were considered:

Route A (chosen): pre-render markdown -> many xhtml -> OPF -> Calibre.
  - Pros: zero work for Calibre's splitter; full control over chunk
          boundaries; matches the H1/H2/H3 toc xpath in `md2epub.py`
          because the rendered headings carry the same tags.
  - Cons: markdown rendering moves out of Calibre's python-markdown to
          markdown-it-py.  Smart quotes / em-dash typography matches
          via typographer=True; `attr_list` IDs are re-injected by
          post-processing the rendered HTML with lxml.

Route B (rejected): split markdown into N tempfiles, use Calibre's
  recipe / glob.  Calibre's markdown input plugin reads a single file
  and there is no documented multi-file markdown ingest; an OPF
  manifest accepts xhtml, not markdown.  Route A subsumes any value
  Route B could offer.

Feature parity vs `md2epub.py`
------------------------------
Matched
  * tables, fenced_code (python-markdown ext) -> markdown-it `table`
    + builtin fence rule.
  * smarty -> markdown-it `typographer=True` (smartquotes + replacements
    "..." -> ellipsis, "--" -> en-dash, "---" -> em-dash).
  * attr_list `{#id}` after headings -> stripped before render, then
    re-injected onto the matching `<hN>` via lxml in document order.
  * --chapter "/" + --chapter-mark none, --level1/2/3-toc xpath,
    --epub-version 3, --output-profile tablet, --pretty-print,
    --minimum-line-height 130, --smarten-punctuation, --extra-css,
    --book-producer, --title/--authors/--publisher/--tags/--cover.
  * --flow-size set very high + --dont-split-on-page-breaks so Calibre
    keeps our spine as-is.

Gaps (documented, not fatal)
  * `abbr` (`*[FOO]: bar` -> <abbr>) -- the corpus has zero abbreviations
    so we don't implement it.  Re-introduce by pre-scanning the markdown
    for `*[X]:` definitions if a future corpus needs it.
  * `def_list` -- 14 occurrences in the AWS-WAF corpus render as
    paragraphs+blank-line "<dl>" loss rather than HTML <dl><dt><dd>.
    Visually still readable; semantics lose the dl structure.
  * The typographer's smartquote heuristic differs slightly from
    python-markdown's smarty (e.g. apostrophe handling around contractions
    inside a `<code>` span).  Negligible in body prose.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Iterable

from lxml import etree
from markdown_it import MarkdownIt

from ._tools import ebook_convert_path, share_dir


# ---- knobs -----------------------------------------------------------------

# Calibre's default flow-size is 260 KB.  Pre-split aggressively so each
# rendered xhtml is below this; Calibre will then keep our chunks intact.
MAX_CHUNK_BYTES = 200 * 1024

# Effectively disable Calibre's auto-split in the pipeline we hand off.
# Anything > FLOW_SIZE_DISABLE_KB still triggers a split, so set very high.
FLOW_SIZE_DISABLE_KB = 10 * 1024  # 10 MB; bigger than the entire input

XML_NS = "http://www.w3.org/1999/xhtml"
OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"


# ---- split-at-H1 (and H2 when needed) --------------------------------------

# Match "# heading" but not "## heading"; allow trailing space variants.
_H1_RE = re.compile(r"^# (?!#)", re.MULTILINE)
_H2_RE = re.compile(r"^## (?!#)", re.MULTILINE)
_HEADING_LINE_RE = re.compile(r"^(#{1,6}) +(.+?)\s*$")
_ATTR_ID_RE = re.compile(r"\s+\{#([A-Za-z][\w.:-]*)\}\s*$")


def _split_at_h1(md: str) -> list[str]:
    """Slice markdown text on every H1 boundary.

    The first slice contains any preamble before the first H1 (in
    practice that's empty for our corpus, but we keep it so we never
    silently drop content).
    """
    indices = [m.start() for m in _H1_RE.finditer(md)]
    if not indices:
        return [md]
    chunks: list[str] = []
    if indices[0] > 0:
        chunks.append(md[: indices[0]])
    for i, start in enumerate(indices):
        end = indices[i + 1] if i + 1 < len(indices) else len(md)
        chunks.append(md[start:end])
    return chunks


def _subsplit_at_h2(chunk: str, max_bytes: int) -> list[str]:
    """If a single H1 chunk is too big, also split on its H2 boundaries.

    Consecutive small H2 sections are coalesced so we don't produce an
    avalanche of tiny xhtml files.
    """
    if len(chunk.encode("utf-8")) <= max_bytes:
        return [chunk]

    indices = [m.start() for m in _H2_RE.finditer(chunk)]
    if not indices:
        return [chunk]

    raw: list[str] = []
    if indices[0] > 0:
        raw.append(chunk[: indices[0]])
    for i, start in enumerate(indices):
        end = indices[i + 1] if i + 1 < len(indices) else len(chunk)
        raw.append(chunk[start:end])

    # Coalesce: pack consecutive H2 slices together while we're under
    # the budget; flush when adding the next one would tip us over.
    out: list[str] = []
    cur = ""
    for s in raw:
        if not cur:
            cur = s
            continue
        if len((cur + s).encode("utf-8")) > max_bytes:
            out.append(cur)
            cur = s
        else:
            cur += s
    if cur:
        out.append(cur)
    return out


def chunk_markdown(md: str, max_bytes: int = MAX_CHUNK_BYTES) -> list[str]:
    """Public entry point used by tests.

    Returns the markdown sliced into chunks suitable for one-xhtml-per-chunk
    rendering: H1 boundaries are always respected; oversized H1 chapters
    are further subdivided at H2.
    """
    chunks: list[str] = []
    for h1 in _split_at_h1(md):
        chunks.extend(_subsplit_at_h2(h1, max_bytes))
    return [c for c in chunks if c.strip()]


# ---- attr_list shim --------------------------------------------------------

def _extract_heading_ids(md: str) -> tuple[str, list[str | None]]:
    """Strip `{#id}` suffixes off heading lines, recording them in order.

    Returns (cleaned_markdown, ids_in_document_order) where the i-th entry
    of `ids_in_document_order` is the id (or None) for the i-th heading
    in the cleaned markdown.  Order matches what a left-to-right HTML
    walk of `<h1>..<h6>` will see.
    """
    ids: list[str | None] = []
    out_lines: list[str] = []
    for line in md.splitlines():
        m = _HEADING_LINE_RE.match(line)
        if not m:
            out_lines.append(line)
            continue
        idmatch = _ATTR_ID_RE.search(line)
        if idmatch:
            ids.append(idmatch.group(1))
            line = _ATTR_ID_RE.sub("", line)
        else:
            ids.append(None)
        out_lines.append(line)
    return "\n".join(out_lines), ids


# ---- markdown -> XHTML -----------------------------------------------------

def _make_md_parser() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"typographer": True, "html": False, "linkify": False})
    md.enable(["table", "strikethrough", "smartquotes", "replacements"])
    return md


def _render_to_body(md_parser: MarkdownIt, src: str, heading_ids: list[str | None]) -> str:
    """Render markdown to an XHTML body fragment with heading IDs injected.

    We let markdown-it produce HTML, then re-parse it with lxml so we can
    walk headings in document order and attach the IDs we stripped out
    above.  lxml's html5 parser is permissive enough for the wide range
    of markdown output, and serialising back as xhtml keeps Calibre's
    EPUB input happy.
    """
    html = md_parser.render(src)
    # Wrap so lxml's HTML parser gets a single root, and so we can find
    # headings in the body afterwards.
    wrapper = f"<div>{html}</div>"
    root = etree.HTML(f"<html><body>{wrapper}</body></html>")
    if root is None:
        return wrapper  # nothing to fix, return as-is

    body = root.find("body")
    if body is None:
        return wrapper

    # Walk headings in document order, attach IDs from our list.
    headings = body.iter("h1", "h2", "h3", "h4", "h5", "h6")
    for h, hid in zip(headings, heading_ids):
        if hid:
            h.set("id", hid)

    # Serialise just the inner contents of <body>'s first <div>.
    container = body[0]
    parts = []
    if container.text:
        parts.append(container.text)
    for child in container:
        parts.append(etree.tostring(child, encoding="unicode", method="xml"))
    return "".join(parts)


# ---- XHTML / OPF assembly --------------------------------------------------

_XHTML_TEMPLATE = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<!DOCTYPE html>\n'
    '<html xmlns="{ns}" xml:lang="{lang}">\n'
    '  <head>\n'
    '    <meta charset="utf-8"/>\n'
    '    <title>{title}</title>\n'
    '    <link rel="stylesheet" type="text/css" href="epub.css"/>\n'
    '  </head>\n'
    '  <body>\n{body}\n  </body>\n'
    '</html>\n'
)


def _xhtml_for_chunk(body_xhtml: str, title: str, lang: str) -> str:
    # The text contained in chunk titles may have markup characters; we
    # only need a safe <title> string, so strip tags and escape & < >.
    bare = re.sub(r"<[^>]+>", "", title).strip()[:120] or "Chapter"
    bare = bare.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _XHTML_TEMPLATE.format(ns=XML_NS, lang=lang, title=bare, body=body_xhtml)


def _first_heading_text(md_chunk: str) -> str:
    for line in md_chunk.splitlines():
        m = _HEADING_LINE_RE.match(line)
        if m:
            t = m.group(2)
            t = _ATTR_ID_RE.sub("", t).strip()
            return t
    return "Chapter"


def _opf_manifest(items: list[tuple[str, str, str]],
                  spine_ids: list[str],
                  *,
                  title: str | None,
                  authors: str | None,
                  language: str,
                  publisher: str | None,
                  tags: str | None,
                  book_producer: str,
                  cover_id: str | None) -> str:
    """Assemble a minimal EPUB 3 OPF.

    items: list of (manifest_id, href, media_type)
    spine_ids: subset of manifest IDs in reading order.
    """
    book_uuid = "urn:uuid:" + str(uuid.uuid4())

    metadata: list[str] = []
    metadata.append(f'<dc:identifier id="bookid">{book_uuid}</dc:identifier>')
    metadata.append(f'<dc:title>{_x(title or "Untitled")}</dc:title>')
    metadata.append(f'<dc:language>{_x(language)}</dc:language>')
    if authors:
        # Calibre's --authors flag uses '&' as separator; mirror that.
        for a in [s.strip() for s in authors.split("&") if s.strip()]:
            metadata.append(f'<dc:creator>{_x(a)}</dc:creator>')
    if publisher:
        metadata.append(f'<dc:publisher>{_x(publisher)}</dc:publisher>')
    if tags:
        for t in [s.strip() for s in tags.split(",") if s.strip()]:
            metadata.append(f'<dc:subject>{_x(t)}</dc:subject>')
    metadata.append(f'<dc:contributor>{_x(book_producer)}</dc:contributor>')
    # EPUB 3 requires dcterms:modified.
    metadata.append(
        '<meta property="dcterms:modified">1970-01-01T00:00:00Z</meta>'
    )
    if cover_id:
        metadata.append(f'<meta name="cover" content="{cover_id}"/>')

    manifest = "\n    ".join(
        f'<item id="{mid}" href="{href}" media-type="{mt}"/>'
        for mid, href, mt in items
    )
    spine = "\n    ".join(f'<itemref idref="{sid}"/>' for sid in spine_ids)

    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<package xmlns="{OPF_NS}" version="3.0" unique-identifier="bookid">\n'
        f'  <metadata xmlns:dc="{DC_NS}">\n    '
        + "\n    ".join(metadata)
        + "\n  </metadata>\n"
        f'  <manifest>\n    {manifest}\n  </manifest>\n'
        f'  <spine>\n    {spine}\n  </spine>\n'
        "</package>\n"
    )


def _x(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ---- main entry point ------------------------------------------------------

def md2epub_chunked(md_in: Path, epub_out: Path, *,
                    title: str | None = None,
                    authors: str | None = None,
                    language: str = "en",
                    publisher: str | None = None,
                    tags: str | None = None,
                    cover: Path | None = None,
                    extra_css: Path | None = None,
                    book_producer: str = "pdf2epub-pro") -> Path:
    """Convert markdown to EPUB via pre-chunked OPF + Calibre.

    Functionally equivalent to `md2epub.md2epub()` from the orchestrator's
    point of view: same signature, same EPUB metadata, same TOC depth,
    same stylesheet.  Internally pre-splits at H1 (subdividing on H2 when
    a chapter exceeds the flow-size budget) so Calibre's auto-split has
    no work to do.
    """
    md_in = Path(md_in)
    epub_out = Path(epub_out)
    if extra_css is None:
        candidate = share_dir() / "epub.css"
        if candidate.exists():
            extra_css = candidate

    raw = md_in.read_text(encoding="utf-8")
    cleaned, all_ids = _extract_heading_ids(raw)
    chunks = chunk_markdown(cleaned)
    if not chunks:
        raise ValueError(f"markdown {md_in} produced zero chunks")

    parser = _make_md_parser()

    with tempfile.TemporaryDirectory(prefix="pdf2epub-chunked-") as td:
        workdir = Path(td)

        # Walk chunks, partitioning the heading-id list among them in
        # document order so post-processing can re-attach IDs.
        cursor = 0  # index into all_ids
        manifest: list[tuple[str, str, str]] = []
        spine_ids: list[str] = []

        for i, chunk_md in enumerate(chunks, 1):
            n_headings = sum(
                1 for line in chunk_md.splitlines() if _HEADING_LINE_RE.match(line)
            )
            ids_for_chunk = all_ids[cursor: cursor + n_headings]
            cursor += n_headings

            body = _render_to_body(parser, chunk_md, ids_for_chunk)
            chunk_title = _first_heading_text(chunk_md)
            xhtml = _xhtml_for_chunk(body, chunk_title, language)

            fname = f"chunk-{i:05d}.xhtml"
            (workdir / fname).write_text(xhtml, encoding="utf-8")
            mid = f"c{i:05d}"
            manifest.append((mid, fname, "application/xhtml+xml"))
            spine_ids.append(mid)

        # CSS
        if extra_css and extra_css.exists():
            shutil.copy(extra_css, workdir / "epub.css")
            manifest.append(("style", "epub.css", "text/css"))

        # Cover image entry only -- we let Calibre handle cover XHTML
        # synthesis (via --cover) for visual parity with md2epub.py.
        cover_id = None
        if cover and cover.exists():
            ext = cover.suffix.lower().lstrip(".") or "jpg"
            media = {
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
                "png": "image/png",
                "webp": "image/webp",
            }.get(ext, "image/jpeg")
            shutil.copy(cover, workdir / f"cover.{ext}")
            cover_id = "cover-image"
            manifest.append((cover_id, f"cover.{ext}", media))

        opf_xml = _opf_manifest(
            manifest, spine_ids,
            title=title, authors=authors, language=language,
            publisher=publisher, tags=tags, book_producer=book_producer,
            cover_id=cover_id,
        )
        opf_path = workdir / "book.opf"
        opf_path.write_text(opf_xml, encoding="utf-8")

        _run_calibre(
            opf_path, epub_out,
            title=title, authors=authors, language=language,
            publisher=publisher, tags=tags, cover=cover,
            extra_css=extra_css, book_producer=book_producer,
        )

    return epub_out


def _run_calibre(opf_path: Path, epub_out: Path, *,
                 title: str | None, authors: str | None,
                 language: str, publisher: str | None,
                 tags: str | None, cover: Path | None,
                 extra_css: Path | None, book_producer: str) -> None:
    """Hand the OPF to Calibre with auto-split disabled.

    All the toc / output-profile / pretty-print / minimum-line-height /
    smarten-punctuation flags from `md2epub.py` are preserved verbatim so
    the resulting EPUB is structurally indistinguishable.  The new bits
    are `--flow-size` (set absurdly high) and `--dont-split-on-page-breaks`
    so Calibre treats our pre-split spine as final.
    """
    cmd = [
        ebook_convert_path(), str(opf_path), str(epub_out),
        # Same chapter / TOC config as md2epub.py.
        "--chapter", "/",
        "--chapter-mark", "none",
        "--level1-toc", "//*[local-name()='h1']",
        "--level2-toc", "//*[local-name()='h2']",
        "--level3-toc", "//*[local-name()='h3']",
        # Disable Calibre's auto-split entirely.
        "--flow-size", str(FLOW_SIZE_DISABLE_KB),
        "--dont-split-on-page-breaks",
        # Output formatting / typography (mirror md2epub.py).
        "--output-profile", "tablet",
        "--epub-version", "3",
        "--pretty-print",
        "--minimum-line-height", "130",
        "--smarten-punctuation",
        "--language", language,
        "--book-producer", book_producer,
    ]
    if title:
        cmd += ["--title", title]
    if authors:
        cmd += ["--authors", authors]
    if publisher:
        cmd += ["--publisher", publisher]
    if tags:
        cmd += ["--tags", tags]
    if extra_css:
        cmd += ["--extra-css", str(extra_css)]
    if cover:
        cmd += ["--cover", str(cover)]

    subprocess.run(cmd, check=True)


def main(argv: Iterable[str] | None = None):
    p = argparse.ArgumentParser(
        prog="pdf2epub-build-chunked",
        description="Run Calibre with pre-chunked input (auto-split bypassed).",
    )
    p.add_argument("md_in")
    p.add_argument("epub_out")
    p.add_argument("--title")
    p.add_argument("--authors")
    p.add_argument("--language", default="en")
    p.add_argument("--publisher")
    p.add_argument("--tags")
    p.add_argument("--cover")
    p.add_argument("--extra-css")
    args = p.parse_args(argv)

    md2epub_chunked(
        Path(args.md_in),
        Path(args.epub_out),
        title=args.title,
        authors=args.authors,
        language=args.language,
        publisher=args.publisher,
        tags=args.tags,
        cover=Path(args.cover) if args.cover else None,
        extra_css=Path(args.extra_css) if args.extra_css else None,
    )


if __name__ == "__main__":
    main()
