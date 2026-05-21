"""Run Calibre's ebook-convert with the curated set of EPUB build flags.

Delegates entirely to `ebook-convert`; this module only assembles arguments.
Pairs with our custom stylesheet under `share/epub.css`.
"""
import argparse
import subprocess
from pathlib import Path

from ._tools import ebook_convert_path, ebook_meta_path, share_dir


def md2epub(md_in: Path, epub_out: Path, *,
            title: str | None = None,
            authors: str | None = None,
            language: str = "en",
            publisher: str | None = None,
            tags: str | None = None,
            cover: Path | None = None,
            extra_css: Path | None = None,
            book_producer: str = "pdf2epub-pro") -> Path:
    """Convert markdown to EPUB via Calibre. Returns epub_out."""
    if extra_css is None:
        candidate = share_dir() / "epub.css"
        if candidate.exists():
            extra_css = candidate

    cmd = [
        ebook_convert_path(), str(md_in), str(epub_out),
        # Disable Calibre's default chapter heuristic: by default it greedily
        # matches H1/H2 with words like "chapter"/"section"/"part" and
        # inserts a pagebreak before each. We want the ONLY pagebreak driver
        # to be our stylesheet's `h1 { page-break-before: always }` so that
        # non-H1 headings keep flowing inline.
        "--chapter", "/",
        "--chapter-mark", "none",
        "--level1-toc", "//*[local-name()='h1']",
        "--level2-toc", "//*[local-name()='h2']",
        "--level3-toc", "//*[local-name()='h3']",
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
    return epub_out


def embed_cover(epub_path: Path, cover: Path) -> None:
    subprocess.run(
        [ebook_meta_path(), str(epub_path), "--cover", str(cover)],
        check=True,
    )


def main(argv=None):
    p = argparse.ArgumentParser(prog="pdf2epub-build",
                                description="Run Calibre with the curated EPUB flags.")
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

    md2epub(
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
