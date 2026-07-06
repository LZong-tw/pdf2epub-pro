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

from markdown_it import MarkdownIt

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
    # Per-best-practice subsections live BELOW the BP (which we promoted to
    # H4), so they should be H5 — using H4 here creates the H2→H4 jumps the
    # audit complained about.
    (re.compile(
        r"^##\s+(Prescriptive guidance|Implementation guidance|"
        r"Implementation steps|Related documents:?|Related videos:?|"
        r"Related examples:?|Common anti-patterns:?|Benefits|Overview|"
        r"Conclusion|Prerequisites|Additional resources)\s*$"
    ), lambda m: f"##### {m.group(1)}"),
    # Same for the H4 form that some passes already produced.
    (re.compile(
        r"^####\s+(Prescriptive guidance|Implementation guidance|"
        r"Implementation steps|Related documents:?|Related videos:?|"
        r"Related examples:?|Common anti-patterns:?|Benefits|Overview|"
        r"Conclusion|Prerequisites|Additional resources)\s*$"
    ), lambda m: f"##### {m.group(1)}"),
]


# AWS WAF questions frequently use lettered sub-bullets ("- a.", "- b.")
# that PDF extraction flattens to column 0 instead of nesting them.
_LETTERED_SUBLIST_RE = re.compile(r"^- ([a-z])\.\s+(.+)$")


def indent_lettered_sublists(lines):
    """Indent `- a.` / `- b.` … bullets one nesting level so they read as
    sub-items of the preceding numbered list / paragraph, rather than as a
    parallel top-level list."""
    return [_LETTERED_SUBLIST_RE.sub(r"  - \1. \2", l) for l in lines]


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


# Use negative lookbehind to exclude image syntax `![alt](src)` — image
# refs are local artifact paths that must NOT get a URL base prepended.
# Allow a single level of nested `[...]` in the link text (e.g. control
# IDs like '[CloudTrail.1] CloudTrail …').
_MD_LINK_NORM_RE = re.compile(
    r"(?<!!)\[((?:[^\[\]]|\[[^\]]*\])+)\]\(([^)\s]+)\)"
)


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


# Docling / Trafilatura output frequently emits emphasis markers that
# python-markdown's CommonMark-leaning parser refuses to recognize because
# of CommonMark's "no flanking whitespace" rule on emphasis runs:
#
#     **CloudWatch ** is used        → the closing `**` has leading space,
#                                       so the run doesn't close here and
#                                       the bold extends into the next
#                                       paragraph, eating prose alive.
#     ** AWS Glue **                 → both sides padded; not recognized.
#     __ AWS KMS __                  → same problem with underscores.
#
# We use markdown-it-py (a CommonMark parser with the same flanking
# semantics as python-markdown) to VALIDATE every candidate fix:
#
#   1.  Find each `**X**` / `__X__` candidate via regex over the text.
#   2.  Parse the original substring — count `strong_open` tokens.
#   3.  Parse the inner-stripped form — count `strong_open` tokens.
#   4.  Replace iff the stripped form produces *more* strong tokens than
#       the original.  This guarantees we never replace a span that was
#       already parsing correctly, and never replace with a form that
#       still doesn't parse.
#
# This is "regex finds candidates; the parser is the oracle on whether
# the fix actually fixes anything."  Strictly safer than the pure-regex
# version: if markdown-it-py says the input already parses as strong,
# we leave it alone; if neither the input nor the stripped form parses,
# we still leave it alone (rather than silently mis-edit).
_MD_PARSER = MarkdownIt("commonmark", {"breaks": False, "html": False})

_EMPHASIS_CANDIDATE_RE = re.compile(r"(\*\*|__)([^\n]+?)\1")


def _count_strong(text: str) -> int:
    n = 0
    for tok in _MD_PARSER.parse(text):
        if tok.type == "inline" and tok.children:
            n += sum(1 for c in tok.children if c.type == "strong_open")
    return n


def strip_emphasis_inner_space(lines):
    """Normalize whitespace-padded **X** / __X__ runs that fail to tokenize
    as emphasis under CommonMark.  See module-level comment for the
    parser-validated algorithm.
    """
    def repl(m):
        marker, inner = m.group(1), m.group(2)
        stripped = inner.strip()
        if not stripped or stripped == inner:
            return m.group(0)
        candidate = f"{marker}{stripped}{marker}"
        if _count_strong(candidate) > _count_strong(m.group(0)):
            return candidate
        return m.group(0)

    return [_EMPHASIS_CANDIDATE_RE.sub(repl, line) for line in lines]


# Docling / Trafilatura also glues emphasis and link syntax onto the
# surrounding word with no space:
#   text](url)to fetch → text](url) to fetch
#   word[link](url)    → word [link](url)
#   **bold**word       → **bold** word
#   word__bold__       → word __bold__
# Each emphasis-aware rule requires the INNER content to start and end
# with a non-whitespace character (`\S`).  This matters when a paragraph
# contains multiple emphasis runs in series:
#
#     **A**is used and **B** and **C** workflows
#                          ^^^^^^^^^^^^
#       a regex of the form `\*\*[^*\n]+\*\*` greedily pairs the closing
#       `**` of one run with the opening `**` of the next, treats the
#       ` and ` in between as bold content, and "fixes" the seam — which
#       both invents fake bold and immediately mis-pairs the markers,
#       re-introducing the whitespace-padded shape CommonMark refuses
#       to recognize.  The `\S … \S` boundary forces real well-formed
#       runs only.
_MD_ADJACENCY_RULES = [
    # Closing ) of a link followed by a letter or backtick
    (re.compile(r"(\]\([^)\s]+\))([A-Za-z`])"), r"\1 \2"),
    # Letter immediately preceding [link]
    (re.compile(r"([A-Za-z,])(\[[^\]]+\]\()"), r"\1 \2"),
    # **bold**word and word**bold**
    (re.compile(r"(\*\*\S(?:[^*\n]*?\S)?\*\*)([A-Za-z])"), r"\1 \2"),
    (re.compile(r"([A-Za-z])(\*\*\S(?:[^*\n]*?\S)?\*\*)"), r"\1 \2"),
    # __bold__word and word__bold__ (symmetric with ** above)
    (re.compile(r"(__\S(?:[^_\n]*?\S)?__)([A-Za-z])"), r"\1 \2"),
    (re.compile(r"([A-Za-z])(__\S(?:[^_\n]*?\S)?__)"), r"\1 \2"),
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
        # Strip leading './' so we don't end up with 'base/./foo.html'.
        while cleaned.startswith("./"):
            cleaned = cleaned[2:]
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


def fix_digit_headings(lines, ruleset="aws"):
    out = []
    for line in lines:
        # Drop empty / punctuation-only headings — they generate IDs like "-"
        # that, with Calibre's auto-disambiguator, become "-_10", "-_11"…
        if _EMPTY_HEADING_RE.match(line):
            continue
        m = _NUMERIC_HEADING_RE.match(line)
        # Rewriting a numbered heading's *visible text* into "Step N:" /
        # "Ref." prose is an AWS-runbook nicety, not a generic slug fix:
        # numbered section headings ("## 1.1 Foo", "## 01") are the norm in
        # ordinary books/standards, and the markdown->HTML synthesizer's
        # auto-identifier already derives a valid slug by stripping the
        # leading digits (pandoc's auto_identifiers + ascii_identifiers). So
        # only the aws ruleset opts into the prose rewrite; every other
        # ruleset keeps the heading verbatim.
        if not m or ruleset != "aws":
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
        lines = indent_lettered_sublists(lines)
    lines = heal_list_gaps(lines)
    lines = heal_hyphen_breaks(lines)
    lines = heal_broken_sentences(lines)
    lines = fix_digit_headings(lines, ruleset)
    lines = un_glue_compounds(lines)
    lines = heal_intra_word_spaces(lines)
    # Strip inner whitespace from emphasis runs BEFORE the adjacency pass,
    # so the adjacency rules see well-formed `**X**` / `__X__` tokens.
    lines = strip_emphasis_inner_space(lines)
    lines = space_markdown_adjacency(lines)
    # Belt-and-suspenders: re-run the emphasis cleanup after adjacency, in
    # case a regex rule accidentally produced a whitespace-padded shape
    # (it shouldn't with the `\S … \S` boundary, but this is cheap insurance).
    lines = strip_emphasis_inner_space(lines)
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
