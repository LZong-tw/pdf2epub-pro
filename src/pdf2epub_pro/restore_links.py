"""Re-attach lost hyperlinks from a PDF onto its Docling markdown.

Docling drops most `Link` annotation URIs.  We walk the PDF via pypdfium2,
pull each link's URI + text under its rectangle, then wrap the first un-linked
occurrence of that text in the markdown with `[text](uri)`.
"""
import argparse
import ctypes
import re
from collections import Counter
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as raw


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


def extract_links(pdf_path: Path):
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
                    uri = buf.value.decode("utf-8", errors="replace")

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
    return pairs


def restore(md_text: str, pairs):
    # If the same single-word text appears in more than one PDF link
    # annotation, it's almost certainly a section anchor / cross-reference
    # being repeated, not a per-instance hyperlink. Reusing it as a needle
    # against the markdown would over-link every prose occurrence.
    text_counts = Counter(t for t, _ in pairs)
    seen = {}
    for txt, uri in pairs:
        if not _is_safe_key(txt):
            continue
        if " " not in txt.strip() and text_counts[txt] > 1:
            continue
        seen.setdefault(txt, uri)
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


def restore_pdf_links(pdf_path: Path, md_in: Path, md_out: Path) -> int:
    print(f"[restore-links] scanning {pdf_path.name} ...", flush=True)
    pairs = extract_links(pdf_path)
    print(f"[restore-links] {len(pairs)} link annotations found", flush=True)
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
    args = p.parse_args(argv)
    restore_pdf_links(Path(args.pdf), Path(args.md_in), Path(args.md_out))


if __name__ == "__main__":
    main()
