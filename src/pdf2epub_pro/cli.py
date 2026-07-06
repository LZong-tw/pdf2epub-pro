"""Top-level CLI: orchestrate the full PDF → EPUB pipeline.

    pdf2epub-pro convert <pdf> [--output-dir <dir>] [--build-dir <dir>]
                          [--title T] [--authors A] [--no-fetch-refs]
                          [--no-cover] [--chunk-size N]
                          [--ruleset aws|generic]
    pdf2epub-pro clean [--stem <stem>]

Stages: split → tidy → restore-links → fetch-refs → cover → md2epub.
Final EPUB lands in ``--output-dir``.  Intermediate artifacts live
under a tool-managed cache (see :mod:`workspace`) so they don't
pollute Downloads/Desktop.  Override per-run with ``--build-dir`` or
permanently with the ``PDF2EPUB_BUILD_ROOT`` environment variable.
"""
import argparse
import json
import shutil
from pathlib import Path

from . import __version__
from .split import safe_artifacts_dirname, split_pdf_to_md
from .tidy import tidy
from .restore_links import restore_pdf_links
from .fetch_refs import fetch_refs
from .make_cover import make_cover, render_pdf_cover
from .formula_fallback import apply_formula_image_fallback
from .md2epub import md2epub
from .md2epub_pandoc import md2epub_pandoc
from .md2epub_chunked import md2epub_chunked
from .workspace import (
    build_dir_for,
    clean_build_dirs,
    default_build_root,
    list_build_dirs,
)


# Backend dispatch: --synthesizer picks the markdown→EPUB stage.
# Default is `pandoc` — broadest feature parity (typography,
# definition lists, etc.) AND 30× faster than baseline `calibre` on
# large books (WAF: ~1m40s vs ~50min) with 0-error audit parity after
# the +ascii_identifiers + dedupe-id passes.  `calibre` is the
# historical reference implementation kept for fallback when pandoc is
# unavailable or output bytes need to exactly match older builds.
# `chunked` pre-renders the markdown via markdown-it-py then hands
# Calibre an OPF so its auto-split stage is a no-op (~34s on WAF) —
# fastest of the three, opt-in for users who already have Calibre and
# don't need pandoc's typography extensions.
_SYNTHESIZERS = {
    "pandoc": md2epub_pandoc,
    "calibre": md2epub,
    "chunked": md2epub_chunked,
}


def cmd_convert(args):
    pdf = Path(args.pdf).resolve()
    out_dir = Path(args.output_dir).resolve() if args.output_dir else pdf.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    stem = pdf.stem
    # Intermediate artifacts go to a tool-managed cache by default —
    # never beside the user's Downloads-bound EPUB.  --build-dir can
    # point to a specific path or a shared parent (we still scope by
    # stem under it so multiple books co-exist).
    if args.build_dir:
        build_root = Path(args.build_dir).resolve()
        build = build_dir_for(stem, root=build_root)
    else:
        build = build_dir_for(stem)

    raw_md = build / f"{stem}.md"
    tidy_md = build / f"{stem}.tidy.md"
    linked_md = build / f"{stem}.tidy.linked.md"
    refs_md = build / f"{stem}.tidy.linked.refs.md"
    cover_path = build / "cover.jpg"
    epub_out = out_dir / f"{stem}.epub"

    # 1. split
    split_pdf_to_md(pdf, raw_md, chunk_size=args.chunk_size,
                    with_images=not args.no_images,
                    enrich_formula=args.math, emit_json=args.math)

    # 2. tidy
    tidy_text = tidy(raw_md.read_text(encoding="utf-8"),
                     doc_title=args.title, ruleset=args.ruleset,
                     math=args.math)

    # 2b. formula image fallback (under --math): any display formula pandoc
    #     cannot turn into MathML is cropped straight from the PDF so it ships
    #     as the author's own typeset image instead of raw TeX text.
    if args.math and not args.no_formula_image_fallback:
        sidecar = build / f"{stem}.formulas.json"
        if sidecar.exists():
            boxes = json.loads(sidecar.read_text(encoding="utf-8"))
            art_name = safe_artifacts_dirname(raw_md.stem)
            tidy_text, n_crop = apply_formula_image_fallback(
                tidy_text, pdf, boxes, build / art_name,
                media_ref_prefix=art_name, dpi=args.formula_dpi)
            if n_crop:
                print(f"[pdf2epub-pro] formula fallback: cropped {n_crop} "
                      "un-renderable formula(s) from the PDF")
        else:
            print("[pdf2epub-pro] note: --math with no formula bbox sidecar; "
                  "un-renderable formulas will remain literal $$...$$ text.")

    tidy_md.write_text(tidy_text, encoding="utf-8")

    # 3. restore links from PDF annotations
    restore_pdf_links(pdf, tidy_md, linked_md)

    # 4. fetch external refs into appendix
    if not args.no_fetch_refs:
        fetch_refs(linked_md, refs_md, delay=args.fetch_delay,
                   ruleset=args.ruleset)
        final_md = refs_md
    else:
        final_md = linked_md

    # 5. cover (if not disabled).  Default source is the PDF's own first
    #    page -- born-digital PDFs almost always carry the real cover
    #    there.  Any --cover-* styling flag (or --cover-source generated)
    #    opts into the procedural cover instead.
    cover_arg = None
    if not args.no_cover:
        wants_generated = bool(args.cover_title or args.cover_super
                               or args.cover_subtitle
                               or args.cover_variant != "pillars")
        source = args.cover_source
        if source == "auto":
            source = "generated" if wants_generated else "pdf"
        if source == "pdf":
            try:
                render_pdf_cover(pdf, cover_path)
                cover_arg = cover_path
            except Exception as exc:
                if args.cover_source == "pdf":
                    raise
                print(f"[cover] page-1 render failed ({exc}); "
                      "falling back to the generated cover")
        if cover_arg is None:
            # Cover text falls back to --title if no --cover-title
            # supplied.  Use '|' inside --cover-title to break the cover
            # heading across multiple lines without polluting the EPUB
            # metadata title.
            cover_text = args.cover_title or args.title or stem
            title_lines = cover_text.split("|")
            make_cover(
                cover_path,
                super_title=args.cover_super or "",
                main_title=title_lines,
                subtitle=args.cover_subtitle or "",
                publisher=args.authors or "",
                variant=args.cover_variant,
            )
            cover_arg = cover_path

    # 6. md → EPUB via the selected synthesizer (default: pandoc — see
    #    _SYNTHESIZERS for trade-off notes).
    synth = _SYNTHESIZERS[args.synthesizer]
    synth_kwargs = dict(
        title=args.title,
        authors=args.authors,
        language=args.language,
        publisher=args.authors,
        tags=args.tags,
        cover=cover_arg,
    )
    if args.synthesizer == "pandoc":
        synth_kwargs["math"] = args.math
    elif args.math:
        print("[pdf2epub-pro] note: --math renders MathML only via the "
              f"pandoc synthesizer; with --synthesizer {args.synthesizer} "
              "the OCR'd formulas remain literal $$...$$ text.")
    synth(final_md, epub_out, **synth_kwargs)

    print(f"\n[pdf2epub-pro] done: {epub_out}")
    print(f"               build artifacts kept in: {build}")
    print(f"               (clean with: pdf2epub-pro clean --stem {stem!r})")


def cmd_clean(args):
    root = Path(args.build_dir).resolve() if args.build_dir else None
    removed = clean_build_dirs(root=root, stem=args.stem)
    if not removed:
        scope = f"stem {args.stem!r}" if args.stem else "build root"
        print(f"[pdf2epub-pro clean] nothing to remove ({scope} empty)")
        return
    for p in removed:
        print(f"[pdf2epub-pro clean] removed {p}")


def build_parser():
    p = argparse.ArgumentParser(prog="pdf2epub-pro", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("convert", help="Run the full PDF→EPUB pipeline.")
    c.add_argument("pdf")
    c.add_argument("--output-dir", "-o", help="Where to write the EPUB.")
    c.add_argument(
        "--build-dir",
        help="Root for intermediate artifacts.  Defaults to a tool-"
             "managed cache (see workspace.default_build_root); each "
             "book gets its own <build-dir>/<stem>/ subtree.  Override "
             "permanently via the PDF2EPUB_BUILD_ROOT environment var.",
    )
    c.add_argument("--title", help="Book title (use '|' for cover line breaks).")
    c.add_argument("--authors", default="Unknown")
    c.add_argument("--language", default="en")
    c.add_argument("--tags", default="")
    c.add_argument("--chunk-size", type=int, default=20)
    c.add_argument("--no-images", action="store_true")
    c.add_argument(
        "--math",
        action="store_true",
        help="OCR mathematical formulas (docling --enrich-formula) and "
             "render them as MathML via the pandoc synthesizer.  Off by "
             "default: it adds ~50%% to parse time and math-free "
             "documents often contain literal '$' that must not be "
             "treated as math delimiters.",
    )
    c.add_argument(
        "--no-formula-image-fallback",
        action="store_true",
        help="Disable the --math fallback that crops formulas pandoc cannot "
             "render as MathML straight from the PDF.  With this flag such "
             "formulas ship as literal TeX text instead of an image.",
    )
    c.add_argument(
        "--formula-dpi",
        type=int,
        default=300,
        help="Rasterization DPI for the formula image fallback (default 300).",
    )
    c.add_argument("--ruleset", default="aws", choices=["aws", "generic"])
    c.add_argument("--no-fetch-refs", action="store_true")
    c.add_argument("--fetch-delay", type=float, default=1.5)
    c.add_argument("--no-cover", action="store_true")
    c.add_argument("--cover-variant", default="pillars",
                   choices=["pillars", "graph"])
    c.add_argument(
        "--cover-source",
        default="auto",
        choices=["auto", "pdf", "generated"],
        help="'pdf' rasterizes the PDF's first page (the book's real "
             "cover), 'generated' draws the procedural cover, 'auto' "
             "(default) uses the PDF page unless a --cover-* styling "
             "flag asks for a generated one.",
    )
    c.add_argument("--cover-title",
                   help="Cover heading text; use '|' for line breaks. "
                        "Defaults to --title if omitted.")
    c.add_argument("--cover-super", default="")
    c.add_argument("--cover-subtitle", default="")
    c.add_argument(
        "--synthesizer",
        default="pandoc",
        choices=sorted(_SYNTHESIZERS.keys()),
        help="markdown→EPUB backend (default: pandoc — ~30× faster than "
             "calibre on large books with 0-error audit parity)",
    )
    c.set_defaults(func=cmd_convert)

    cl = sub.add_parser(
        "clean",
        help="Remove tool-managed intermediate artifacts.  Default "
             "scope is the whole build root; pass --stem <stem> to "
             "scope to a single book's cache.",
    )
    cl.add_argument(
        "--stem",
        help="Only remove the per-stem subtree under the build root.",
    )
    cl.add_argument(
        "--build-dir",
        help="Override the build root (otherwise uses the default "
             "resolved by workspace.default_build_root).",
    )
    cl.set_defaults(func=cmd_clean)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
