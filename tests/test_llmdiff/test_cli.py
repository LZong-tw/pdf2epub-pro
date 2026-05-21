"""Unit tests for pdf2epub_pro.llmdiff.cli."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pdf2epub_pro.llmdiff import cli
from pdf2epub_pro.llmdiff.cli import main


@pytest.fixture(autouse=True)
def _force_fallback_renderer(monkeypatch):
    """Make sure the CLI never tries to launch Playwright during tests."""
    from pdf2epub_pro.llmdiff import renderer
    monkeypatch.setattr(renderer, "_try_playwright_render",
                        lambda *a, **kw: False)


def test_cli_dry_run_writes_report(
    tiny_pair: tuple[Path, Path], tmp_path: Path, monkeypatch, capsys
):
    pdf, epub = tiny_pair
    out = tmp_path / "report.md"
    # Make sure ANTHROPIC_API_KEY is gone so the dry-run stub kicks in
    # even without --dry-run; here we still pass --dry-run for clarity.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    rc = main([
        str(pdf), str(epub),
        "-n", "3",
        "--out", str(out),
        "--dry-run",
        "--render-dir", str(tmp_path / "renders"),
        "--dpi", "72",
    ])
    assert rc == 0
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "# pdf2epub-llmdiff report" in text
    assert "**mode:** dry-run" in text
    # Should mention each sampled page.
    assert "PDF page 0" in text
    # Report banner should mention the model.
    assert "claude-haiku-4-5" in text
    # Console output should describe the run.
    out_msg = capsys.readouterr().out
    assert "chunks" in out_msg


def test_cli_falls_back_to_dry_run_when_no_api_key(
    tiny_pair: tuple[Path, Path], tmp_path: Path, monkeypatch
):
    pdf, epub = tiny_pair
    out = tmp_path / "noapi.md"
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    rc = main([str(pdf), str(epub), "-n", "2", "--out", str(out),
               "--render-dir", str(tmp_path / "r"), "--dpi", "72"])
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    # Without an API key we should be in dry-run mode even without the flag.
    assert "**mode:** dry-run" in text


def test_cli_dump_request_prints_json(
    tiny_pair: tuple[Path, Path], tmp_path: Path, monkeypatch, capsys
):
    pdf, epub = tiny_pair
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    rc = main([str(pdf), str(epub),
               "--dump-request",
               "--render-dir", str(tmp_path / "r"),
               "--dpi", "72"])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["model"]
    # Base64 image data should be truncated for readability.
    blocks = payload["messages"][0]["content"]
    images = [b for b in blocks if b["type"] == "image"]
    assert len(images) == 2
    for b in images:
        assert "truncated" in b["source"]["data"]


def test_cli_returns_2_when_pdf_missing(tmp_path: Path, capsys):
    rc = main(["nope.pdf", "nope.epub", "--dry-run",
               "--out", str(tmp_path / "x.md")])
    assert rc == 2
