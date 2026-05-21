"""Fetch high-value external refs from a markdown doc and embed as appendix.

Walks every `[text](url)` in the input md, keeps URLs matching KEEP_PATTERNS,
fetches with `requests`, extracts main content via `trafilatura`, caches
under `~/.cache/pdf2epub-refs/<sha1>.md`, and appends an
"Appendix: Referenced Content" section to the output md.
"""
import argparse
import hashlib
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
import trafilatura


_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _absolutize_links(body: str, source_url: str) -> str:
    """Trafilatura output preserves relative links from the source page.
    Inside an EPUB those refs (./foo.html, #section-X) don't resolve. Convert
    them to absolute URLs against the article URL so they at least open in
    the user's browser.
    """
    def repl(m):
        text, href = m.group(1), m.group(2).strip()
        if href.startswith(("http://", "https://", "mailto:", "tel:")):
            return m.group(0)
        return f"[{text}]({urljoin(source_url, href)})"
    return _MD_LINK_RE.sub(repl, body)

CACHE = Path.home() / ".cache" / "pdf2epub-refs"

# Default keep filter is tuned for AWS docs; pass --keep-pattern to extend.
DEFAULT_KEEP_PATTERNS = [
    r"aws\.amazon\.com/blogs/",
    r"docs\.aws\.amazon\.com/whitepapers/",
    r"docs\.aws\.amazon\.com/wellarchitected/",
    r"aws\.amazon\.com/architecture/",
    r"aws\.amazon\.com/getting-started/",
    r"aws\.amazon\.com/solutions/",
    r"^https?://github\.com/[^/]+/[^/]+/?$",
    r"docs\.aws\.amazon\.com/[^/]+/latest/(userguide|developerguide|APIReference|gsg|dg|API)/",
]
DEFAULT_SKIP_PATTERNS = [
    r"/signin",
    r"/console\.",
    r"/products/",
    r"#aws-account-",
    r"\?",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; pdf2epub-pro/0.1; archive)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
    "Accept-Language": "en-US,en;q=0.5",
}
DELAY = 1.5


def make_filter(keep_patterns, skip_patterns):
    keep = [re.compile(p) for p in keep_patterns]
    skip = [re.compile(p) for p in skip_patterns]
    def should_keep(url: str) -> bool:
        if any(s.search(url) for s in skip):
            return False
        return any(k.search(url) for k in keep)
    return should_keep


def cache_path(url: str) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha1(url.encode()).hexdigest()
    return CACHE / f"{h}.md"


def fetch_one(url: str):
    cp = cache_path(url)
    if cp.exists():
        text = cp.read_text(encoding="utf-8")
        m = re.match(r"<!-- title: (.*?) -->\n", text)
        title = m.group(1) if m else url
        body = text[m.end():] if m else text
        # Re-absolutize relative links from older cached entries (idempotent).
        body = _absolutize_links(body, url)
        return {"title": title, "url": url, "content": body, "cached": True}

    try:
        r = requests.get(url, headers=HEADERS, timeout=30, allow_redirects=True)
    except Exception as e:
        sys.stderr.write(f"[fetch-refs] ERROR {url}: {e}\n")
        return None
    if r.status_code != 200:
        sys.stderr.write(f"[fetch-refs] HTTP {r.status_code} {url}\n")
        return None

    html = r.text
    metadata = trafilatura.extract_metadata(html)
    title = (metadata.title if metadata else None) or url
    title = title.replace("\n", " ").strip()

    body = trafilatura.extract(
        html,
        output_format="markdown",
        include_links=True,
        include_images=False,
        favor_recall=True,
    )
    if not body or len(body) < 200:
        sys.stderr.write(f"[fetch-refs] short body, skipping {url}\n")
        return None

    body = _absolutize_links(body, url)
    cp.write_text(f"<!-- title: {title} -->\n{body}", encoding="utf-8")
    time.sleep(DELAY)
    return {"title": title, "url": url, "content": body, "cached": False}


def fetch_refs(md_in: Path, md_out: Path, *,
               keep_patterns=None, skip_patterns=None, delay: float | None = None):
    global DELAY
    if delay is not None:
        DELAY = delay
    keep_patterns = keep_patterns or DEFAULT_KEEP_PATTERNS
    skip_patterns = skip_patterns or DEFAULT_SKIP_PATTERNS
    should_keep = make_filter(keep_patterns, skip_patterns)

    text = md_in.read_text(encoding="utf-8")
    urls = list(dict.fromkeys(re.findall(r"\]\((https?://[^)\s]+)\)", text)))
    keep = [u for u in urls if should_keep(u)]
    print(f"[fetch-refs] {len(urls)} unique URLs; {len(keep)} match keep filters",
          flush=True)

    refs = []
    for i, url in enumerate(keep, 1):
        result = fetch_one(url)
        status = ("cached" if result and result["cached"]
                  else "fetched" if result else "skip")
        print(f"[fetch-refs] {i}/{len(keep)} {status} {url}", flush=True)
        if result:
            refs.append(result)

    if not refs:
        md_out.write_text(text, encoding="utf-8")
        return 0

    parts = [text.rstrip(), "\n\n# Appendix: Referenced Content\n"]
    parts.append("\nThis appendix archives a snapshot of the externally referenced "
                 "content at the time this EPUB was built. Original links are "
                 "preserved inline; refer to the source URL for the latest version.\n")
    for r in refs:
        parts.append(f"\n## {r['title']}\n")
        parts.append(f"\nSource: <{r['url']}>\n")
        parts.append(f"\n{r['content']}\n")
    md_out.write_text("".join(parts), encoding="utf-8")
    print(f"[fetch-refs] wrote {md_out} with {len(refs)} embedded refs",
          flush=True)
    return len(refs)


def main(argv=None):
    p = argparse.ArgumentParser(prog="pdf2epub-refs")
    p.add_argument("md_in")
    p.add_argument("md_out")
    p.add_argument("--delay", type=float, default=1.5,
                   help="Seconds between requests (default 1.5)")
    args = p.parse_args(argv)
    fetch_refs(Path(args.md_in), Path(args.md_out), delay=args.delay)


if __name__ == "__main__":
    main()
