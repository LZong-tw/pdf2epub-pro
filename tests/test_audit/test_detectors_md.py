"""Per-detector tests for markdown-level defect detectors.

Each detector gets at least one positive (defect-bearing) and one negative
(clean input) test.  Fixtures live inline so the failure mode of the regex
is obvious next to its assertion.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pdf2epub_pro.audit.detectors_md import (
    ExternalImageLinkDetector,
    BulletAsHeadingDetector,
    CodeFenceFalsePositiveDetector,
    CodeFenceMissingDetector,
    GluedCompoundDetector,
    EmptyHeadingDetector,
    H1ExplosionDetector,
    HyphenBreakArtifactDetector,
    ListingPageContaminationDetector,
    MojibakeDetector,
    NestedBracketUnresolvedLinkDetector,
    OrphanPageNumberDetector,
    SlurmDirectiveHeadingDetector,
    UrlBackslashDetector,
)


def _write(tmp_path: Path, text: str, name: str = "doc.md") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# -- 1. SLURM directive heading --------------------------------------------
def test_slurm_directive_heading_positive(tmp_path):
    src = "Intro paragraph.\n\n#SBATCH --time=01:00:00\n\nMore text.\n"
    findings = list(SlurmDirectiveHeadingDetector().run(_write(tmp_path, src)))
    assert len(findings) == 1
    assert findings[0].line == 3
    assert findings[0].severity == "error"


def test_slurm_directive_inside_fence_is_ignored(tmp_path):
    src = "```bash\n#SBATCH --time=01:00:00\n```\n"
    findings = list(SlurmDirectiveHeadingDetector().run(_write(tmp_path, src)))
    assert findings == []


def test_slurm_directive_heading_clean(tmp_path):
    src = "# Real heading\n\nBody.\n"
    assert list(SlurmDirectiveHeadingDetector().run(_write(tmp_path, src))) == []


# -- 2. Code-fence false positive ------------------------------------------
def test_code_fence_false_positive_flags_short_prose(tmp_path):
    src = "Intro.\n\n```\nThis is a normal sentence about cats.\n```\n\nAfter.\n"
    findings = list(CodeFenceFalsePositiveDetector().run(_write(tmp_path, src)))
    assert len(findings) == 1
    assert findings[0].detector == "code_fence_false_positive"


def test_code_fence_false_positive_skips_real_code(tmp_path):
    src = "```python\ndef foo():\n    return 1\n```\n"
    assert list(CodeFenceFalsePositiveDetector().run(_write(tmp_path, src))) == []


def test_code_fence_false_positive_skips_long_block(tmp_path):
    body = "\n".join(f"line {i}" for i in range(8))
    src = f"```\n{body}\n```\n"
    assert list(CodeFenceFalsePositiveDetector().run(_write(tmp_path, src))) == []


# -- 3. Code-fence missing -------------------------------------------------
def test_code_fence_missing_flags_long_inline(tmp_path):
    blob = "x" * 250
    src = f"prose `{blob}` more\n"
    findings = list(CodeFenceMissingDetector().run(_write(tmp_path, src)))
    assert len(findings) == 1
    assert findings[0].line == 1


def test_code_fence_missing_ignores_short_inline(tmp_path):
    src = "Use `foo()` to call it.\n"
    assert list(CodeFenceMissingDetector().run(_write(tmp_path, src))) == []


# -- 4. External (remote) image link --------------------------------------
def test_external_image_link_positive(tmp_path):
    src = "![diagram](https://example.com/images/foo.png)\n"
    findings = list(ExternalImageLinkDetector().run(_write(tmp_path, src)))
    assert len(findings) == 1
    assert findings[0].severity == "error"


def test_external_image_link_negative_local(tmp_path):
    src = "![diagram](images/foo.png)\n"
    assert list(ExternalImageLinkDetector().run(_write(tmp_path, src))) == []


def test_external_image_link_negative_normal_anchor(tmp_path):
    # Plain `[text](http-url)` is NOT an image — must not trigger.
    src = "[See diagram](https://example.com/page.html)\n"
    assert list(ExternalImageLinkDetector().run(_write(tmp_path, src))) == []


def test_external_image_link_host_pattern_override(tmp_path):
    # Custom subclass narrowing to one host should ignore other hosts.
    class StrictDetector(ExternalImageLinkDetector):
        host_pattern = r"https?://only-this-host\.example/[^)\s]+"

    src = (
        "![a](https://only-this-host.example/x.png)\n"
        "![b](https://other-host.example/y.png)\n"
    )
    findings = list(StrictDetector().run(_write(tmp_path, src)))
    assert len(findings) == 1
    assert "only-this-host" in findings[0].snippet


# -- 5. Nested-bracket relative link ---------------------------------------
def test_nested_bracket_unresolved_link_positive(tmp_path):
    src = "See [[CT.1] CloudTrail item](./security-hub-controls.html) above.\n"
    findings = list(NestedBracketUnresolvedLinkDetector().run(_write(tmp_path, src)))
    assert len(findings) == 1
    assert findings[0].severity == "error"


def test_nested_bracket_unresolved_link_negative_absolute(tmp_path):
    src = "See [[CT.1] item](https://docs.aws.amazon.com/securityhub.html) above.\n"
    assert list(NestedBracketUnresolvedLinkDetector().run(_write(tmp_path, src))) == []


# -- 6. Mojibake -----------------------------------------------------------
def test_mojibake_positive(tmp_path):
    src = "The ï¬le contains a typo.\n"
    findings = list(MojibakeDetector().run(_write(tmp_path, src)))
    assert len(findings) == 1
    assert "mojibake" in findings[0].message


def test_mojibake_negative(tmp_path):
    src = "The file contains no problems.\n"
    assert list(MojibakeDetector().run(_write(tmp_path, src))) == []


# -- 7. Orphan page number -------------------------------------------------
def test_orphan_page_number_positive(tmp_path):
    src = "End of section.\n\n15\n\nNew section starts.\n"
    findings = list(OrphanPageNumberDetector().run(_write(tmp_path, src)))
    assert len(findings) == 1
    assert findings[0].line == 3


def test_orphan_page_number_skips_step_n(tmp_path):
    # A digit on its own line with content adjacent — not a page number.
    src = "Step 15 in the process.\n"
    assert list(OrphanPageNumberDetector().run(_write(tmp_path, src))) == []


# -- 8. Listing-page contamination -----------------------------------------
def test_listing_page_contamination_positive(tmp_path):
    rows = "\n".join(f"Section {i} ............... {i + 10}" for i in range(6))
    src = f"Intro.\n\n{rows}\n\nDone.\n"
    findings = list(ListingPageContaminationDetector().run(_write(tmp_path, src)))
    assert len(findings) == 1
    assert "6 consecutive" in findings[0].message


def test_listing_page_contamination_short_run_ignored(tmp_path):
    rows = "\n".join(f"Section {i} ............... {i + 10}" for i in range(3))
    src = f"Intro.\n\n{rows}\n\nDone.\n"
    assert list(ListingPageContaminationDetector().run(_write(tmp_path, src))) == []


# -- 9. Hyphen-break artifact ---------------------------------------------
def test_hyphen_break_artifact_positive(tmp_path):
    src = "We use a fault- tolerant system.\n"
    findings = list(HyphenBreakArtifactDetector().run(_write(tmp_path, src)))
    assert len(findings) == 1
    assert "fault-tolerant" in findings[0].message


def test_hyphen_break_artifact_negative_unknown_compound(tmp_path):
    # `rubber- ducky` is not in the known compound list, so don't flag.
    src = "We bought a rubber- ducky yesterday.\n"
    assert list(HyphenBreakArtifactDetector().run(_write(tmp_path, src))) == []


# -- 10. Glued compound regression ----------------------------------------
@pytest.mark.parametrize("token", [
    "realtime", "thirdparty", "finegrained", "costeffective",
])
def test_glued_compound_positive(tmp_path, token):
    src = f"We need a {token} system.\n"
    findings = list(GluedCompoundDetector().run(_write(tmp_path, src)))
    assert len(findings) == 1


def test_glued_compound_skips_url(tmp_path):
    # The default token list intentionally does NOT include 'wellarchitected'
    # any more (that was AWS-corpus-specific noise); pick a token that IS in
    # the default list and verify URL context exclusion still applies.
    src = "See https://example.com/realtime/intro.html for details.\n"
    assert list(GluedCompoundDetector().run(_write(tmp_path, src))) == []


def test_glued_compound_tokens_overridable(tmp_path):
    class FooDetector(GluedCompoundDetector):
        tokens = ("foobarbaz",)

    src = "Avoid foobarbaz in prose. realtime is not in this list.\n"
    findings = list(FooDetector().run(_write(tmp_path, src)))
    assert len(findings) == 1
    assert "foobarbaz" in findings[0].message


def test_glued_compound_empty_tokens_is_noop(tmp_path):
    class EmptyDetector(GluedCompoundDetector):
        tokens = ()

    src = "realtime thirdparty finegrained should not fire.\n"
    assert list(EmptyDetector().run(_write(tmp_path, src))) == []


# -- 11. H1 explosion ------------------------------------------------------
def test_h1_explosion_positive(tmp_path):
    src = "\n".join(f"# Section {i}" for i in range(10)) + "\n"
    findings = list(H1ExplosionDetector().run(_write(tmp_path, src)))
    assert len(findings) == 1
    assert findings[0].extra["count"] == 10


def test_h1_explosion_below_threshold(tmp_path):
    src = "\n".join(f"# Section {i}" for i in range(5)) + "\n"
    assert list(H1ExplosionDetector().run(_write(tmp_path, src))) == []


# -- 12. Bullet-as-heading -------------------------------------------------
@pytest.mark.parametrize("glyph", ["·", "•", "●"])
def test_bullet_as_heading_positive(tmp_path, glyph):
    src = f"## {glyph}\n"
    findings = list(BulletAsHeadingDetector().run(_write(tmp_path, src)))
    assert len(findings) == 1


def test_bullet_as_heading_negative(tmp_path):
    src = "## Real subheading\n"
    assert list(BulletAsHeadingDetector().run(_write(tmp_path, src))) == []


# -- 13. Empty heading -----------------------------------------------------
def test_empty_heading_positive_empty(tmp_path):
    src = "#\n\n## \n"
    findings = list(EmptyHeadingDetector().run(_write(tmp_path, src)))
    assert len(findings) == 2


def test_empty_heading_positive_punct_only(tmp_path):
    src = "## ---\n"
    findings = list(EmptyHeadingDetector().run(_write(tmp_path, src)))
    assert len(findings) == 1


def test_empty_heading_negative(tmp_path):
    src = "## Section A\n"
    assert list(EmptyHeadingDetector().run(_write(tmp_path, src))) == []


# -- 14. URL with backslash -----------------------------------------------
def test_url_backslash_positive(tmp_path):
    src = r"Go to https://example.com\foo\bar.html now." + "\n"
    findings = list(UrlBackslashDetector().run(_write(tmp_path, src)))
    assert len(findings) == 1
    assert findings[0].severity == "error"


def test_url_backslash_negative(tmp_path):
    src = "Go to https://example.com/foo/bar.html now.\n"
    assert list(UrlBackslashDetector().run(_write(tmp_path, src))) == []
