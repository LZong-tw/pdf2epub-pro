"""Unit tests for pdf2epub_pro.llmdiff.cli."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pdf2epub_pro.llmdiff import cli
from pdf2epub_pro.llmdiff.cli import _select_backend, main


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
    # Simulate a host with no claude / codex CLIs installed so the
    # auto-select chain has nothing to grab onto and falls all the way
    # through to dry_run. (On developer machines those CLIs are usually
    # present, so we have to neutralise PATH here.)
    monkeypatch.setattr(cli.shutil, "which", lambda *a, **kw: None)

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
    # Force the JSON-payload dump path by selecting anthropic_api
    # explicitly. We never actually call the SDK because --dump-request
    # short-circuits before any network I/O.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-for-dump")
    rc = main([str(pdf), str(epub),
               "--dump-request",
               "--backend", "anthropic_api",
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


# ---------------------------------------------------------------------------
# Backend auto-selection
# ---------------------------------------------------------------------------

def _which_simulator(present: set[str]):
    """Return a fake ``shutil.which`` that reports tools in ``present``."""

    def fake_which(name: str, *args, **kwargs):
        return f"/usr/local/bin/{name}" if name in present else None

    return fake_which


def test_select_backend_prefers_claude_cli(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", _which_simulator({"claude", "codex"}))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert _select_backend("auto", dry_run_flag=False) == "claude_cli"


def test_select_backend_falls_back_to_codex(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", _which_simulator({"codex"}))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert _select_backend("auto", dry_run_flag=False) == "codex_cli"


def test_select_backend_falls_back_to_api_when_clis_absent(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", _which_simulator(set()))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(cli, "_anthropic_sdk_available", lambda: True)
    assert _select_backend("auto", dry_run_flag=False) == "anthropic_api"


def test_select_backend_falls_back_to_dry_run_when_nothing_available(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", _which_simulator(set()))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(cli, "_anthropic_sdk_available", lambda: False)
    assert _select_backend("auto", dry_run_flag=False) == "dry_run"


def test_select_backend_dry_run_flag_short_circuits_clis(monkeypatch):
    """``--dry-run`` is unconditional — historical contract."""
    monkeypatch.setattr(cli.shutil, "which", _which_simulator({"claude", "codex"}))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert _select_backend("auto", dry_run_flag=True) == "dry_run"


def test_select_backend_explicit_flag_overrides_auto_logic(monkeypatch):
    """Explicit ``--backend codex_cli`` wins even when claude is on PATH."""
    monkeypatch.setattr(cli.shutil, "which", _which_simulator({"claude", "codex"}))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert _select_backend("codex_cli", dry_run_flag=False) == "codex_cli"


def test_select_backend_no_api_key_skips_anthropic(monkeypatch):
    """An importable SDK is not enough — we also need the key."""
    monkeypatch.setattr(cli.shutil, "which", _which_simulator(set()))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(cli, "_anthropic_sdk_available", lambda: True)
    assert _select_backend("auto", dry_run_flag=False) == "dry_run"


def test_select_backend_no_sdk_skips_anthropic(monkeypatch):
    """Having the key but no SDK installed → don't claim anthropic_api."""
    monkeypatch.setattr(cli.shutil, "which", _which_simulator(set()))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(cli, "_anthropic_sdk_available", lambda: False)
    assert _select_backend("auto", dry_run_flag=False) == "dry_run"


def test_cli_explicit_claude_backend_invokes_subprocess(
    tiny_pair: tuple[Path, Path], tmp_path: Path, monkeypatch, capsys
):
    """``--backend claude_cli`` reaches subprocess.run with the right argv."""
    from pdf2epub_pro.llmdiff import differ

    seen: dict[str, object] = {}

    class _Fake:
        def __init__(self):
            self.returncode = 0
            self.stdout = json.dumps({"findings": []})
            self.stderr = ""

    def fake_run(cmd, **kwargs):
        seen.setdefault("calls", []).append(list(cmd))
        return _Fake()

    monkeypatch.setattr(differ.subprocess, "run", fake_run)
    # Ensure auto-select would also be claude_cli, but we're using explicit.
    monkeypatch.setattr(cli.shutil, "which", _which_simulator({"claude"}))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    pdf, epub = tiny_pair
    rc = main([str(pdf), str(epub),
               "-n", "2",
               "--out", str(tmp_path / "claude.md"),
               "--backend", "claude_cli",
               "--render-dir", str(tmp_path / "r"), "--dpi", "72"])
    assert rc == 0
    calls = seen.get("calls", [])
    assert calls, "expected at least one claude -p invocation"
    for call in calls:
        assert call[0] == "claude"
        assert call[1] == "-p"
        # Prompt must reference both image files.
        assert "PDF source" in call[2]
    out = capsys.readouterr().out
    assert "claude_cli" in out


def test_cli_dry_run_flag_beats_explicit_claude_backend(
    tiny_pair: tuple[Path, Path], tmp_path: Path, monkeypatch
):
    """``--dry-run --backend claude_cli`` must stay offline (no subprocess)."""
    from pdf2epub_pro.llmdiff import differ

    def fake_run(*a, **kw):
        raise AssertionError("subprocess.run must not be called in dry-run")

    monkeypatch.setattr(differ.subprocess, "run", fake_run)

    pdf, epub = tiny_pair
    rc = main([str(pdf), str(epub),
               "-n", "2",
               "--out", str(tmp_path / "dry.md"),
               "--backend", "claude_cli",
               "--dry-run",
               "--render-dir", str(tmp_path / "r"), "--dpi", "72"])
    assert rc == 0
    text = (tmp_path / "dry.md").read_text(encoding="utf-8")
    assert "**mode:** dry-run" in text


def test_cli_dump_request_with_claude_backend_dumps_prompt_string(
    tiny_pair: tuple[Path, Path], tmp_path: Path, monkeypatch, capsys
):
    """For CLI backends ``--dump-request`` should print the prompt string,
    not the Anthropic JSON payload."""
    pdf, epub = tiny_pair
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    rc = main([str(pdf), str(epub),
               "--dump-request",
               "--backend", "claude_cli",
               "--render-dir", str(tmp_path / "r"),
               "--dpi", "72"])
    assert rc == 0
    out = capsys.readouterr().out
    # Should be raw text, not JSON — and must name both image files.
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)
    assert "PDF source" in out
    assert "EPUB rendering" in out
    assert '"findings"' in out


def test_cli_auto_select_uses_claude_when_on_path(
    tiny_pair: tuple[Path, Path], tmp_path: Path, monkeypatch, capsys
):
    """End-to-end: when no flag is passed but ``claude`` is on PATH, the
    auto-selector must wire up the CLI backend and we observe the
    subprocess call."""
    from pdf2epub_pro.llmdiff import differ

    calls: list[list[str]] = []

    class _Fake:
        returncode = 0
        stdout = json.dumps({"findings": []})
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _Fake()

    monkeypatch.setattr(differ.subprocess, "run", fake_run)
    monkeypatch.setattr(cli.shutil, "which", _which_simulator({"claude"}))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    pdf, epub = tiny_pair
    rc = main([str(pdf), str(epub),
               "-n", "1",
               "--out", str(tmp_path / "auto.md"),
               "--render-dir", str(tmp_path / "r"), "--dpi", "72"])
    assert rc == 0
    assert calls and calls[0][0] == "claude"
    text = (tmp_path / "auto.md").read_text(encoding="utf-8")
    # Live backend (not dry-run) when claude_cli is selected.
    assert "**mode:** live" in text


def test_cli_anthropic_backend_without_key_errors(
    tiny_pair: tuple[Path, Path], tmp_path: Path, monkeypatch
):
    """``--backend anthropic_api`` with no key must raise SystemExit, not
    silently fall back to dry-run."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    pdf, epub = tiny_pair
    with pytest.raises(SystemExit):
        main([str(pdf), str(epub),
              "-n", "1",
              "--out", str(tmp_path / "x.md"),
              "--backend", "anthropic_api",
              "--render-dir", str(tmp_path / "r"), "--dpi", "72"])
