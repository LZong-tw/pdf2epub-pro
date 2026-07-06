"""Unit tests for pdf2epub_pro.tidy — the heart of the cleanup pipeline."""
from pdf2epub_pro.tidy import (
    apply_corpus_fixes,
    consolidate_title,
    demote_subsections_aws,
    fix_digit_headings,
    fix_digit_headings_text,
    heal_broken_sentences,
    heal_hyphen_breaks,
    heal_intra_word_spaces,
    heal_list_gaps,
    indent_lettered_sublists,
    normalize_relative_links,
    promote_numbered_chapters,
    promote_pillars_aws,
    space_markdown_adjacency,
    strip_chunk_dividers,
    strip_emphasis_inner_space,
    strip_orphan_dashes,
    escape_prose_dollars,
    wrap_aligned_math,
    strip_placeholder_image_alt,
    strip_orphan_page_numbers,
    strip_toc,
    tidy,
    un_glue_compounds,
)


# ---------------------------------------------------------------------- strip


def test_strip_toc_removes_dotted_leader_table():
    src = [
        "Intro paragraph.",
        "",
        "## Table of Contents",
        "",
        "| Section A ............ | 1 |",
        "| Section B ............ | 2 |",
        "",
        "## First real heading",
    ]
    out = strip_toc(src)
    assert "## Table of Contents" not in out
    assert "| Section A ............ | 1 |" not in out
    assert "## First real heading" in out


def test_strip_toc_contents_heading_with_table():
    # REGRESSION: numbered books emit their in-book TOC as "## CONTENTS"
    # followed by a dotted-leader table; the old regex only knew
    # "Table of Contents", so the junk table shipped into the EPUB.
    src = [
        "## CONTENTS",
        "",
        "| Preface | Preface | xi |",
        "| 1 Introduction | 1 Introduction | 1 |",
        "",
        "## PREFACE",
        "Prose.",
    ]
    out = strip_toc(src)
    assert "## CONTENTS" not in out
    assert not any(l.startswith("|") for l in out)
    assert "## PREFACE" in out


def test_strip_toc_survives_page_number_interruptions():
    # REGRESSION: docling page breaks drop orphan page-number lines into
    # the middle of the TOC table; strip_toc stopped there and shipped
    # the second half of the table (124 dotted-leader rows) into the EPUB.
    src = [
        "## CONTENTS",
        "",
        "| 1.1 | Foo . . . . . 3 |",
        " 5",
        "| 4.4 | Multicast communication . . . . . 158 |",
        "",
        "## PREFACE",
        "Prose.",
    ]
    out = strip_toc(src)
    assert not any(l.lstrip().startswith("|") for l in out)
    assert "## PREFACE" in out


def test_strip_placeholder_image_alt():
    # REGRESSION: docling stamps every extracted image with alt text
    # "Image"; pandoc's implicit_figures turned that into a visible
    # "Image" figcaption under all 249 diagrams of a book.
    src = [
        "![Image](art/diagram.png)",
        "![ image ](x.png) and ![Image](y.png)",
    ]
    out = strip_placeholder_image_alt(src)
    assert out[0] == "![](art/diagram.png)"
    assert out[1] == "![](x.png) and ![](y.png)"


def test_strip_placeholder_image_alt_keeps_real_captions():
    src = ["![Figure 4.2: RPC flow](art/rpc.png)"]
    assert strip_placeholder_image_alt(src) == src


def test_strip_placeholder_image_alt_skips_fences():
    src = ["```", "![Image](literal.png)", "```"]
    assert strip_placeholder_image_alt(src) == src


def test_strip_toc_keeps_prose_contents_section():
    # A section that happens to be titled "Contents" but is followed by
    # prose (not a table) is real content and must survive.
    src = ["## Contents", "", "This chapter covers packaging."]
    out = strip_toc(src)
    assert "## Contents" in out
    assert "This chapter covers packaging." in out


def test_strip_chunk_dividers_drops_horizontal_rule_lines():
    assert strip_chunk_dividers(["a", "---", "b", "  ---  ", "c"]) == ["a", "b", "c"]


def test_strip_orphan_dashes_drops_bare_dash_lines():
    assert strip_orphan_dashes(["foo", "-", "bar"]) == ["foo", "bar"]
    # don't touch legit bullet items
    assert strip_orphan_dashes(["- a bullet"]) == ["- a bullet"]


def test_strip_orphan_page_numbers_removes_isolated_numbers():
    src = ["End of page text.", "", "15", "", "Next page starts."]
    out = strip_orphan_page_numbers(src)
    assert "15" not in out
    # multi-digit content lines should remain
    src2 = ["Step 15 in the process", "is required."]
    assert strip_orphan_page_numbers(src2) == src2


# --------------------------------------------------------------------- titles


def test_consolidate_title_promotes_first_match():
    src = [
        "## AWS Well-Architected Framework — Foo",
        "Some intro.",
        "## AWS Well-Architected Framework — Foo",
    ]
    out = consolidate_title(src, doc_title="Real Title")
    # First H2 became H1 with the supplied title; the duplicate dropped.
    assert out[0] == "# Real Title"
    assert out.count("# Real Title") == 1
    assert "## AWS Well-Architected Framework — Foo" not in out


# ---------------------------------------------------------------------- aws


def test_promote_pillars_to_h1():
    src = ["## Security", "## Reliability", "## Some other section"]
    out = promote_pillars_aws(src)
    assert "# Security" in out
    assert "# Reliability" in out
    # unknown H2 stays H2
    assert "## Some other section" in out


def test_demote_subsections_aws_handles_bullets_and_fsi():
    src = [
        "## · Compliance",
        "## FSIOPS01: Have you defined risk roles?",
        "## FSIOPS01-BP01 Define roles",
        "## Prescriptive guidance",
        "## Reference architecture",
    ]
    out = demote_subsections_aws(src)
    # bullet-as-H2 -> bullet
    assert out[0] == "- **Compliance**"
    # question -> H3
    assert out[1] == "### FSIOPS01: Have you defined risk roles?"
    # BP -> H4
    assert out[2] == "#### FSIOPS01-BP01 Define roles"
    # per-BP labels -> H5
    assert out[3] == "##### Prescriptive guidance"
    # subsection name -> H3
    assert out[4] == "### Reference architecture"


def test_apply_corpus_fixes_well_architected():
    src = ["The WellArchitected Framework defines …"]
    out = apply_corpus_fixes(src, ruleset="aws")
    assert out[0] == "The Well-Architected Framework defines …"


# ------------------------------------------------------------------- compounds


def test_un_glue_compounds_basic():
    src = [
        "Use realtime monitoring with thirdparty tools and finegrained access.",
        "Apply costeffective and faulttolerant patterns.",
    ]
    out = un_glue_compounds(src)
    joined = " ".join(out)
    assert "real-time" in joined
    assert "third-party" in joined
    assert "fine-grained" in joined
    assert "cost-effective" in joined
    assert "fault-tolerant" in joined


def test_un_glue_compounds_skips_url_paths():
    # `wellarchitected` is the official AWS docs URL path component and MUST
    # not get a hyphen injected.
    src = [
        "See https://docs.aws.amazon.com/wellarchitected/latest/foo.html for more.",
        "Visit aws.amazon.com/wellarchitected/ for the framework.",
        "The wellarchitected approach is core to AWS.",
    ]
    out = un_glue_compounds(src)
    # First two lines: URL paths preserved verbatim.
    assert "/wellarchitected/" in out[0]
    assert "/wellarchitected/" in out[1]
    # Third line: prose form correctly hyphenated.
    assert "well-architected approach" in out[2]


def test_heal_intra_word_spaces():
    src = ["This is a sustainab ility concern with component s."]
    out = heal_intra_word_spaces(src)
    assert "sustainability" in out[0]
    assert "components" in out[0]


# ------------------------------------------------------------------- adjacency


def test_space_markdown_adjacency_link_seam():
    src = ["See[the docs](https://example.com/foo)for more."]
    out = space_markdown_adjacency(src)
    assert out[0] == "See [the docs](https://example.com/foo) for more."


def test_space_markdown_adjacency_bold_seam():
    # Test each direction in isolation so adjacent overlapping matches
    # don't interfere.
    after_bold = space_markdown_adjacency(["use **bold**next"])
    assert after_bold[0] == "use **bold** next"
    before_bold = space_markdown_adjacency(["pre**bold** suffix"])
    assert before_bold[0] == "pre **bold** suffix"


def test_space_markdown_adjacency_underscore_bold_seam():
    # REGRESSION: __X__ form was missing from adjacency rules, so
    # `__AWS Key Management Service__The customer` ate the next word
    # into the bold run.
    after = space_markdown_adjacency(["use __KMS__then"])
    assert after[0] == "use __KMS__ then"
    before = space_markdown_adjacency(["pre__KMS__ suffix"])
    assert before[0] == "pre __KMS__ suffix"


def test_space_markdown_adjacency_does_not_pair_across_bolds():
    # REGRESSION: a greedy `\*\*[^*\n]+\*\*` regex paired the CLOSE `**` of
    # one bold with the OPEN `**` of the next, treating ` and ` between
    # them as bold content and inserting a fake seam.  The `\S … \S`
    # boundary on the inner pattern rejects that, leaving real adjacent
    # bolds alone.  Symptom this protects against:
    #     '**A**is, **B** and **C**' → '**A ** is, **B ** and ** C**'
    src = ["**CloudWatch**is used, **AWS Glue** and **Step Functions** workflows."]
    out = space_markdown_adjacency(src)
    # Only the **CloudWatch**is seam should change (insert one space).
    expected = "**CloudWatch** is used, **AWS Glue** and **Step Functions** workflows."
    assert out[0] == expected


# ----------------------------------------------------------- inner-space


def test_strip_emphasis_inner_space_bold_asterisk():
    # `**bold **` and `** bold **` and `** bold**` all unbalanced —
    # CommonMark refuses to close them, the bold run runs forever.
    src = [
        "Use **CloudWatch ** to measure ML ops metrics.",
        "Pair ** AWS Glue ** with ** Step Functions ** workflows.",
        "And ** Lambda** for serverless triggers.",
    ]
    out = strip_emphasis_inner_space(src)
    assert out[0] == "Use **CloudWatch** to measure ML ops metrics."
    assert out[1] == "Pair **AWS Glue** with **Step Functions** workflows."
    assert out[2] == "And **Lambda** for serverless triggers."


def test_strip_emphasis_inner_space_bold_underscore():
    src = ["Replace __ AWS KMS __ with the actual ARN."]
    out = strip_emphasis_inner_space(src)
    assert out[0] == "Replace __AWS KMS__ with the actual ARN."


def test_strip_emphasis_inner_space_leaves_clean_bold_alone():
    src = ["Use **CloudWatch** for monitoring; __KMS__ for encryption."]
    out = strip_emphasis_inner_space(src)
    assert out == src


# ------------------------------------------------------------------ link norm


def test_normalize_relative_links_adds_aws_base():
    src = ["[OPS11-BP01 ...](wellarchitected\\latest\\op\\foo.html)"]
    out = normalize_relative_links(src)
    assert out[0] == (
        "[OPS11-BP01 ...](https://docs.aws.amazon.com/"
        "wellarchitected/latest/op/foo.html)"
    )


def test_normalize_relative_links_passes_absolute_through():
    src = ["[Doc](https://aws.amazon.com/foo) and [mail](mailto:x@y.com)"]
    out = normalize_relative_links(src)
    assert out == src


def test_normalize_relative_links_handles_nested_brackets_in_link_text():
    """REGRESSION: AWS docs embed control IDs like '[CloudTrail.1] X' as the
    visible label of an outer markdown link. A naive `[^\\]]+` link-text
    regex bails on the inner ']' and leaves 335+ relative './foo.html'
    URLs unresolved in the EPUB.
    """
    src = [
        "See [[CloudTrail.1] CloudTrail should be enabled](./cloudtrail-controls.html#cloudtrail-1) for details.",
    ]
    out = normalize_relative_links(src)
    assert (
        "[[CloudTrail.1] CloudTrail should be enabled]"
        "(https://docs.aws.amazon.com/cloudtrail-controls.html#cloudtrail-1)"
        in out[0]
    )


def test_normalize_relative_links_preserves_image_syntax():
    """REGRESSION: image refs ![alt](src) must NOT get a URL base
    prepended — they're local relative paths to artifact PNGs in the EPUB
    package.  Adding 'https://docs.aws.amazon.com/' breaks the resolver
    and the entire book ends up with no diagrams."""
    src = [
        "![Image](AWS-WAF-FS-Lens_artifacts/c0001_image_000000.png)",
        "Inline ![diagram](local/foo.png) here.",
    ]
    out = normalize_relative_links(src)
    assert out == src


# --------------------------------------------------------------- list gaps


def test_heal_list_gaps_merges_same_indent_bullets():
    src = ["- alpha", "", "- bravo", "", "  - nested"]
    out = heal_list_gaps(src)
    # The blank between "- alpha" and "- bravo" is removed.
    assert out == ["- alpha", "- bravo", "", "  - nested"]


def test_heal_broken_sentences_joins_mid_sentence():
    src = [
        "1. Model Build Workflow: trains using SageMaker, then the model's",
        "",
        "performance is evaluated against test data.",
    ]
    out = heal_broken_sentences(src)
    # The two halves are now joined on one line.
    joined = "\n".join(out)
    assert "the model's performance is evaluated" in joined


def test_heal_hyphen_breaks_rejoins_line_wrap_artifacts():
    src = ["machine- learning powered recommendations"]
    out = heal_hyphen_breaks(src)
    assert out[0] == "machine-learning powered recommendations"


def test_heal_hyphen_breaks_leaves_parallel_constructions_alone():
    # "Over- or under-sizing" is intentional; don't reglue.
    src = ["Avoid over- or under-sizing your CIDR blocks."]
    out = heal_hyphen_breaks(src)
    assert out == src


# -------------------------------------------------------------- digit heading


def test_fix_digit_headings_step_pattern():
    src = ["## 1. Identify foo", "## 2 Track state"]
    out = fix_digit_headings(src)
    assert out[0] == "## Step 1: Identify foo"
    assert out[1] == "## Step 2: Track state"


def test_fix_digit_headings_fallback_for_nonperiod():
    # "24×7 ..." doesn't match the period/space patterns, so falls back to
    # "Ref." prefix so the slug starts with a letter.
    src = ["##### 24×7 provisioning"]
    out = fix_digit_headings(src)
    assert out[0].startswith("##### Ref. 24×7")


def test_fix_digit_headings_drops_punctuation_only():
    # "## -" — Calibre would emit '<h2 id="-">-</h2>'; tidy must drop it.
    src = ["## -", "## --", "## Real heading"]
    out = fix_digit_headings(src)
    assert "## -" not in out
    assert "## --" not in out
    assert "## Real heading" in out


def test_fix_digit_headings_generic_preserves_numbered_sections():
    # REGRESSION: converting a numbered book with ruleset=generic must NOT
    # rewrite "## 1.1 Foo" into "## Ref. 1.1 Foo".  Shipped that way, every
    # TOC entry in a 685-page textbook read "Ref. N.M ..." (the aws-runbook
    # "Step/Ref." heading rewrite leaked into the generic default).  Dotted
    # sections, bare chapter numbers, single enumerators and digit-symbol
    # headings all pass through verbatim.
    src = [
        "## 01",
        "## 1.1 From networked systems to distributed systems",
        "## 1.1.1 Distributed versus decentralized systems",
        "## 1. Identify foo",
        "##### 24x7 provisioning",
    ]
    out = fix_digit_headings(src, ruleset="generic")
    assert out == src


def test_fix_digit_headings_generic_still_drops_punctuation_only():
    # The punctuation-only drop is a real slug bug, not an aws nicety --
    # it must fire for every ruleset.
    src = ["## -", "## 1.1 Real section"]
    out = fix_digit_headings(src, ruleset="generic")
    assert "## -" not in out
    assert "## 1.1 Real section" in out


def test_tidy_generic_keeps_numbered_section_headings():
    # REGRESSION: end-to-end guard on the generic pipeline -- a numbered
    # section heading survives tidy() untouched (no "Ref." prefix).
    src = "## 1.1 From networked systems\n\nbody text.\n"
    out = tidy(src, ruleset="generic")
    assert "## 1.1 From networked systems" in out.splitlines()
    assert "Ref. 1.1" not in out


# ------------------------------------------------- numbered chapter hierarchy
# REGRESSION: a 685-page numbered textbook shipped as ONE 10k-line
# ch001.xhtml with a flat 440-entry TOC.  ML parsers flatten every
# chapter/section/subsection heading to H2; nothing rebuilt the hierarchy,
# so pandoc (--split-level=1) saw a single H1 and never split the book.


def test_promote_numbered_chapters_merges_number_and_title():
    src = [
        "## 01", "", "## INTRODUCTION", "",
        "## 1.1 Foundations", "## 1.2 Goals",
        "## 02", "", "## ARCHITECTURES", "",
        "## 2.1 Styles", "## 2.2 Middleware",
    ]
    out = promote_numbered_chapters(src)
    assert "# 1 INTRODUCTION" in out
    assert "# 2 ARCHITECTURES" in out
    assert "## 01" not in out
    assert "## INTRODUCTION" not in out
    assert "## 1.1 Foundations" in out  # single dot stays H2


def test_promote_numbered_chapters_merges_split_caps_title():
    # The parser can capture the first title word INTO the number heading:
    # "## 08 FAULT" + "## TOLERANCE" is one chapter called FAULT TOLERANCE.
    src = [
        "## 7.1 Consistency", "## 7.2 Replication",
        "## 08 FAULT", "", "## TOLERANCE",
        "## 8.1 Introduction", "## 8.2 Resilience",
    ]
    out = promote_numbered_chapters(src)
    assert "# 8 FAULT TOLERANCE" in out
    assert "## TOLERANCE" not in out


def test_promote_numbered_chapters_inline_number_and_title():
    src = [
        "## 8.1 A", "## 8.2 B",
        "## 09 SECURITY",
        "## 9.1 Intro", "## 9.2 Channels",
    ]
    out = promote_numbered_chapters(src)
    assert "# 9 SECURITY" in out


def test_promote_numbered_chapters_allcaps_boundary_gets_number():
    # A chapter whose number heading the parser dropped entirely: the
    # all-caps title sits between chapter 3's and chapter 4's sections,
    # so the numbering tells us it is chapter 4.
    src = [
        "## 3.5 Migration", "## 3.6 Summary",
        "## COMMUNICATION",
        "## 4.1 Foundations", "## 4.2 RPC",
    ]
    out = promote_numbered_chapters(src)
    assert "# 4 COMMUNICATION" in out


def test_promote_numbered_chapters_allcaps_inside_chapter_untouched():
    # All-caps headings BETWEEN sections of the same chapter (acronym
    # sub-heads etc.) must not be mistaken for chapter titles.
    src = [
        "## 3.6 Summary",
        "## 4.1 Foundations", "## HTTP", "## 4.2 RPC", "## 4.3 MOM",
    ]
    out = promote_numbered_chapters(src)
    assert "## HTTP" in out


def test_promote_numbered_chapters_only_last_allcaps_wins_boundary():
    # Trailing all-caps matter of the previous chapter (exercises pages
    # etc.) sits in the same boundary window as the real chapter title;
    # only the LAST all-caps heading before the next numbered section is
    # the title.
    src = [
        "## 3.5 M", "## 3.6 Summary",
        "## EXERCISES", "## COMMUNICATION",
        "## 4.1 Foundations", "## 4.2 RPC",
    ]
    out = promote_numbered_chapters(src)
    assert "# 4 COMMUNICATION" in out
    assert "## EXERCISES" in out


def test_promote_numbered_chapters_front_matter_all_promoted():
    # REGRESSION: the last-in-window guard saw chapter 1's title heading
    # ("## INTRODUCTION", half of a number+title merge) and refused to
    # promote PREFACE, leaving front matter nested under the book title.
    src = [
        "## FOREWORD", "text",
        "## PREFACE", "text",
        "## 01", "", "## INTRODUCTION",
        "## 1.1 A", "## 1.2 B", "## 2.1 C", "## 2.2 D",
    ]
    out = promote_numbered_chapters(src)
    assert "# FOREWORD" in out
    assert "# PREFACE" in out
    assert "# 1 INTRODUCTION" in out


def test_promote_numbered_chapters_back_matter_promoted_unnumbered():
    src = [
        "## 8.1 A", "## 8.2 B", "## 9.1 C", "## 9.7 Summary",
        "## INDEX", "index body", "## BIBLIOGRAPHY", "bib body",
    ]
    out = promote_numbered_chapters(src)
    assert "# INDEX" in out
    assert "# BIBLIOGRAPHY" in out


def test_promote_numbered_chapters_dotted_depth():
    src = [
        "## 1.1 Sharing", "## 1.1.1 Deep",
        "## 1.2 Goals", "## 2.1 Styles",
    ]
    out = promote_numbered_chapters(src)
    assert "### 1.1.1 Deep" in out
    assert "## 1.1 Sharing" in out


def test_promote_numbered_chapters_demotes_unnumbered_subsections():
    # An unnumbered heading inside a chapter is a sub-topic of the
    # numbered section above it, one level deeper.
    src = [
        "## 1.1 Sharing", "## 1.2.2 Transparency",
        "## Types of transparency",
        "## 1.2.3 Openness", "## 2.1 Styles",
    ]
    out = promote_numbered_chapters(src)
    assert "### 1.2.2 Transparency" in out
    assert "#### Types of transparency" in out


def test_promote_numbered_chapters_ignores_unbacked_numbers():
    # A bare number heading whose value never appears as a section major
    # is junk, not a chapter.
    src = [
        "## 1.1 A", "## 1.2 B", "## 2.1 C", "## 2.2 D",
        "## 42",
    ]
    out = promote_numbered_chapters(src)
    assert "## 42" in out


def test_promote_numbered_chapters_skips_fenced_code():
    # Known-bugs list: heading logic must never be fence-blind — a code
    # listing's comments must not become chapters.
    src = [
        "## 1.1 A", "## 1.2 B", "## 2.1 C", "## 2.2 D",
        "```",
        "## 02",
        "## FAKE CHAPTER",
        "# 1.1 comment",
        "```",
    ]
    out = promote_numbered_chapters(src)
    assert "## 02" in out
    assert "## FAKE CHAPTER" in out
    assert "# 1.1 comment" in out


def test_promote_numbered_chapters_noop_without_numbering():
    # Gate: documents without a numbered-section signature pass through
    # completely untouched.
    src = ["## Intro", "## Setup", "## 1.1 Lone numbered"]
    assert promote_numbered_chapters(src) == src


def test_promote_numbered_chapters_drops_duplicate_title_headings():
    # The book title repeated as a heading on the cover pages must not
    # become a bogus chapter.
    src = [
        "# Distributed Systems", "",
        "## DISTRIBUTED SYSTEMS", "",
        "## 1.1 A", "## 1.2 B", "## 2.1 C", "## 2.2 D",
    ]
    out = promote_numbered_chapters(src, doc_title="Distributed Systems")
    assert "## DISTRIBUTED SYSTEMS" not in out
    assert "# DISTRIBUTED SYSTEMS" not in out


def test_fix_digit_headings_text_generic_passthrough():
    # fetch_refs post-processes the whole document through
    # fix_digit_headings_text; with ruleset=generic it must not undo the
    # hierarchy pass ("# 1 INTRODUCTION" -> "# Step 1: INTRODUCTION").
    text = "# 1 INTRODUCTION\n## 1.1 Foo"
    assert fix_digit_headings_text(text, "generic") == text


def test_tidy_generic_rebuilds_numbered_chapter_hierarchy():
    src = "\n".join([
        "## 01", "", "## INTRODUCTION", "",
        "## 1.1 From networked systems", "", "Body.", "",
        "## 1.1.1 Distributed versus decentralized", "", "Body.", "",
        "## 1.2 Design goals", "", "Body.", "",
        "## 02", "", "## ARCHITECTURES", "",
        "## 2.1 Architectural styles", "", "Body.", "",
        "## 2.2 Middleware", "", "Body.", "",
    ])
    out = tidy(src, doc_title="My Book", ruleset="generic").splitlines()
    assert "# 1 INTRODUCTION" in out
    assert "# 2 ARCHITECTURES" in out
    assert "## 1.1 From networked systems" in out
    assert "### 1.1.1 Distributed versus decentralized" in out


# ----------------------------------------------------------- lettered sublist


def test_indent_lettered_sublists():
    src = [
        "3. Once you've generated a list...",
        "- a. Use AWS Config rules",
        "- b. If you use AWS Organizations",
        "- 4. unrelated digit bullet stays",
    ]
    out = indent_lettered_sublists(src)
    assert out[1] == "  - a. Use AWS Config rules"
    assert out[2] == "  - b. If you use AWS Organizations"
    assert out[3] == "- 4. unrelated digit bullet stays"


# ----------------------------------------------------------------- tidy()


def test_tidy_end_to_end_aws_pipeline():
    # Each line that could be merged by heal_broken_sentences ends with a
    # sentence terminator so the assertion targets stay stable.
    src = "\n".join([
        "## Table of Contents",
        "| Foo .... | 1 |",
        "",
        "## AWS Well-Architected Framework",
        "",
        "Some intro about the WellArchitected approach.",
        "",
        "## Security",
        "",
        "## 1. Identify roles",
        "",
        "- a. Use IAM.",
        "",
        "Machine- learning is great.",
        "",
        "See[the doc](wellarchitected\\latest\\foo.html)now.",
        "",
        "![Image](local_artifacts/diagram.png)",
        "",
    ])
    out = tidy(src, doc_title="My Book", ruleset="aws")
    lines = out.splitlines()
    assert "# My Book" in lines  # title consolidated
    assert "# Security" in lines  # pillar promoted
    assert any("## Step 1: Identify roles" == l for l in lines)
    assert any(l.startswith("  - a. Use IAM") for l in lines)
    assert any("machine-learning" in l.lower() for l in lines)
    assert any("Well-Architected" in l for l in lines)
    assert any(
        "[the doc](https://docs.aws.amazon.com/wellarchitected/latest/foo.html)"
        in l for l in lines
    )
    # IMAGE PATH not rewritten — most important regression target.
    # (The placeholder alt text is stripped by strip_placeholder_image_alt,
    # but the local path must never gain a URL base.)
    assert any(
        "![](local_artifacts/diagram.png)" in l for l in lines
    )


# ------------------------------------------------------- math dollar escaping
def test_escape_prose_dollars_protects_display_math():
    # REGRESSION (adversarial verify): with --math, pandoc's
    # tex_math_dollars swallowed the prose between a price '$5' and a
    # later 'done$', deleting the words.  Escape prose '$', keep $$math$$.
    src = [
        r"A node costs $5 but the pipeline uses a done$ sentinel token.",
        r"$$n = -\frac{m}{k}\ln(1-x)$$",
    ]
    out = escape_prose_dollars(src)
    assert out[0] == r"A node costs \$5 but the pipeline uses a done\$ sentinel token."
    assert out[1] == r"$$n = -\frac{m}{k}\ln(1-x)$$"  # display math untouched


def test_escape_prose_dollars_keeps_code_spans():
    src = ["Set " + chr(96) + "$PATH" + chr(96) + " and pay $5."]
    out = escape_prose_dollars(src)
    assert out[0] == "Set " + chr(96) + "$PATH" + chr(96) + " and pay \\$5."


def test_escape_prose_dollars_skips_fenced_code():
    src = [chr(96)*3, "export PS1='$ '", "echo $HOME", chr(96)*3, "Prose $5."]
    out = escape_prose_dollars(src)
    assert out[1] == "export PS1='$ '"      # fenced code verbatim
    assert out[2] == "echo $HOME"
    assert out[4] == "Prose \\$5."           # prose still escaped


def test_escape_prose_dollars_idempotent_on_escaped():
    src = [r"already \$escaped"]
    assert escape_prose_dollars(src) == src


def test_tidy_math_escapes_prose_dollars():
    src = "A node costs $5 but a done$ sentinel.\n\n$$x^2$$\n"
    out = tidy(src, ruleset="generic", math=True)
    assert r"\$5" in out and r"done\$" in out
    assert "$$x^2$$" in out


def test_tidy_without_math_leaves_dollars_literal():
    src = "A node costs $5 but a done$ sentinel.\n"
    out = tidy(src, ruleset="generic", math=False)
    assert "$5" in out and "done$" in out
    assert "\\$" not in out



# ------------------------------------------------- aligned-equation wrapping
def test_wrap_aligned_math_wraps_bare_alignment():
    # REGRESSION: docling emits multi-line derivations with bare & and \\;
    # pandoc rejected them and dumped raw TeX.  Wrap in aligned -> MathML.
    src = [r"$$\overline{N} & = \sum_k k p_k \\ & = \frac{U}{1-U}$$"]
    out = wrap_aligned_math(src)
    assert out[0] == (
        r"$$\begin{aligned}\overline{N} & = \sum_k k p_k \\ "
        r"& = \frac{U}{1-U}\end{aligned}$$"
    )


def test_wrap_aligned_math_leaves_plain_formula():
    src = [r"$$n = -\frac{m}{k}\ln(1-x)$$"]
    assert wrap_aligned_math(src) == src


def test_wrap_aligned_math_skips_existing_environment():
    # Already carries its own environment -> never double-wrap.
    src = [r"$$v \leftarrow \begin{cases} 1 & a \\ 0 & b \end{cases}$$"]
    assert wrap_aligned_math(src) == src


def test_wrap_aligned_math_skips_fenced_code():
    src = [chr(96) * 3, r"echo $$a & b$$", chr(96) * 3]
    assert wrap_aligned_math(src) == src


def test_tidy_math_wraps_then_escapes(tmp_path=None):
    # Full math pipeline order: aligned wrap happens, prose $ still escaped,
    # the display block stays a single $$...$$ token.
    src = "Cost is $5.\n\n" + r"$$a & = b \\ c & = d$$" + "\n"
    out = tidy(src, ruleset="generic", math=True)
    assert r"\begin{aligned}" in out
    assert r"\$5" in out           # prose dollar escaped
    assert out.count("$$") == 2     # exactly one display block, intact
