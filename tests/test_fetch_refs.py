"""Unit tests for pdf2epub_pro.fetch_refs — the appendix builder helpers."""
from pdf2epub_pro.fetch_refs import (
    _absolutize_links,
    _demote_headings,
    _fence_inline_code,
    _fix_broken_tables,
    _fix_mojibake,
    make_filter,
)


# ------------------------------------------------------------------- mojibake


def test_fix_mojibake_em_dash_round_trip():
    # 'â€™' is the classic Latin-1 misread of U+2019 (’) — UTF-8 bytes
    # E2 80 99 visible as 'â', '€', '™' under Latin-1.
    src = "Itâ€™s working"
    out = _fix_mojibake(src)
    assert "It’s working" in out
    assert "â" not in out


def test_fix_mojibake_idempotent_on_clean_text():
    src = "This sentence has — a real em dash."
    assert _fix_mojibake(src) == src


def test_fix_mojibake_ligature_dictionary():
    src = (
        "The data ï¬ow is critical. ï¬eld extraction is deï¬ned. "
        "eï¬ective and eï¬icient. speciï¬c modiï¬cations."
    )
    out = _fix_mojibake(src)
    assert "flow" in out
    assert "field" in out
    assert "defined" in out
    assert "effective" in out
    assert "efficient" in out
    assert "specific" in out
    assert "modifications" in out
    # No ligature leftover.
    assert "ï¬" not in out


# ----------------------------------------------------------------- demote


def test_demote_headings_shifts_levels():
    src = "# Top\n## Sub\n### Sub-sub\nbody"
    out = _demote_headings(src, by=2)
    assert "### Top" in out
    assert "#### Sub" in out
    assert "##### Sub-sub" in out


def test_demote_headings_caps_at_h6():
    src = "# Title\n##### deep"
    out = _demote_headings(src, by=3)
    # H1 + 3 = H4
    assert "#### Title" in out
    # H5 + 3 would be H8, but markdown stops at H6.
    assert "###### deep" in out


# --------------------------------------------------------------- absolutize


def test_absolutize_links_converts_relative_to_absolute():
    body = "See [the docs](./rds-controls.html) for more."
    out = _absolutize_links(body, "https://aws.amazon.com/blogs/foo/article/")
    # urljoin resolves "./rds-controls.html" against the article page,
    # keeping the path prefix.
    assert (
        "https://aws.amazon.com/blogs/foo/article/rds-controls.html" in out
    )


def test_absolutize_links_preserves_absolute_and_anchors():
    body = "[abs](https://x.com/a) and [mail](mailto:y@z.com)"
    out = _absolutize_links(body, "https://aws.amazon.com/")
    assert out == body


# ---------------------------------------------------------------- tables


def test_fix_broken_tables_collapses_pipe_rows():
    body = (
        "| On-Premises | vs. | On AWS |\n"
        "| Pre-provisioned grids |\n"
        "|\n"
        "Plain paragraph after."
    )
    out = _fix_broken_tables(body)
    # The pipe rows turn into em-dash joined text; lone-pipe line is gone.
    assert "On-Premises — vs. — On AWS" in out
    assert "|\n" not in out


def test_fix_broken_tables_preserves_real_tables():
    body = "| Col A | Col B |\n| --- | --- |\n| Cell 1 | Cell 2 |\n"
    out = _fix_broken_tables(body)
    # Valid table with separator passes through untouched.
    assert "| Col A | Col B |" in out
    assert "| --- | --- |" in out


# --------------------------------------------------------------- code fence


def test_fence_inline_code_promotes_long_json_inline():
    body = "Set the policy to `" + ('{"Sid": "X", "Effect": "Allow"}' * 10) + "` for real."
    out = _fence_inline_code(body)
    assert "```json" in out


def test_fence_inline_code_handles_multiline_backtick_span():
    body = "Use this template:\n`\nAWSTemplateFormatVersion: '2010'\nResources:\n  Foo: Bar\n`\nDone."
    out = _fence_inline_code(body)
    assert "```" in out


# -------------------------------------------------------------------- filter


def test_make_filter_keep_and_skip():
    keep = make_filter(
        keep_patterns=[r"aws\.amazon\.com/blogs/"],
        skip_patterns=[r"/category/"],
    )
    assert keep("https://aws.amazon.com/blogs/foo-post/")
    assert not keep("https://aws.amazon.com/blogs/category/financial-services/")
    assert not keep("https://other.example.com/")
