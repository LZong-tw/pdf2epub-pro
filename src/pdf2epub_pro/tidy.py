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
def tidy(text: str, *, doc_title: str | None = None, ruleset: str = "aws") -> str:
    lines = text.splitlines()
    lines = strip_toc(lines)
    lines = strip_chunk_dividers(lines)
    lines = strip_orphan_page_numbers(lines)
    lines = consolidate_title(lines, doc_title,
                              AWS_TITLE_RE if ruleset == "aws" else None)
    if ruleset == "aws":
        lines = promote_pillars_aws(lines)
        lines = demote_subsections_aws(lines)
    lines = heal_list_gaps(lines)
    lines = heal_broken_sentences(lines)
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
