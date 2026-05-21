"""Unit tests for pdf2epub_pro.restore_links."""
from pdf2epub_pro.restore_links import (
    DEFAULT_REL_URI_BASE,
    _is_safe_key,
    _norm,
    _normalize_uri,
    restore,
)


def test_norm_collapses_whitespace_and_lowercases():
    assert _norm("  Hello  WORLD\n ") == "hello world"


def test_is_safe_key_rejects_common_short_words():
    assert not _is_safe_key("how")
    assert not _is_safe_key("see")
    assert not _is_safe_key("here")
    assert not _is_safe_key("work")          # < 6 chars single-word
    assert not _is_safe_key("fOo")           # < 6 chars
    assert _is_safe_key("How Scaling Plans Work")   # multi-word OK
    assert _is_safe_key("Architecture")             # >= 6 chars single OK


def test_normalize_uri_prepends_base_to_relative():
    assert _normalize_uri("wellarchitected/latest/foo.html") == (
        DEFAULT_REL_URI_BASE + "wellarchitected/latest/foo.html"
    )


def test_normalize_uri_converts_backslashes():
    out = _normalize_uri("wellarchitected\\latest\\foo.html")
    assert "\\" not in out
    assert out.endswith("wellarchitected/latest/foo.html")


def test_normalize_uri_passes_absolute_through():
    assert _normalize_uri("https://aws.amazon.com/foo") == "https://aws.amazon.com/foo"
    assert _normalize_uri("mailto:x@y.com") == "mailto:x@y.com"
    assert _normalize_uri("#anchor") == "#anchor"


def test_restore_wraps_unlinked_occurrence():
    md = "See the AWS Well-Architected Framework Whitepaper for details."
    pairs = [("AWS Well-Architected Framework Whitepaper", "https://example.com/waf")]
    out, n = restore(md, pairs)
    assert n == 1
    assert "[AWS Well-Architected Framework Whitepaper](https://example.com/waf)" in out


def test_restore_skips_lines_already_containing_a_markdown_link():
    md = "Already linked: [AWS WAF](https://aws.amazon.com) and Lambda mentions."
    pairs = [("AWS WAF", "https://example.com/wrong"),
             ("Lambda", "https://example.com/lambda")]
    out, n = restore(md, pairs)
    # The line already contains [..](..), so we don't try to add more.
    assert n == 0
    assert "https://example.com/wrong" not in out


def test_restore_word_boundaries_prevent_mid_word_match():
    md = "Workloads run continuously."
    # If "Work" leaked into restore without word boundaries, it would split
    # "Workloads" into [Work](url)loads. The is_safe_key filter (< 6 chars,
    # single word) plus word-boundary regex must prevent that.
    pairs = [("Work", "https://example.com/work")]
    out, n = restore(md, pairs)
    assert n == 0
    assert "Workloads" in out
    assert "https://example.com/work" not in out


def test_restore_drops_oversaturated_single_word_key():
    """A single-word key whose match count blows past its PDF appearance
    count by more than 3× is treated as a section anchor, not an inline
    link — don't wrap the prose occurrences."""
    md = (
        "Resources are everywhere.\n"
        "Use Resources wisely.\n"
        "Resources must be tagged.\n"
        "Look at our Resources.\n"
        "Some Resources are private.\n"
    )
    # PDF only had 1 annotation pointing at /resources, but the word appears
    # 5+ times in prose — must NOT auto-link all of them.
    pairs = [("Resources", "https://example.com/resources")]
    out, n = restore(md, pairs)
    assert n == 0
    assert "https://example.com/resources" not in out
