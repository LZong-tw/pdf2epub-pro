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

from .tidy import fix_digit_headings_text, strip_orphan_dashes


_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)

# Long backtick-quoted inline code blocks (>200 chars) are almost always
# `<code>` HTML elements that Trafilatura flattened into one giant inline
# span. Promote them to fenced blocks so they render as readable code.
_INLINE_BLOCK_RE = re.compile(r"`([^`\n]{200,})`")
# Inline code containing both braces and newlines is almost certainly meant
# to be multi-line.
_INLINE_MULTILINE_RE = re.compile(r"`([^`]*\n[^`]*)`")


def _fence_inline_code(body: str) -> str:
    """Promote oversized / multiline inline `…` spans to fenced blocks."""
    def lang_for(text: str) -> str:
        t = text.strip()
        if t.startswith(("{", "[")) and t.rstrip().endswith(("}", "]")):
            return "json"
        if t.startswith("<") and ">" in t:
            return "xml"
        if any(kw in t for kw in ("aws ", "sudo ", "$ ", "git ")):
            return "bash"
        return ""

    def repl(m):
        body = m.group(1).strip()
        lang = lang_for(body)
        return f"\n\n```{lang}\n{body}\n```\n\n"
    body = _INLINE_MULTILINE_RE.sub(repl, body)
    body = _INLINE_BLOCK_RE.sub(repl, body)
    return body


# Trafilatura sometimes emits "tables" from AWS doc pages without the
# `| --- | --- |` separator row that python-markdown's tables extension
# requires. Without that header, the markdown parser falls back to plain
# text and the literal "|" pipes leak into the rendered EPUB.
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")


def _fix_broken_tables(body: str) -> str:
    """Salvage malformed pipe-tables; pass real tables through untouched.

    A real pipe table is "header row + separator row + N data rows".  If we
    see a row whose immediate next line is a separator, the entire run of
    pipe rows is emitted verbatim.  Standalone pipe rows (no separator
    next) are flattened into prose joined by em-dashes.
    """
    lines = body.splitlines()
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        is_row = bool(_TABLE_ROW_RE.match(line))
        is_lone = line.strip() == "|"
        if is_lone:
            i += 1
            continue
        if is_row:
            next_line = lines[i + 1] if i + 1 < len(lines) else ""
            if _TABLE_SEP_RE.match(next_line):
                # Emit header + separator + all consecutive data rows as-is.
                out.append(line)
                out.append(next_line)
                i += 2
                while i < len(lines) and _TABLE_ROW_RE.match(lines[i]):
                    out.append(lines[i])
                    i += 1
                continue
            # Broken table: flatten to prose.
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            cells = [c for c in cells if c]
            out.append(" — ".join(cells))
            i += 1
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


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


# Common PDF ligatures (ﬁ / ﬂ / ﬀ / ﬃ / ﬄ) often arrive in Trafilatura's
# output as the two-byte mojibake "ï¬" because the third UTF-8 byte was
# filtered. A round-trip latin-1↔utf-8 can't restore them (the third byte
# is gone); the only safe recovery is a curated word dictionary.
_LIGATURE_FIXES = {
    # fl ligature
    "ï¬ow": "flow", "ï¬ows": "flows", "ï¬owing": "flowing",
    "ï¬oat": "float", "ï¬oats": "floats", "ï¬oating": "floating",
    "ï¬oor": "floor", "ï¬oors": "floors",
    "ï¬exible": "flexible", "ï¬exibility": "flexibility",
    "ï¬ag": "flag", "ï¬ags": "flags",
    "ï¬aws": "flaws", "ï¬aw": "flaw",
    "ï¬uctuate": "fluctuate", "ï¬uctuation": "fluctuation",
    "ï¬ush": "flush",
    # fi ligature
    "ï¬eld": "field", "ï¬elds": "fields",
    "ï¬lter": "filter", "ï¬lters": "filters", "ï¬ltering": "filtering",
    "ï¬nal": "final", "ï¬nally": "finally",
    "ï¬nance": "finance", "ï¬nances": "finances",
    "ï¬nancial": "financial",
    "ï¬nd": "find", "ï¬nding": "finding", "ï¬ndings": "findings",
    "ï¬gure": "figure", "ï¬gures": "figures",
    "ï¬x": "fix", "ï¬xed": "fixed", "ï¬xes": "fixes", "ï¬xing": "fixing",
    "ï¬rst": "first", "ï¬re": "fire", "ï¬ve": "five",
    "deï¬ne": "define", "deï¬nes": "defines", "deï¬ned": "defined",
    "deï¬ning": "defining",
    "deï¬nition": "definition", "deï¬nitions": "definitions",
    "deï¬nitive": "definitive", "deï¬nitively": "definitively",
    "speciï¬c": "specific", "speciï¬cally": "specifically",
    "speciï¬cation": "specification", "speciï¬cations": "specifications",
    "modiï¬ed": "modified", "modiï¬cation": "modification",
    "modiï¬cations": "modifications", "modiï¬er": "modifier",
    "identiï¬er": "identifier", "identiï¬ers": "identifiers",
    "identiï¬ed": "identified",
    "notiï¬cation": "notification", "notiï¬cations": "notifications",
    "veriï¬ed": "verified", "veriï¬cation": "verification",
    "certiï¬ed": "certified", "certiï¬cate": "certificate",
    "certiï¬cates": "certificates", "certiï¬cation": "certification",
    "classiï¬ed": "classified", "classiï¬cation": "classification",
    "qualiï¬ed": "qualified", "qualiï¬cations": "qualifications",
    "simpliï¬ed": "simplified", "simpliï¬cation": "simplification",
    "uniï¬ed": "unified", "diversiï¬ed": "diversified",
    "signiï¬cant": "significant", "signiï¬cantly": "significantly",
    "signiï¬cance": "significance",
    "scientiï¬c": "scientific",
    "magniï¬cent": "magnificent",
    "conï¬dential": "confidential",
    "conï¬guration": "configuration", "conï¬gurations": "configurations",
    "conï¬gured": "configured", "conï¬gure": "configure",
    # ff / ffi / ffl ligatures (less common but present in AWS docs)
    "eï¬ect": "effect", "eï¬ects": "effects",
    "eï¬ective": "effective", "eï¬ectively": "effectively",
    "eï¬ectiveness": "effectiveness",
    "eï¬icient": "efficient", "eï¬iciently": "efficiently",
    "eï¬iciency": "efficiency", "eï¬iciencies": "efficiencies",
    "suï¬icient": "sufficient", "suï¬iciently": "sufficiently",
    "diï¬icult": "difficult", "diï¬iculty": "difficulty",
    "oï¬er": "offer", "oï¬ers": "offers", "oï¬ering": "offering",
    "oï¬icer": "officer", "oï¬icers": "officers",
    "oï¬ice": "office",
    "staï¬": "staff",
    "tariï¬": "tariff", "tariï¬s": "tariffs",
}
_LIGATURE_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in _LIGATURE_FIXES) + r")\b"
)


def _fix_mojibake(text: str) -> str:
    """Heal text that was decoded as Latin-1 when it was really UTF-8.

    Two passes:
      1. Latin-1↔UTF-8 round trip for "â" / "Ã" style mojibake (em-dash,
         smart quotes, accented letters).
      2. Curated dictionary for ligature mojibake (ï¬X), where the third
         UTF-8 byte was dropped and round-tripping can't restore the word.
    """
    if "ï¬" in text:
        text = _LIGATURE_RE.sub(lambda m: _LIGATURE_FIXES[m.group(1)], text)
    if "â" not in text and "Ã" not in text:
        return text
    # Use cp1252 (Windows-1252) — it's a superset of Latin-1 that also covers
    # smart-quote / em-dash / euro-sign mojibake sequences like "â€™" (’).
    try:
        return text.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def _demote_headings(body: str, by: int = 2) -> str:
    """Demote every markdown heading by N levels (capped at H6).

    Trafilatura preserves the source page's heading hierarchy, which means
    each fetched article ships with its own `# Title` H1.  Embedding 1000+
    fetched articles thus injects 1000+ H1s into the EPUB — and with our
    CSS rule `h1 { page-break-before: always }` that explodes into 1000+
    split HTML files.  Demoting by 2 keeps the article's body H1 below
    our wrapper `## Article title` H2, so only the main book has H1s.
    """
    def repl(m):
        new_level = min(len(m.group(1)) + by, 6)
        return f"{'#' * new_level} {m.group(2)}"
    return _HEADING_RE.sub(repl, body)

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
    # Listing / category pages — Trafilatura extracts a list of article
    # previews each truncated with "[...]", which is useless inside an EPUB.
    r"aws\.amazon\.com/blogs/[^/]+/category/",
    r"aws\.amazon\.com/blogs/[^/]+/?$",
    r"aws\.amazon\.com/blogs/?$",
    r"aws\.amazon\.com/architecture/?$",
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
        title = _fix_mojibake(m.group(1)) if m else url
        body = text[m.end():] if m else text
        # Re-run all post-extract passes on the cached body so fixes added
        # after the cache was written still take effect. All are idempotent.
        body = _fix_mojibake(body)
        body = _absolutize_links(body, url)
        body = _fix_broken_tables(body)
        body = _fence_inline_code(body)
        return {"title": title, "url": url, "content": body, "cached": True}

    try:
        r = requests.get(url, headers=HEADERS, timeout=30, allow_redirects=True)
    except Exception as e:
        sys.stderr.write(f"[fetch-refs] ERROR {url}: {e}\n")
        return None
    if r.status_code != 200:
        sys.stderr.write(f"[fetch-refs] HTTP {r.status_code} {url}\n")
        return None

    # AWS docs occasionally advertise the wrong charset in their HTTP header,
    # causing requests to decode UTF-8 bytes as Latin-1 and producing "â"
    # mojibake from em-dashes / quotes. Force UTF-8 so trafilatura sees the
    # right characters.
    r.encoding = "utf-8"
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

    # Trafilatura adds "[...]" markers when it skipped over content. On a
    # category / listing page it does this for every article preview,
    # producing a body full of stubs. Reject if there are more than 2.
    if body.count("[...]") > 2:
        sys.stderr.write(f"[fetch-refs] listing/preview page, skipping {url}\n")
        return None

    body = _absolutize_links(body, url)
    body = _fix_broken_tables(body)
    body = _fence_inline_code(body)
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
    for idx, r in enumerate(refs, 1):
        # Explicit unique ID via python-markdown's attr_list extension so
        # cross-file ID collisions between appendix articles can't happen.
        parts.append(f"\n## {r['title']} {{#ref-{idx:04d}}}\n")
        parts.append(f"\nSource: <{r['url']}>\n")
        parts.append(f"\n{_demote_headings(r['content'])}\n")
    # Run the slug-safety + dash-stripping passes over the whole appendix
    # too — Trafilatura sprinkles lone '-' lines and digit-led headings
    # ('# 7 Pitfalls ...', '# 24×7 ...') that Calibre's parser otherwise
    # turns into invalid XML IDs or spurious `<h2>-</h2>` elements.
    text = "".join(parts)
    text = "\n".join(strip_orphan_dashes(text.splitlines()))
    md_out.write_text(fix_digit_headings_text(text), encoding="utf-8")
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
