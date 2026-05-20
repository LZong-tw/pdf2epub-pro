# X / Twitter thread

**Tweet 1 (hook):**
Spent the weekend turning a 1000-page AWS whitepaper into a clean EPUB that reads on a plane.

Shipping the pipeline: pdf2epub-pro — Docling + Calibre + link recovery + offline ref fetcher.

🧵 ↓

https://github.com/LZong-tw/pdf2epub-pro

**Tweet 2:**
Calibre's PDF→EPUB on its own loses too much — links become plain text, headings collapse, dotted-leader TOCs end up in the body.

Docling's ML parser fixes the text quality, but it OOMs around page 150 on most laptops. Fix: split with pypdfium2, run 20 pages per subprocess.

**Tweet 3:**
The detail that took longest: Docling's markdown serializer drops PDF `Link` annotations.

So I walk the PDF separately, extract every (rect, URI) pair, look up text under each rect, and re-attach as [text](url) in the markdown.

Naive O(N×M) was 4 hours for 6,622 links. Single alternation regex: under a minute.

**Tweet 4:**
Best feature for offline reading: a "Referenced Content" appendix.

For each external URL matching the whitelist, fetch + Trafilatura extract + cache + embed as a final chapter. The 1000-page WAF doc grew from 3 MB to 13 MB but now reads end-to-end without internet.

**Tweet 5:**
Calibre quirk worth noting:
`--level1-toc //h:h1` (the documented form) does NOT match markdown-derived HTML.
`//*[local-name()='h1']` does. Cost me an hour.

**Tweet 6 (close):**
MIT licensed. Calibre + Docling are the heavy deps.

PRs welcome — the tidy rules are AWS-flavored right now, would love patches for other doc styles.

https://github.com/LZong-tw/pdf2epub-pro
