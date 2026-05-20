# pdf2epub-pro

High-quality PDF → EPUB pipeline. Better than running Calibre directly on a PDF, because it:

1. **Reflows text via Docling (ML)** — preserves heading structure, tables, and code blocks instead of Calibre's heuristic line unwrap.
2. **Restores hyperlinks Docling dropped** — pulls every `Link` annotation rectangle + URI from the PDF and re-attaches them in the markdown.
3. **Embeds referenced web content as an offline appendix** — fetches each high-value external URL (e.g. AWS blog posts) and inlines a cleaned markdown snapshot via Trafilatura, so the EPUB reads end-to-end without the browser.
4. **Chunked + memory-safe** — splits long PDFs into N-page sub-PDFs, runs each in its own Docling subprocess. Conservatively handles the layout transformer's memory accumulation that otherwise OOMs on >150-page documents on a typical laptop.
5. **Generates a clean cover** — procedural Pillow design (no AI required), tunable palette + variant.
6. **Curated Calibre flags** — `--epub-version 3`, three-level TOC from `//*[local-name()='h1/h2/h3']` (the standard `//h:h1` doesn't match markdown-derived HTML), justified text, fixed line height, smart quotes, custom stylesheet.

Built to convert AWS Well-Architected whitepapers into readable EPUBs that survive a long flight, but the pipeline is general — pass `--ruleset generic` and any born-digital PDF works.

## Install

```bash
# Calibre is required (provides ebook-convert + ebook-meta).
# Windows:  https://calibre-ebook.com/download_windows
# macOS:    brew install --cask calibre
# Linux:    sudo apt install calibre

# Install pdf2epub-pro itself (with uv recommended, pip works too).
uv tool install pdf2epub-pro
# or:
pipx install pdf2epub-pro
```

First run downloads Docling's layout/table models (~1.5 GB) to `~/.cache/huggingface/`.

## Quick start

```bash
pdf2epub-pro convert "some-paper.pdf" \
    --output-dir ~/ebooks \
    --title "Some Paper" \
    --authors "Author Name" \
    --cover-super "Whitepaper" \
    --cover-subtitle "Subtitle here"
```

Produces:

```
~/ebooks/
├── some-paper.epub                    # the deliverable
└── some-paper-build/                  # intermediates (keep or delete)
    ├── some-paper.md                  # raw Docling output
    ├── some-paper.tidy.md             # after tidy
    ├── some-paper.tidy.linked.md      # after link restoration
    ├── some-paper.tidy.linked.refs.md # after ref fetch
    ├── some-paper_artifacts/          # extracted images
    └── cover.jpg
```

### Common options

| Flag | Default | Effect |
|---|---|---|
| `--chunk-size N` | `20` | Pages per Docling subprocess. Lower if you hit OOM. |
| `--no-images` | off | Skip image extraction (text-only EPUB, less memory). |
| `--ruleset {aws,generic}` | `aws` | AWS tidy rules add pillar promotion + FSI question demotion. `generic` keeps only universal cleanups (TOC strip, sentence heal, etc.). |
| `--no-fetch-refs` | off | Skip the appendix fetch step. |
| `--fetch-delay 1.5` | 1.5s | Politeness delay between external requests. |
| `--no-cover` | off | Don't generate a cover. |
| `--cover-variant {pillars,graph}` | `pillars` | Cover ornament. |

### Per-stage scripts

The pipeline is also exposed as individual commands you can mix-and-match or wrap into your own scripts:

```bash
pdf2epub-split  paper.pdf  paper.md  --chunk-size 20
pdf2epub-tidy   paper.md   paper.tidy.md   --title "Some Paper"
pdf2epub-links  paper.pdf  paper.tidy.md   paper.tidy.linked.md
pdf2epub-refs   paper.tidy.linked.md       paper.tidy.linked.refs.md
pdf2epub-cover  cover.jpg  --title "Some|Paper" --subtitle "..." --variant pillars
pdf2epub-build  paper.tidy.linked.refs.md  paper.epub --title "Some Paper" --cover cover.jpg
```

## Why each step exists

### Why chunked Docling instead of one pass

Docling's `standard` pipeline rasterizes every page for the layout transformer. On a 200-page PDF with limited RAM the C++ allocator throws `std::bad_alloc` around page 160. Running 20 pages per subprocess resets the allocator and the same hardware handles a 1000-page PDF without trouble.

### Why we restore links separately

Docling's markdown serializer drops `Link` annotations (the rectangle + URI metadata) and only keeps the visible text. `pypdfium2` lets us walk the same PDF, extract every link rectangle, look up the text under it, and wrap matches in the markdown with `[text](uri)` — using a single alternation regex so 6000+ links match in linear time.

### Why we fetch refs into an appendix

AWS whitepapers and similar technical PDFs link to dozens or hundreds of external blog posts / docs. Online they're fine; offline (Kindle on a plane) the links are dead ends. Trafilatura is the most accurate open extractor for "what's the article text on this page" — we keep just the article body, cache by URL hash, and append everything as one chapter at the end of the EPUB.

### Why `//*[local-name()='h1']` instead of `//h:h1`

Calibre's `--level1-toc //h:h1` is the documented form but doesn't match `<h1>` elements when the input is markdown-derived HTML — the elements aren't placed in the `h:` namespace. `//*[local-name()='h1']` matches by local-name regardless of namespace, which restores TOC detection.

## Pipeline diagram

```
PDF ─▶ pypdfium2 split ─▶ ┐
                          ├─▶ chunked Docling ─▶ raw .md + _artifacts/
                          ┘
raw .md ─▶ tidy ─▶ tidy .md
tidy .md + PDF annotations ─▶ restore-links ─▶ linked .md
linked .md ─▶ fetch-refs (requests + trafilatura) ─▶ refs .md
              ├─▶ caches under ~/.cache/pdf2epub-refs/
refs .md + cover.jpg + epub.css ─▶ Calibre ebook-convert ─▶ EPUB
```

## Requirements

- Python ≥ 3.10
- [Calibre](https://calibre-ebook.com/) (provides `ebook-convert` + `ebook-meta`)
- ~2 GB disk for the first run's model downloads
- No GPU required; CPU is fine for everything

## Legal note

This is a build tool. The output EPUB inherits the source PDF's copyright. AWS whitepapers, for example, are © Amazon — you can format-shift for personal reading but redistribution is on you. The tool itself is MIT.

## License

[MIT](LICENSE) © Lim Un-tiong
