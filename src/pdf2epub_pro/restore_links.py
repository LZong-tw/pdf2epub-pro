"""Re-attach lost hyperlinks from a PDF onto its Docling markdown.

Docling drops most `Link` annotation URIs.  We walk the PDF via pypdfium2,
pull each link's URI + text under its rectangle, then wrap the first un-linked
occurrence of that text in the markdown with `[text](uri)`.
"""
import argparse
import ctypes
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as raw

# When a PDF link annotation contains a relative path instead of a full URL
# (rare but happens — e.g. AWS Well-Architected cross-references), prepend
# this base so the EPUB ends up with a valid clickable absolute URL.
DEFAULT_REL_URI_BASE = "https://docs.aws.amazon.com/"

# Annotation extraction is deterministic: the (rect, URI) pairs only change
# when the source PDF itself changes.  Cache by path + mtime + size so a re-
# extraction is forced when the PDF is replaced.
_LINK_CACHE_DIR = Path.home() / ".cache" / "pdf2epub-links"


def _pdf_cache_path(pdf_path: Path) -> Path:
    _LINK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    st = pdf_path.stat()
    raw_key = f"{pdf_path.resolve()}|{st.st_mtime_ns}|{st.st_size}"
    h = hashlib.sha1(raw_key.encode("utf-8")).hexdigest()[:16]
    return _LINK_CACHE_DIR / f"{h}.json"


def _normalize_uri(uri: str, base: str = DEFAULT_REL_URI_BASE) -> str:
    uri = uri.replace("\\", "/").strip()
    if "://" in uri or uri.startswith(("mailto:", "tel:", "#")):
        return uri
    return base + uri.lstrip("/")


_MD_LINK_RE = re.compile(r"\[[^\]]+\]\([^)]+\)")

# Short/common single-word link texts that cause false positives if used as
# regex needles against general prose. Curated from observation on AWS
# whitepapers; extend as you see new false matches.
_STOPWORD_LINK_TEXTS = frozenset({
    "how", "here", "see", "read", "view", "click", "use", "make",
    "get", "visit", "learn", "this", "that", "these", "those", "some",
    "small", "large", "more", "less", "many", "details", "section",
    "overview", "summary", "above", "below", "next", "back",
})


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _is_safe_key(key: str) -> bool:
    norm = _norm(key)
    if norm in _STOPWORD_LINK_TEXTS:
        return False
    # Single short word is too risky — too many incidental matches.
    if " " not in norm and len(norm) < 6:
        return False
    return True


def extract_links(pdf_path: Path, *, no_cache: bool = False):
    cp = _pdf_cache_path(pdf_path)
    if not no_cache and cp.exists():
        cached = json.loads(cp.read_text(encoding="utf-8"))
        print(f"[restore-links] cache hit -> {len(cached)} pairs ({cp.name})",
              flush=True)
        # Re-run normalization so a fix applied after the cache was written
        # (e.g. backslash->slash, relative->absolute) still takes effect.
        return [(t, _normalize_uri(u)) for t, u in cached]

    print(f"[restore-links] scanning {pdf_path.name} ...", flush=True)
    pdf = pdfium.PdfDocument(str(pdf_path))
    pairs = []
    for pi in range(len(pdf)):
        page = pdf[pi]
        textpage = raw.FPDFText_LoadPage(page)
        try:
            n = raw.FPDFPage_GetAnnotCount(page)
            for i in range(n):
                a = raw.FPDFPage_GetAnnot(page, i)
                try:
                    if raw.FPDFAnnot_GetSubtype(a) != raw.FPDF_ANNOT_LINK:
                        continue
                    link = raw.FPDFAnnot_GetLink(a)
                    action = raw.FPDFLink_GetAction(link)
                    n_uri = raw.FPDFAction_GetURIPath(pdf.raw, action, None, 0)
                    if n_uri <= 1:
                        continue
                    buf = ctypes.create_string_buffer(n_uri)
                    raw.FPDFAction_GetURIPath(pdf.raw, action, buf, n_uri)
                    uri = _normalize_uri(
                        buf.value.decode("utf-8", errors="replace"))

                    rect = raw.FS_RECTF()
                    if not raw.FPDFAnnot_GetRect(a, ctypes.byref(rect)):
                        continue
                    nchars = raw.FPDFText_GetBoundedText(
                        textpage, rect.left, rect.top, rect.right, rect.bottom,
                        None, 0,
                    )
                    if nchars <= 0:
                        continue
                    ubuf = (ctypes.c_ushort * (nchars + 1))()
                    raw.FPDFText_GetBoundedText(
                        textpage, rect.left, rect.top, rect.right, rect.bottom,
                        ubuf, nchars,
                    )
                    text = "".join(chr(ch) for ch in ubuf[:nchars] if ch)
                    norm = re.sub(r"\s+", " ", text).strip()
                    if norm and len(norm) > 2:
                        pairs.append((norm, uri))
                finally:
                    raw.FPDFPage_CloseAnnot(a)
        finally:
            raw.FPDFText_ClosePage(textpage)

    cp.write_text(json.dumps(pairs, ensure_ascii=False), encoding="utf-8")
    print(f"[restore-links] {len(pairs)} link annotations found "
          f"(cached -> {cp.name})", flush=True)
    return pairs


def restore(md_text: str, pairs):
    text_counts = Counter(t for t, _ in pairs)
    seen = {}
    for txt, uri in pairs:
        if not _is_safe_key(txt):
            continue
        seen.setdefault(txt, uri)
    if not seen:
        return md_text, 0

    # Prune single-word keys whose body match count blows past their PDF
    # annotation count by more than 3×. The single-word "Resources" annotation
    # appearing once in the PDF should not turn 327 prose occurrences of the
    # word into the same hyperlink. Multi-word keys are precise enough to
    # bypass this check.
    for k in list(seen.keys()):
        if " " in k.strip():
            continue
        pdf_count = text_counts.get(k, 1)
        pat = re.compile(r"\b" + re.escape(k).replace(r"\ ", r"\s+") + r"\b",
                         re.IGNORECASE)
        body_count = sum(1 for _ in pat.finditer(md_text))
        if body_count > pdf_count * 3:
            del seen[k]
    if not seen:
        return md_text, 0

    keys = sorted(seen.keys(), key=len, reverse=True)
    norm_to_uri = {}
    for k in keys:
        norm_to_uri.setdefault(_norm(k), seen[k])

    # Word-boundary anchors on both sides prevent mid-word matches such as
    # `pillar` matching inside `pillars` or `Config` inside `Configure`.
    parts = [re.escape(k).replace(r"\ ", r"\s+") for k in keys]
    big_re = re.compile(
        r"(?<!\[)\b(" + "|".join(parts) + r")\b",
        re.IGNORECASE,
    )

    count = 0
    out_lines = []
    for line in md_text.splitlines():
        if "[" in line and _MD_LINK_RE.search(line):
            out_lines.append(line)
            continue
        m = big_re.search(line)
        if m:
            matched = m.group(1)
            uri = norm_to_uri.get(_norm(matched))
            if uri:
                line = line[: m.start()] + f"[{matched}]({uri})" + line[m.end():]
                count += 1
        out_lines.append(line)
    return "\n".join(out_lines), count


def restore_pdf_links(pdf_path: Path, md_in: Path, md_out: Path,
                      *, no_cache: bool = False) -> int:
    pairs = extract_links(pdf_path, no_cache=no_cache)
    text = md_in.read_text(encoding="utf-8")
    new_text, n = restore(text, pairs)
    md_out.write_text(new_text, encoding="utf-8")
    print(f"[restore-links] wrapped {n} occurrences -> {md_out}", flush=True)
    return n


def main(argv=None):
    p = argparse.ArgumentParser(prog="pdf2epub-links")
    p.add_argument("pdf")
    p.add_argument("md_in")
    p.add_argument("md_out")
    p.add_argument("--no-cache", action="store_true",
                   help="Force re-extraction of PDF annotations.")
    args = p.parse_args(argv)
    restore_pdf_links(Path(args.pdf), Path(args.md_in), Path(args.md_out),
                      no_cache=args.no_cache)


if __name__ == "__main__":
    main()
