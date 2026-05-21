"""Unit tests for pdf2epub_pro.tidy — the heart of the cleanup pipeline."""
from pdf2epub_pro.tidy import (
    apply_corpus_fixes,
    consolidate_title,
    demote_subsections_aws,
    fix_digit_headings,
    heal_broken_sentences,
    heal_hyphen_breaks,
    heal_intra_word_spaces,
    heal_list_gaps,
    indent_lettered_sublists,
    normalize_relative_links,
    promote_pillars_aws,
    space_markdown_adjacency,
    strip_chunk_dividers,
    strip_emphasis_inner_space,
    strip_orphan_dashes,
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
    # IMAGE not rewritten — most important regression target.
    assert any(
        "![Image](local_artifacts/diagram.png)" in l for l in lines
    )
