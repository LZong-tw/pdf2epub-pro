"""Tests for the detector framework (Finding, registry, runners, exit code)."""
from pathlib import Path

from pdf2epub_pro.audit import framework as fw
from pdf2epub_pro.audit.framework import (
    EpubDetector,
    Finding,
    MarkdownDetector,
    exit_code_for,
    run_all_md,
)


def test_finding_to_dict_roundtrips_required_fields():
    f = Finding(detector="x", severity="warn", message="hi", file="a.md", line=4)
    d = f.to_dict()
    assert d["detector"] == "x"
    assert d["severity"] == "warn"
    assert d["file"] == "a.md"
    assert d["line"] == 4
    assert d["snippet"] is None


def test_severity_rank_ordering():
    info = Finding("x", "info", "")
    warn = Finding("x", "warn", "")
    err = Finding("x", "error", "")
    assert info.severity_rank < warn.severity_rank < err.severity_rank


def test_exit_code_for_empty_is_zero():
    assert exit_code_for([]) == 0


def test_exit_code_for_warn_only_is_one():
    assert exit_code_for([Finding("d", "warn", "m")]) == 1


def test_exit_code_for_any_error_is_two():
    assert exit_code_for([Finding("d", "warn", "m"),
                          Finding("d2", "error", "m")]) == 2


def test_run_all_md_catches_detector_crash(tmp_path: Path):
    """A misbehaving detector should not abort the whole run."""
    sample = tmp_path / "x.md"
    sample.write_text("hello\n", encoding="utf-8")

    class _Boom(MarkdownDetector):
        name = "boom"

        def run(self, path):
            raise RuntimeError("nope")

    findings = run_all_md(sample, detectors=[_Boom])
    assert len(findings) == 1
    assert findings[0].severity == "error"
    assert "crashed" in findings[0].message


def test_registry_decorators_are_idempotent():
    before_md = len(fw.registered_md_detectors())
    before_epub = len(fw.registered_epub_detectors())

    @fw.register_md_detector
    class _A(MarkdownDetector):
        name = "_test_a"

        def run(self, path):
            return []

    @fw.register_md_detector
    class _B(EpubDetector):  # type: ignore[misc] — sanity check shape
        name = "_test_b"

        def run(self, path):
            return []

    # Re-register the same class — should not duplicate.
    fw.register_md_detector(_A)
    fw.register_md_detector(_A)
    assert fw.registered_md_detectors().count(_A) == 1
    assert len(fw.registered_md_detectors()) >= before_md + 1
    # _B was registered via the md decorator but should still appear there;
    # epub registry should be unchanged.
    assert len(fw.registered_epub_detectors()) == before_epub
