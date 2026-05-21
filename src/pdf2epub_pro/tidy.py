"""Tidy a Docling-produced markdown for EPUB conversion.

Generic rules (always applied):
- strip_toc            : remove dotted-leader Table of Contents tables
- strip_chunk_dividers : remove the `---` chunk separators we inserted
- strip_orphan_page_numbers : remove bare-number lines surrounded by blanks
- consolidate_title    : promote first matching title H2 to a single H1
- heal_list_gaps       : merge blank-line-broken lists
- heal_broken_sentences: rejoin mid-sentence paragraph breaks

AWS-specific rules (opt-in via --ruleset aws, default ON):
- promote_pillars       : 6 WAF pillars + appendix sections → H1
- demote_subsections    : Reference architecture / Documentation / Blogs … → H3
- bullet-as-H2 → bullet : "## · X" → "- **X**"
- FSI question pattern  : "## FSIxxx01:" → H3
- FSI BP pattern        : "## FSIxxx01-BP01" → H4
- Per-question labels   : Prescriptive guidance / Related documents → H4
"""
import argparse
import re
from pathlib import Path

# -- Generic patterns --------------------------------------------------------
TOC_HEADING_RE = re.compile(r"^\s*##\s+Table of Contents\s*$", re.IGNORECASE)
LIST_ITEM_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+")
_PAGE_NUM_RE = re.compile(r"^\s*\d{1,4}\s*$")
_SENTENCE_END = (".", "!", "?", ":", ";", '"', "”", "’", ")", "]", "}")

# Title-pattern is user-supplied OR derived from the first matching H2.
DEFAULT_TITLE_RE = re.compile(r"^##\s+(.+)$")

# -- AWS-specific (override via Tidier(ruleset=...)) -------------------------
AWS_PILLAR_NAMES = frozenset({
    "Operational excellence", "Security", "Reliability",
    "Performance efficiency", "Cost optimization", "Sustainability",
    "Scenarios", "Introduction", "Contributors", "Document revisions",
    "Notices", "AWS Glossary",
})
AWS_SUBSECTION_H3 = frozenset({
    "Reference architecture", "Architecture description",
    "AI/ML architecture description",
    "Cyber Event Recovery reference architecture",
    "Documentation", "Documents and blogs", "Documents",
    "Blogs", "Workshops", "Whitepapers", "Videos",
    "Training", "Training materials", "Partner solutions",
    "Reference architectures", "For Enterprise Support customers",
})
AWS_TITLE_RE = re.compile(
    r"^##\s+.*(Financial Services Industry Lens|"
    r"Well[- ]?Architected Framework).*$",
    re.IGNORECASE,
)
AWS_H2_RULES = [
    (re.compile(r"^##\s+[·•]\s+(.+?)\s*$"),
     lambda m: f"- **{m.group(1).strip()}**"),
    (re.compile(r"^##\s+(FSI[A-Z]+\d+-BP\d+.*?)\s*$"),
     lambda m: f"#### {m.group(1)}"),
    (re.compile(r"^##\s+(FSI[A-Z]+\d+[A-Z]?:\s+.+?)\s*$"),
     lambda m: f"### {m.group(1)}"),
    (re.compile(
        r"^##\s+(Prescriptive guidance|Implementation guidance|"
        r"Implementation steps|Related documents:?|Related videos:?|"
        r"Related examples:?)\s*$"
    ), lambda m: f"#### {m.group(1)}"),
]


def _is_structural(line: str) -> bool:
    s = line.lstrip()
    return s.startswith(("#", "|", ">", "```", "!["))


# -- Generic transforms ------------------------------------------------------
def strip_toc(lines):
    out = []
    i = 0
    while i < len(lines):
        if TOC_HEADING_RE.match(lines[i]):
            i += 1
            while i < len(lines):
                s = lines[i].strip()
                if s.startswith("|") or s == "":
                    i += 1
                else:
                    break
            continue
        out.append(lines[i])
        i += 1
    return out


def strip_chunk_dividers(lines):
    return [l for l in lines if l.strip() != "---"]


_MD_LINK_NORM_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


# PDF text extraction periodically drops the hyphen in well-known compound
# words ("cloud-based" → "cloudbased") OR injects a stray space mid-word
# ("sustainability" → "sustainab ility").  These dictionaries are the seed
# observed so far; extend as the audit surfaces more variants.
COMPOUND_REJOINS = {
    "antipattern": "anti-pattern",
    "autodiscovery": "auto-discovery",
    "cloudbased": "cloud-based",
    "cloudnative": "cloud-native",
    "costeffective": "cost-effective",
    "crossaccount": "cross-account",
    "crossregion": "cross-region",
    "crosssite": "cross-site",
    "datadriven": "data-driven",
    "decisionmaking": "decision-making",
    "endtoend": "end-to-end",
    "eventdriven": "event-driven",
    "faulttolerant": "fault-tolerant",
    "faulttolerance": "fault-tolerance",
    "finegrained": "fine-grained",
    "highavailability": "high-availability",
    "highperformance": "high-performance",
    "highthroughput": "high-throughput",
    "idempotencyrelated": "idempotency-related",
    "lowlatency": "low-latency",
    "longrunning": "long-running",
    "longterm": "long-term",
    "machinereadable": "machine-readable",
    "multiaccount": "multi-account",
    "multifactor": "multi-factor",
    "multiregion": "multi-region",
    "multistep": "multi-step",
    "multitenant": "multi-tenant",
    "networkbased": "network-based",
    "nonproduction": "non-production",
    "ondemand": "on-demand",
    "onpremises": "on-premises",
    "openssource": "open-source",
    "policybased": "policy-based",
    "realtime": "real-time",
    "rolebased": "role-based",
    "selfservice": "self-service",
    "serviceoriented": "service-oriented",
    "shortterm": "short-term",
    "thirdparty": "third-party",
    "timebased": "time-based",
    "wellarchitected": "well-architected",
    "wellknown": "well-known",
    "writeonce": "write-once",
}
INTRA_WORD_SPACE_FIXES = {
    "architect ural": "architectural",
    "architect ure": "architecture",
    "component s": "components",
    "credentia ls": "credentials",
    "efficient ly": "efficiently",
    "minimizin g": "minimizing",
    "performan ce": "performance",
    "sustainab ility": "sustainability",
    "threat intellige nce": "threat intelligence",
}
_COMPOUND_RE = None  # populated lazily once dicts settle


def _compile_word_re(words):
    if not words:
        return None
    # Word-boundary on both sides, case-insensitive only for ASCII letters.
    return re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b")


def un_glue_compounds(lines):
    """Re-hyphenate compound words that lost their hyphen in PDF extraction.

    Guards against URL contexts: `docs.aws.amazon.com/wellarchitected/...` and
    similar paths legitimately contain lowercase concatenated forms, so any
    match adjacent to `/` or `.` is left alone.
    """
    pat = _compile_word_re(list(COMPOUND_REJOINS))
    if pat is None:
        return lines

    def make_repl(line):
        def repl(m):
            start, end = m.start(), m.end()
            prev = line[start - 1] if start > 0 else ""
            nxt = line[end] if end < len(line) else ""
            if prev in "/\\." or nxt in "/\\.":
                return m.group(0)
            return COMPOUND_REJOINS[m.group(1)]
        return repl

    return [pat.sub(make_repl(l), l) for l in lines]


def heal_intra_word_spaces(lines):
    """Apply a curated dictionary of `wo rd → word` repairs."""
    if not INTRA_WORD_SPACE_FIXES:
        return lines
    # Build a single alternation regex; INTRA_WORD_SPACE_FIXES keys contain
    # a space, so we anchor with word-start at the first half and word-end
    # at the second half.
    pat = re.compile(
        r"\b(" + "|".join(re.escape(k) for k in INTRA_WORD_SPACE_FIXES) + r")\b"
    )
    return [pat.sub(lambda m: INTRA_WORD_SPACE_FIXES[m.group(1)], l)
            for l in lines]


# Docling / Trafilatura output frequently glues markdown link/bold syntax
# onto the surrounding word with no space:
#   text](url)to fetch → text](url) to fetch
#   word[link](url)    → word [link](url)
#   **bold**word       → **bold** word
# The substitutions below add a space at the seam while leaving punctuation
# (.,;:?!) and start-of-line cases alone.
_MD_ADJACENCY_RULES = [
    # Closing ) of a link followed by a letter or backtick
    (re.compile(r"(\]\([^)\s]+\))([A-Za-z`])"), r"\1 \2"),
    # Letter immediately preceding [link]
    (re.compile(r"([A-Za-z,])(\[[^\]]+\]\()"), r"\1 \2"),
    # **bold**word
    (re.compile(r"(\*\*[^*\n]+\*\*)([A-Za-z])"), r"\1 \2"),
    # word**bold**
    (re.compile(r"([A-Za-z])(\*\*[^*\n]+\*\*)"), r"\1 \2"),
]


def space_markdown_adjacency(lines):
    out = []
    for line in lines:
        for pat, repl in _MD_ADJACENCY_RULES:
            line = pat.sub(repl, line)
        out.append(line)
    return out


def normalize_relative_links(lines, base="https://docs.aws.amazon.com/"):
    """Rewrite `[text](href)` where href is a relative path (no scheme) or
    contains Windows backslashes — both happen when Docling pulls a PDF's
    embedded cross-reference URL verbatim. Turn them into absolute URLs so
    the EPUB doesn't end up with "missing target" links.
    """
    def repl(m):
        text, href = m.group(1), m.group(2)
        cleaned = href.replace("\\", "/")
        if "://" in cleaned or cleaned.startswith(("mailto:", "tel:", "#", "/")):
            return f"[{text}]({cleaned})"
        return f"[{text}]({base}{cleaned})"
    return [_MD_LINK_NORM_RE.sub(repl, line) for line in lines]


def strip_orphan_dashes(lines):
    """Remove bare '-' lines (Trafilatura artifact).

    Trafilatura often outputs an isolated '-' between a paragraph and the next
    heading.  When Calibre's markdown parser sees `\\n-\\n##### Heading`, it
    interprets the dash as a setext H2 underline whose content is whatever
    came before, yielding spurious empty `<h2>-</h2>` elements.  Bare dashes
    are never meaningful as standalone content here, so drop them.
    """
    return [l for l in lines if l.strip() != "-"]


def strip_orphan_page_numbers(lines):
    out = []
    for i, line in enumerate(lines):
        if _PAGE_NUM_RE.match(line):
            prev = out[-1].strip() if out else ""
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if prev == "" and nxt == "":
                continue
        out.append(line)
    return out


def consolidate_title(lines, doc_title, title_re=None):
    """Promote the first H2 matching `title_re` (or first H2 overall) to a
    single H1; drop subsequent H2s that match the same pattern.
    """
    title_re = title_re or AWS_TITLE_RE
    out, promoted = [], False
    for line in lines:
        if title_re.match(line):
            if not promoted:
                out.append(f"# {doc_title or line.lstrip('#').strip()}")
                promoted = True
            continue
        out.append(line)
    if not promoted and doc_title:
        out.insert(0, f"# {doc_title}")
        out.insert(1, "")
    return out


def heal_list_gaps(lines):
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        if line.strip() == "" and out:
            prev = out[-1]
            j = i + 1
            while j < len(lines) and lines[j].strip() == "":
                j += 1
            if j < len(lines):
                nxt = lines[j]
                pm, nm = LIST_ITEM_RE.match(prev), LIST_ITEM_RE.match(nxt)
                if pm and nm and pm.group(1) == nm.group(1):
                    i = j
                    continue
        out.append(line)
        i += 1
    return out


_FUNCTION_WORDS = frozenset({
    "and", "or", "to", "the", "a", "an", "of", "in", "by",
    "for", "on", "with", "from", "as", "at",
})
_HYPHEN_SPACE_RE = re.compile(r"\b([A-Za-z]+)-\s+([a-z][a-z]\w*)")


def heal_hyphen_breaks(lines):
    """Rejoin words PDF line wrap split as `prefix- suffix`.

    Skips parallel constructions like "Over- or under-sizing" where the word
    after the hyphen-space is a function word (and/or/to/...).
    """
    def repl(m):
        prefix, suffix = m.group(1), m.group(2)
        if suffix.lower() in _FUNCTION_WORDS:
            return m.group(0)
        return f"{prefix}-{suffix}"

    return [_HYPHEN_SPACE_RE.sub(repl, line) for line in lines]


def heal_broken_sentences(lines):
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        if (line.strip()
                and not _is_structural(line)
                and not line.rstrip().endswith(_SENTENCE_END)
                and i + 1 < len(lines)
                and lines[i + 1].strip() == ""):
            j = i + 1
            while j < len(lines) and lines[j].strip() == "":
                j += 1
            if j < len(lines):
                nxt = lines[j]
                if (nxt and nxt[0].islower()
                        and not _is_structural(nxt)
                        and not LIST_ITEM_RE.match(nxt)):
                    out.append(line.rstrip() + " " + nxt.lstrip())
                    i = j + 1
                    continue
        out.append(line)
        i += 1
    return out


# -- AWS-specific transforms -------------------------------------------------
def promote_pillars_aws(lines):
    out = []
    for line in lines:
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m and m.group(1) in AWS_PILLAR_NAMES:
            out.append(f"# {m.group(1)}")
        else:
            out.append(line)
    return out


def demote_subsections_aws(lines):
    out = []
    for line in lines:
        replaced = False
        for pat, fn in AWS_H2_RULES:
            m = pat.match(line)
            if m:
                out.append(fn(m))
                replaced = True
                break
        if replaced:
            continue
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m and m.group(1) in AWS_SUBSECTION_H3:
            out.append(f"### {m.group(1)}")
        else:
            out.append(line)
    return out


# -- Orchestrator ------------------------------------------------------------
_AWS_CORPUS_FIXES = [
    (re.compile(r"\bWellArchitected\b"), "Well-Architected"),
]

# Markdown→HTML slug derivation produces invalid IDs from headings whose
# text starts with a digit (or only contains punctuation). We rewrite them
# to start with a letter while keeping the original information.
_NUMERIC_HEADING_RE = re.compile(r"^(#+)\s+(\d.*)$")
_EMPTY_HEADING_RE = re.compile(r"^#+\s+[\W_]+\s*$")  # only punctuation/whitespace
_ENUM_RE = re.compile(r"^(\d+)[.\)]\s+(.+)$")        # "1. Foo" or "1) Foo"
_PLAIN_NUM_RE = re.compile(r"^(\d+)\s+(.+)$")        # "1 Foo"


def fix_digit_headings(lines):
    out = []
    for line in lines:
        # Drop empty / punctuation-only headings — they generate IDs like "-"
        # that, with Calibre's auto-disambiguator, become "-_10", "-_11"…
        if _EMPTY_HEADING_RE.match(line):
            continue
        m = _NUMERIC_HEADING_RE.match(line)
        if not m:
            out.append(line)
            continue
        hashes = m.group(1)
        rest = m.group(2).strip().rstrip(":：.")
        em = _ENUM_RE.match(rest)
        if em:
            out.append(f"{hashes} Step {em.group(1)}: {em.group(2)}")
            continue
        pm = _PLAIN_NUM_RE.match(rest)
        if pm:
            out.append(f"{hashes} Step {pm.group(1)}: {pm.group(2)}")
            continue
        # Fallback for things like "24×7 provisioning..." — keep verbatim
        # but inject a letter prefix so the slug starts with one.
        out.append(f"{hashes} Ref. {rest}")
    return out


def fix_digit_headings_text(text: str) -> str:
    """Apply fix_digit_headings to a full markdown string (convenience for
    callers that work with text rather than line lists)."""
    return "\n".join(fix_digit_headings(text.splitlines()))


def apply_corpus_fixes(lines, ruleset):
    if ruleset != "aws":
        return lines
    out = []
    for line in lines:
        for pat, repl in _AWS_CORPUS_FIXES:
            line = pat.sub(repl, line)
        out.append(line)
    return out


def tidy(text: str, *, doc_title: str | None = None, ruleset: str = "aws") -> str:
    lines = text.splitlines()
    lines = strip_toc(lines)
    lines = strip_chunk_dividers(lines)
    lines = strip_orphan_dashes(lines)
    lines = strip_orphan_page_numbers(lines)
    lines = consolidate_title(lines, doc_title,
                              AWS_TITLE_RE if ruleset == "aws" else None)
    if ruleset == "aws":
        lines = promote_pillars_aws(lines)
        lines = demote_subsections_aws(lines)
    lines = heal_list_gaps(lines)
    lines = heal_hyphen_breaks(lines)
    lines = heal_broken_sentences(lines)
    lines = fix_digit_headings(lines)
    lines = un_glue_compounds(lines)
    lines = heal_intra_word_spaces(lines)
    lines = space_markdown_adjacency(lines)
    lines = normalize_relative_links(lines)
    lines = apply_corpus_fixes(lines, ruleset)
    return "\n".join(lines) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(prog="pdf2epub-tidy",
                                description="Clean up Docling markdown.")
    p.add_argument("md_in")
    p.add_argument("md_out")
    p.add_argument("--title", default=None,
                   help="Replace document title with this string")
    p.add_argument("--ruleset", default="aws", choices=["aws", "generic"],
                   help="Domain ruleset (default: aws)")
    args = p.parse_args(argv)

    src = Path(args.md_in)
    dst = Path(args.md_out)
    before = src.read_text(encoding="utf-8").splitlines()
    result = tidy(src.read_text(encoding="utf-8"),
                  doc_title=args.title, ruleset=args.ruleset)
    dst.write_text(result, encoding="utf-8")
    after = result.splitlines()
    print(f"{len(before)} -> {len(after)} lines; wrote {dst}")


if __name__ == "__main__":
    main()
