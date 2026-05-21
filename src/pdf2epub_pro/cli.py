"""Top-level CLI: orchestrate the full PDF → EPUB pipeline.

    pdf2epub-pro convert <pdf> [--output-dir <dir>] [--title T] [--authors A]
                          [--no-fetch-refs] [--no-cover] [--chunk-size N]
                          [--ruleset aws|generic]

Stages: split → tidy → restore-links → fetch-refs → cover → md2epub.
Intermediate artifacts land under <output-dir>/<stem>-build/.
"""
import argparse
import shutil
from pathlib import Path

from . import __version__
from .split import split_pdf_to_md
from .tidy import tidy
from .restore_links import restore_pdf_links
from .fetch_refs import fetch_refs
from .make_cover import make_cover
from .md2epub import md2epub
from .md2epub_pandoc import md2epub_pandoc
from .md2epub_chunked import md2epub_chunked


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
    build = out_dir / f"{stem}-build"
    build.mkdir(exist_ok=True)

    raw_md = build / f"{stem}.md"
    tidy_md = build / f"{stem}.tidy.md"
    linked_md = build / f"{stem}.tidy.linked.md"
    refs_md = build / f"{stem}.tidy.linked.refs.md"
    cover_path = build / "cover.jpg"
    epub_out = out_dir / f"{stem}.epub"

    # 1. split
    split_pdf_to_md(pdf, raw_md, chunk_size=args.chunk_size,
                    with_images=not args.no_images)

    # 2. tidy
    tidy_md.write_text(
        tidy(raw_md.read_text(encoding="utf-8"),
             doc_title=args.title, ruleset=args.ruleset),
        encoding="utf-8",
    )

    # 3. restore links from PDF annotations
    restore_pdf_links(pdf, tidy_md, linked_md)

    # 4. fetch external refs into appendix
    if not args.no_fetch_refs:
        fetch_refs(linked_md, refs_md, delay=args.fetch_delay)
        final_md = refs_md
    else:
        final_md = linked_md

    # 5. cover (if not disabled)
    cover_arg = None
    if not args.no_cover:
        # Cover text falls back to --title if no --cover-title supplied.
        # Use '|' inside --cover-title to break the cover heading across
        # multiple lines without polluting the EPUB metadata title.
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
    synth(
        final_md,
        epub_out,
        title=args.title,
        authors=args.authors,
        language=args.language,
        publisher=args.authors,
        tags=args.tags,
        cover=cover_arg,
    )

    print(f"\n[pdf2epub-pro] done: {epub_out}")
    print(f"               build artifacts kept in: {build}")


def build_parser():
    p = argparse.ArgumentParser(prog="pdf2epub-pro", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("convert", help="Run the full PDF→EPUB pipeline.")
    c.add_argument("pdf")
    c.add_argument("--output-dir", "-o", help="Where to write the EPUB.")
    c.add_argument("--title", help="Book title (use '|' for cover line breaks).")
    c.add_argument("--authors", default="Unknown")
    c.add_argument("--language", default="en")
    c.add_argument("--tags", default="")
    c.add_argument("--chunk-size", type=int, default=20)
    c.add_argument("--no-images", action="store_true")
    c.add_argument("--ruleset", default="aws", choices=["aws", "generic"])
    c.add_argument("--no-fetch-refs", action="store_true")
    c.add_argument("--fetch-delay", type=float, default=1.5)
    c.add_argument("--no-cover", action="store_true")
    c.add_argument("--cover-variant", default="pillars",
                   choices=["pillars", "graph"])
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
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
