# Reddit /r/selfhosted (or /r/Calibre, /r/Kindle)

**Title:**
Built a PDF→EPUB pipeline that pulls hyperlinks + referenced web pages into the EPUB itself, so technical docs read offline end-to-end

**Body:**

I read on my Kindle (and on planes), and Calibre's direct PDF→EPUB drops too much — hyperlinks vanish, the dotted-leader Table of Contents from the PDF ends up in the body as a giant ugly markdown table, headings come out flat.

`pdf2epub-pro` is my fix. Six stages:

1. **Docling** for ML-based parsing (much cleaner text than Calibre's heuristics) — chunked through subprocesses so it doesn't OOM on long PDFs
2. **Tidy** — strip the embedded TOC, page numbers, chunk dividers; heal broken lists and sentences
3. **Link restoration** — Docling drops PDF link annotations, so I walk the PDF separately and re-attach them
4. **Reference fetcher** — for each external URL in the doc (AWS blogs, whitepapers, etc.), fetch + extract main content with Trafilatura + cache + embed as a final chapter. The whole book stays readable offline.
5. **Cover** generated procedurally (no AI)
6. **Calibre ebook-convert** with curated flags

Tested on AWS Well-Architected Framework (1002 pages, 6,622 hyperlinks). The final EPUB is 13.5 MB with 1040 referenced articles inlined.

GitHub: https://github.com/LZong-tw/pdf2epub-pro

MIT. Calibre and Docling are the prereqs. Works on Windows/macOS/Linux.
