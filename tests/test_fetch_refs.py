"""Unit tests for pdf2epub_pro.fetch_refs — the appendix builder helpers."""
from pdf2epub_pro.fetch_refs import (
    _absolutize_links,
    _demote_headings,
    _escape_placeholders_in_code,
    _escape_shell_directive_lines,
    _fence_inline_code,
    _fix_broken_tables,
    _fix_mojibake,
    _looks_like_code,
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


def test_absolutize_links_handles_nested_brackets():
    """Trafilatura outputs AWS Security Hub control links as
    `[[ServiceName.N] Title text](./service-controls.html#service-N)`.
    The link-text regex must allow the inner balanced `[...]`.
    """
    body = (
        "Apply [[CloudTrail.1] CloudTrail should be enabled "
        "and configured](./cloudtrail-controls.html#cloudtrail-1)."
    )
    out = _absolutize_links(body, "https://docs.aws.amazon.com/securityhub/latest/userguide/sec-hub.html")
    assert "https://docs.aws.amazon.com/securityhub/latest/userguide/cloudtrail-controls.html#cloudtrail-1" in out
    # The visible label survives intact.
    assert "[CloudTrail.1] CloudTrail should be enabled and configured" in out


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


def test_fence_inline_code_keeps_prose_unfenced():
    # REGRESSION: a backtick span that wraps a prose sentence broken across
    # two lines must NOT be promoted to a code block.  Earlier behaviour
    # turned 'command in the AWS CLI:' into '<pre><code>command in the AWS
    # CLI:</code></pre>'.
    body = "Run `create-repository\ncommand in the AWS CLI:` then continue."
    out = _fence_inline_code(body)
    assert "```" not in out
    assert "command in the AWS CLI:" in out


def test_escape_placeholders_in_code_handles_cli_angle_brackets():
    body = "Set `export ACCOUNT=<TOOLING_ACCOUNT_ID>` in your shell."
    out = _escape_placeholders_in_code(body)
    assert "&lt;TOOLING_ACCOUNT_ID&gt;" in out
    assert "<TOOLING_ACCOUNT_ID>" not in out


def test_escape_placeholders_in_code_handles_bare_multiword_placeholder():
    # REGRESSION: '<Microsoft Entra Tenant ID>' in bare prose used to slip
    # through and python-markdown parsed it as a malformed tag with
    # ``id=""``, colliding across chunks and tripping Calibre's
    # DuplicateId on the final EPUB.
    body = "Use <Microsoft Entra Tenant ID> as the tenant identifier."
    out = _escape_placeholders_in_code(body)
    assert "&lt;Microsoft Entra Tenant ID&gt;" in out
    assert "<Microsoft Entra Tenant ID>" not in out


def test_escape_placeholders_in_code_leaves_real_html_alone():
    # Real markdown-embedded HTML tags start with lowercase and must not
    # get escaped; otherwise legitimate raw-HTML blocks break.
    body = 'Click <a href="https://example.com">here</a> for more.'
    out = _escape_placeholders_in_code(body)
    assert '<a href="https://example.com">' in out
    assert "&lt;a" not in out


def test_escape_placeholders_in_code_leaves_autolinks_alone():
    body = "See <https://example.com> for the spec."
    out = _escape_placeholders_in_code(body)
    assert "<https://example.com>" in out
    assert "&lt;https" not in out


def test_escape_placeholders_in_code_skips_singleword_capital():
    # A single capitalized token without whitespace is not a placeholder
    # pattern under our rule — too risky given valid uses like <DETAILS>
    # uppercase HTML or section markers.  These should be handled by the
    # backtick branch when authors mark them as code.
    body = "The <FOO> section is optional."
    out = _escape_placeholders_in_code(body)
    assert "<FOO>" in out
    assert "&lt;FOO&gt;" not in out


def test_looks_like_code_recognises_shell_directives():
    # SLURM directive must be detected so multiline backticks containing
    # them stay fenced instead of being unwrapped into prose with #SBATCH
    # lines that markdown would parse as H1 headings.
    assert _looks_like_code("#SBATCH -o video-gen-stage-1.out\nexport X=1")
    assert _looks_like_code("#!/bin/bash\nset -e\nexport FOO=bar")
    assert _looks_like_code("aws s3 ls s3://my-bucket\nsource ./venv/bin/activate")


def test_escape_shell_directive_lines_prevents_h1_hijack():
    body = (
        "Some prose here.\n"
        "#SBATCH -o video-gen-stage-1.out\n"
        "#SBATCH --job-name=video-gen\n"
        "More prose.\n"
        "#!/bin/bash\n"
    )
    out = _escape_shell_directive_lines(body)
    # The '#' is escaped so markdown won't treat the line as an H1.
    assert "\\#SBATCH -o video-gen-stage-1.out" in out
    assert "\\#SBATCH --job-name=video-gen" in out
    assert "\\#!/bin/bash" in out
    # Regular prose lines are untouched.
    assert "Some prose here." in out
    assert "More prose." in out


# -------------------------------------------------------------------- filter


def test_make_filter_keep_and_skip():
    keep = make_filter(
        keep_patterns=[r"aws\.amazon\.com/blogs/"],
        skip_patterns=[r"/category/"],
    )
    assert keep("https://aws.amazon.com/blogs/foo-post/")
    assert not keep("https://aws.amazon.com/blogs/category/financial-services/")
    assert not keep("https://other.example.com/")
