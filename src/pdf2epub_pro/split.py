"""Chunked PDF→Markdown via Docling subprocess (memory-safe for large PDFs)."""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pypdfium2 as pdfium

from ._tools import docling_path


def split_pdf(src: Path, chunk_size: int, work_dir: Path):
    pdf = pdfium.PdfDocument(str(src))
    total = len(pdf)
    out = []
    for start in range(0, total, chunk_size):
        end = min(start + chunk_size, total)
        new = pdfium.PdfDocument.new()
        new.import_pages(pdf, pages=list(range(start, end)))
        chunk = work_dir / f"chunk_{start:05d}_{end:05d}.pdf"
        new.save(str(chunk))
        new.close()
        out.append((chunk, start, end))
    pdf.close()
    return out, total


def run_docling(chunk_pdf: Path, out_dir: Path, with_images: bool):
    env = {**os.environ, "CUDA_VISIBLE_DEVICES": "-1"}
    cmd = [
        docling_path(), str(chunk_pdf),
        "--pipeline", "standard",
        "--to", "md",
        "--image-export-mode", "referenced" if with_images else "placeholder",
        "--no-enrich-code",
        "--no-enrich-formula",
        "--no-enrich-picture-classes",
        "--no-enrich-picture-description",
        "--no-enrich-chart-extraction",
        "--output", str(out_dir),
    ]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        sys.stderr.write(r.stdout)
        sys.stderr.write(r.stderr)
        return None
    return next(iter(out_dir.glob("*.md")), None)


# NOTE: the character class excludes only `)` and newlines — NOT all
# whitespace.  Docling emits the image as `![Image](<absolute path>)` and
# on Windows that path routinely contains spaces ("C:\Users\First Last\...",
# "C:\Program Files\...").  A `[^)\s]` class stops at the first space, so
# the path never matches, `absorb_artifacts` moves the image but leaves the
# markdown ref pointing at the now-deleted temp path, and every diagram
# silently disappears from the EPUB.  Bound the path by the closing paren of
# the markdown image syntax (and newlines) instead of by whitespace.
_IMG_PATH_RE = re.compile(
    r"(?:[A-Za-z]:[\\/]|/)[^)\r\n]*?image_\d+_[0-9a-f]+\.png", re.IGNORECASE
)


def safe_artifacts_dirname(stem: str) -> str:
    """Return a filesystem- and URL-safe artifacts dir name for ``stem``.

    The returned name is embedded verbatim into every rewritten image ref
    as a relative markdown path (``<name>/c0001_image_….png``).  If it
    contains spaces — as it does whenever the source PDF filename does,
    e.g. ``Distributed Systems 4th Edition`` — the ref is not a valid
    CommonMark link destination, so markdown-it / pandoc emit no ``<img>``
    at all and every diagram silently vanishes from the EPUB.  Collapse
    anything outside a conservative ``[A-Za-z0-9._-]`` set to ``_`` so the
    ref always parses and always resolves back to the on-disk directory
    (which is created from this same name).
    """
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("_") or "doc"
    return f"{safe}_artifacts"


def absorb_artifacts(md_text, chunk_out, md_stem, global_artifacts, chunk_index):
    art = chunk_out / f"{md_stem}_artifacts"
    if not art.is_dir():
        return md_text
    prefix = f"c{chunk_index:04d}"
    global_artifacts.mkdir(parents=True, exist_ok=True)
    name_map = {}
    for img in art.iterdir():
        target_name = f"{prefix}_{img.name}"
        shutil.move(str(img), str(global_artifacts / target_name))
        name_map[img.name] = f"{global_artifacts.name}/{target_name}"

    def repl(m):
        base = Path(m.group(0).replace("\\", "/")).name
        return name_map.get(base, m.group(0))

    return _IMG_PATH_RE.sub(repl, md_text)


def split_pdf_to_md(pdf_path: Path, out_md: Path, chunk_size: int = 20,
                    with_images: bool = True) -> Path:
    """Programmatic entry: run the chunked pipeline; returns out_md."""
    pdf_path = pdf_path.resolve()
    out_md = out_md.resolve()
    global_artifacts = out_md.with_name(safe_artifacts_dirname(out_md.stem))
    if with_images and global_artifacts.exists():
        shutil.rmtree(global_artifacts)

    with tempfile.TemporaryDirectory(prefix="docling_") as tmp:
        tmp = Path(tmp)
        chunks_dir = tmp / "chunks"
        chunks_dir.mkdir()
        chunks, total = split_pdf(pdf_path, chunk_size, chunks_dir)
        print(f"[split] {pdf_path.name}: {total} pages -> {len(chunks)} chunks of "
              f"<={chunk_size}  ({'with' if with_images else 'no'} images)", flush=True)

        parts = []
        for i, (chunk_pdf, start, end) in enumerate(chunks, 1):
            label = f"pages {start+1}-{end}"
            chunk_out = tmp / f"out_{start:05d}"
            chunk_out.mkdir()
            print(f"[{i}/{len(chunks)}] {label} ...", flush=True)
            md = run_docling(chunk_pdf, chunk_out, with_images)
            if md is None:
                print(f"[{i}/{len(chunks)}] {label} FAILED", flush=True)
                parts.append(f"\n<!-- [chunk {label} failed] -->\n")
                continue
            text = md.read_text(encoding="utf-8")
            if with_images:
                text = absorb_artifacts(text, chunk_out, md.stem,
                                        global_artifacts, i)
            parts.append(text)

        out_md.write_text("\n\n".join(parts), encoding="utf-8")
        size_md = out_md.stat().st_size
        size_art = (sum(f.stat().st_size for f in global_artifacts.rglob("*"))
                    if global_artifacts.exists() else 0)
        print(f"[done] wrote {out_md} ({size_md:,} bytes); "
              f"artifacts {size_art:,} bytes", flush=True)
    return out_md


def main(argv=None):
    p = argparse.ArgumentParser(prog="pdf2epub-split",
                                description="Chunked PDF→Markdown via Docling.")
    p.add_argument("pdf")
    p.add_argument("md_out")
    p.add_argument("--chunk-size", type=int, default=20)
    p.add_argument("--no-images", action="store_true")
    args = p.parse_args(argv)
    split_pdf_to_md(Path(args.pdf), Path(args.md_out), args.chunk_size,
                    not args.no_images)


if __name__ == "__main__":
    main()
