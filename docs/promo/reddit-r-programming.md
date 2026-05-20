# Reddit /r/programming

**Title:**
pdf2epub-pro: ML-based PDF→EPUB pipeline that recovers Calibre's dropped hyperlinks and embeds referenced web content as an offline appendix

**Flair:** Project / Show off

**Body:**

Calibre's PDF→EPUB is OK but loses a lot — hyperlinks vanish, heading hierarchy collapses to one flat level, dotted-leader TOC tables get carried into the body as ugly markdown. I wanted whitepaper-grade EPUBs that read end-to-end offline.

Pipeline:

1. **Chunked Docling for parsing.** Docling (IBM Research's ML PDF parser) gives way cleaner text + heading structure than Calibre's heuristics, but its layout transformer accumulates memory and OOMs around page 150. I split with pypdfium2 and run N pages per subprocess.

2. **Tidy pass** — strips the dotted-leader TOC, missed page numbers, chunk dividers; heals lists that Docling broke by injecting blank lines mid-list; rejoins half-sentences split into separate paragraphs (detect: line ends without `.!?` AND next non-blank starts lowercase). Optional AWS-flavored rules promote the 6 Well-Architected pillars to H1.

3. **Link restoration.** Docling's markdown serializer drops `Link` annotations. So I walk the PDF separately via `pypdfium2.raw`, extract every (rectangle, URI) pair, get the text under each rectangle with `FPDFText_GetBoundedText`, and re-wrap matches in the markdown. The fun part: naive per-key regex compile is O(N×M) and would take 4+ hours on a doc with 6,622 link annotations. One big alternation regex makes it O(N), finishes in a minute.

4. **Reference fetcher.** For each external link matching a whitelist (AWS blogs, whitepapers, WA docs), fetch with `requests`, extract main content via Trafilatura, cache by URL hash, append all as one "Appendix: Referenced Content" chapter at the end. Self-contained reading material.

5. **Procedural cover.** Pillow-based deterministic cover generator (no AI). Hexagonal-pillars or ascending-graph variants.

6. **Calibre ebook-convert.** Curated flag set including `--level1-toc "//*[local-name()='h1']"` because the documented `//h:h1` namespace prefix doesn't match markdown-derived HTML (took me an hour to figure that out).

Tested on AWS Well-Architected Framework (1002 pages, 6,622 links, 1040 fetched refs → 13.5 MB EPUB).

MIT licensed: https://github.com/LZong-tw/pdf2epub-pro

PRs welcome especially for non-AWS heading rule sets.
