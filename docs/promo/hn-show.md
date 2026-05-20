# Show HN

**Title (80 chars max):**
Show HN: pdf2epub-pro – Docling + Calibre pipeline that restores dropped PDF links

**URL field:** https://github.com/LZong-tw/pdf2epub-pro

**Body (paste this into the text box):**

I built this because Calibre's straight PDF→EPUB conversion loses a lot — hyperlinks become plain text, headings get mangled into one flat level, and AWS-style whitepapers come out with their dotted-leader Tables of Contents intact.

The pipeline is six stages. The interesting ones:

**Chunked Docling.** Docling's transformer-based layout pipeline accumulates memory and the C++ allocator throws `std::bad_alloc` somewhere past page 150 on a typical laptop. I split the PDF with `pypdfium2`, run 20 pages at a time in a subprocess, then merge. A 1000-page document goes through cleanly in ~25 minutes on CPU.

**Link restoration.** Docling's markdown serializer drops PDF `Link` annotations and keeps only the visible text. So I walk the same PDF through `pypdfium2.raw`, pull every annotation's rectangle + URI + the text under it, and re-attach them in the markdown. The naive `for-each-key compile-regex search-line` is O(N×M) — that took 4 hours estimated on a doc with 6,622 annotations. Switching to one big alternation regex makes it O(N), finishes in under a minute.

**Offline appendix.** AWS whitepapers reference hundreds of blog posts. Online links are fine; offline (on a plane) they're dead. I filter to high-value patterns (`aws.amazon.com/blogs/`, `docs.aws.amazon.com/whitepapers/`, etc.), fetch with `requests`, extract the article body with Trafilatura, and append everything as a final chapter. A 1000-page whitepaper grows from ~3 MB EPUB to ~13 MB with 1040 referenced articles embedded.

**TOC quirk worth a writeup.** Calibre's documented `--level1-toc //h:h1` doesn't match `<h1>` elements when the input is markdown-derived HTML (namespace mismatch). `//*[local-name()='h1']` works.

MIT licensed. Calibre and Docling are the heavy dependencies. Tested heavily on AWS Well-Architected whitepapers but `--ruleset generic` works on any born-digital PDF.

Critique welcome — the AWS-specific tidy rules are hard-coded right now, would love thoughts on how to generalize.
