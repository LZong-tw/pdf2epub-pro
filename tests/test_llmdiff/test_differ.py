"""Unit tests for pdf2epub_pro.llmdiff.differ."""
from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from pdf2epub_pro.llmdiff import differ
from pdf2epub_pro.llmdiff.chunker import Chunk, sample_chunks
from pdf2epub_pro.llmdiff.differ import (
    DEFAULT_MODEL,
    DEFAULT_SYSTEM_PROMPT,
    Finding,
    build_cli_prompt,
    build_request,
    claude_cli_call_fn,
    codex_cli_call_fn,
    diff_chunk,
    dry_run_llm,
    parse_findings,
)


def _make_tiny_png(path: Path, color: str = "white") -> Path:
    img = Image.new("RGB", (32, 32), color)
    img.save(path, format="PNG")
    return path


def test_build_request_shape(tmp_path: Path):
    a = _make_tiny_png(tmp_path / "a.png", "white")
    b = _make_tiny_png(tmp_path / "b.png", "black")
    req = build_request(a, b, model="claude-haiku-4-5-20251001")

    assert req["model"] == "claude-haiku-4-5-20251001"
    assert req["max_tokens"] > 0
    assert req["system"] == DEFAULT_SYSTEM_PROMPT
    assert isinstance(req["messages"], list) and len(req["messages"]) == 1

    msg = req["messages"][0]
    assert msg["role"] == "user"
    blocks = msg["content"]
    # Expected interleave: text, image, text, image, text.
    types = [b["type"] for b in blocks]
    assert types == ["text", "image", "text", "image", "text"]

    images = [b for b in blocks if b["type"] == "image"]
    for blk in images:
        src = blk["source"]
        assert src["type"] == "base64"
        assert src["media_type"] == "image/png"
        # Round-trip the base64 to make sure it's valid.
        base64.standard_b64decode(src["data"])


def test_build_request_custom_system_prompt(tmp_path: Path):
    a = _make_tiny_png(tmp_path / "a.png")
    b = _make_tiny_png(tmp_path / "b.png")
    req = build_request(a, b, model="x", system_prompt="STRICT JSON ONLY.")
    assert req["system"] == "STRICT JSON ONLY."


def test_dry_run_llm_returns_empty_findings():
    req = {"model": "x", "messages": []}
    resp = dry_run_llm(req)
    parsed = json.loads(resp["content"][0]["text"])
    assert parsed == {"findings": []}
    # parse_findings round-trip is empty.
    chunk = Chunk(pdf_page=0, epub_file="", epub_para_range=(0, 0),
                  anchor_text="")
    assert parse_findings(resp, chunk) == []


def test_parse_findings_handles_markdown_fences():
    chunk = Chunk(pdf_page=2, epub_file="ch.xhtml", epub_para_range=(1, 4),
                  anchor_text="hello")
    response = {
        "content": [{"type": "text", "text": (
            "Here are my findings:\n"
            "```json\n"
            '{"findings": [{"severity": "error", "type": "missing_text", '
            '"description": "Footnote dropped."}]}\n'
            "```\n"
        )}],
    }
    out = parse_findings(response, chunk)
    assert len(out) == 1
    assert out[0].severity == "error"
    assert out[0].type == "missing_text"
    assert out[0].pdf_page == 2
    assert out[0].epub_file == "ch.xhtml"


def test_parse_findings_surfaces_parse_errors_as_info():
    chunk = Chunk(pdf_page=0, epub_file="", epub_para_range=(0, 0),
                  anchor_text="")
    response = {"content": [{"type": "text", "text": "totally not json"}]}
    out = parse_findings(response, chunk)
    assert len(out) == 1
    assert out[0].severity == "info"
    assert out[0].type == "parse_error"


def test_diff_chunk_end_to_end_dry_run(
    tiny_pair: tuple[Path, Path], tmp_path: Path, monkeypatch
):
    # Force the fallback renderer so the test runs without Playwright.
    from pdf2epub_pro.llmdiff import renderer
    monkeypatch.setattr(renderer, "_try_playwright_render",
                        lambda *a, **kw: False)

    pdf, epub = tiny_pair
    chunks = sample_chunks(pdf, epub, n=2)
    findings = diff_chunk(
        chunks[0], pdf, epub, dry_run_llm,
        render_dir=tmp_path / "renders",
        dpi=72,
    )
    assert findings == []  # dry run returns empty findings


def test_diff_chunk_passes_model_into_request(
    tiny_pair: tuple[Path, Path], tmp_path: Path, monkeypatch
):
    """The model arg must reach build_request — capture it via a fake."""
    from pdf2epub_pro.llmdiff import renderer
    monkeypatch.setattr(renderer, "_try_playwright_render",
                        lambda *a, **kw: False)

    seen: dict[str, object] = {}

    def fake_llm(req: dict) -> dict:
        seen["model"] = req["model"]
        seen["system"] = req["system"]
        seen["n_blocks"] = len(req["messages"][0]["content"])
        return dry_run_llm(req)

    pdf, epub = tiny_pair
    chunks = sample_chunks(pdf, epub, n=1)
    diff_chunk(
        chunks[0], pdf, epub, fake_llm,
        model="claude-haiku-4-5-20251001",
        render_dir=tmp_path / "r",
        dpi=72,
    )
    assert seen["model"] == "claude-haiku-4-5-20251001"
    assert "JSON" in seen["system"]
    assert seen["n_blocks"] == 5


# ---------------------------------------------------------------------------
# build_cli_prompt
# ---------------------------------------------------------------------------

def test_build_cli_prompt_mentions_both_files(tmp_path: Path):
    pdf_img = _make_tiny_png(tmp_path / "pdf.png")
    epub_img = _make_tiny_png(tmp_path / "epub.png")
    prompt = build_cli_prompt(pdf_img, epub_img)

    # Both absolute paths must appear so the CLI tool can open the files.
    assert str(pdf_img.resolve()) in prompt
    assert str(epub_img.resolve()) in prompt
    # PDF labelled as source-of-truth, EPUB as the candidate under review.
    assert "PDF source" in prompt
    assert "EPUB rendering" in prompt
    # JSON schema description must be present so the model knows the
    # exact shape expected by parse_findings.
    assert '"findings"' in prompt
    assert "severity" in prompt
    assert "missing_text" in prompt
    # Must instruct against prose/markdown fences.
    assert "no markdown" in prompt.lower()


def test_build_cli_prompt_uses_absolute_paths(tmp_path: Path, monkeypatch):
    """Relative paths get resolved so the CLI sees a stable location even
    when its working directory differs from ours."""
    monkeypatch.chdir(tmp_path)
    _make_tiny_png(tmp_path / "a.png")
    _make_tiny_png(tmp_path / "b.png")
    prompt = build_cli_prompt("a.png", "b.png")
    # Resolved paths are absolute on every platform.
    assert str((tmp_path / "a.png").resolve()) in prompt
    assert str((tmp_path / "b.png").resolve()) in prompt


# ---------------------------------------------------------------------------
# claude_cli_call_fn / codex_cli_call_fn
# ---------------------------------------------------------------------------

class _FakeCompleted:
    """Minimal stand-in for subprocess.CompletedProcess used by the mocks."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _capture_subprocess(monkeypatch, *, returncode: int = 0,
                       stdout: str = "", stderr: str = "",
                       raise_exc: Exception | None = None):
    """Install a fake subprocess.run that records its arguments."""
    seen: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = list(cmd)
        seen["kwargs"] = kwargs
        if raise_exc is not None:
            raise raise_exc
        return _FakeCompleted(returncode, stdout, stderr)

    monkeypatch.setattr(differ.subprocess, "run", fake_run)
    return seen


def test_claude_cli_call_fn_happy_path(monkeypatch):
    payload = json.dumps({"findings": [
        {"severity": "warn", "type": "formatting", "description": "italics lost"},
    ]})
    seen = _capture_subprocess(monkeypatch, returncode=0, stdout=payload + "\n")

    req = {"model": "claude-haiku-4-5-20251001",
           "cli_prompt": "compare these images: a.png b.png"}
    resp = claude_cli_call_fn(req)

    assert seen["cmd"] == ["claude", "-p", req["cli_prompt"]]
    assert seen["kwargs"]["capture_output"] is True
    assert seen["kwargs"]["text"] is True
    assert seen["kwargs"]["timeout"] == differ.DEFAULT_CLI_TIMEOUT
    # Response is wrapped in Messages-API shape so parse_findings works.
    assert resp["content"][0]["type"] == "text"
    chunk = Chunk(pdf_page=0, epub_file="x.xhtml", epub_para_range=(0, 1),
                  anchor_text="")
    findings = parse_findings(resp, chunk)
    assert len(findings) == 1
    assert findings[0].type == "formatting"


def test_codex_cli_call_fn_invokes_codex_binary(monkeypatch):
    seen = _capture_subprocess(
        monkeypatch, returncode=0,
        stdout='{"findings": []}',
    )
    resp = codex_cli_call_fn({"model": "x", "cli_prompt": "p"})
    assert seen["cmd"][0] == "codex"
    assert seen["cmd"][1] == "-p"
    chunk = Chunk(pdf_page=0, epub_file="", epub_para_range=(0, 0),
                  anchor_text="")
    assert parse_findings(resp, chunk) == []


def test_cli_call_fn_nonzero_exit_returns_empty_findings(monkeypatch, capsys):
    _capture_subprocess(
        monkeypatch, returncode=1, stdout="", stderr="boom: something broke",
    )
    resp = claude_cli_call_fn({"model": "x", "cli_prompt": "p"})
    # Should log to stderr and return wrapped empty findings, not raise.
    err = capsys.readouterr().err
    assert "claude" in err
    assert "exited" in err
    chunk = Chunk(pdf_page=0, epub_file="", epub_para_range=(0, 0),
                  anchor_text="")
    assert parse_findings(resp, chunk) == []


def test_cli_call_fn_missing_binary_raises(monkeypatch):
    """FileNotFoundError must propagate so the auto-select chain can catch
    it and try the next backend candidate."""
    _capture_subprocess(monkeypatch,
                        raise_exc=FileNotFoundError("no such CLI"))
    with pytest.raises(FileNotFoundError):
        claude_cli_call_fn({"model": "x", "cli_prompt": "p"})


def test_cli_call_fn_unparseable_stdout_surfaces_parse_error(monkeypatch):
    """The CLI returned 0 but its stdout isn't JSON. parse_findings should
    flag that as a single ``info``/``parse_error`` finding rather than
    blowing up — same behaviour as for the API path."""
    _capture_subprocess(
        monkeypatch, returncode=0,
        stdout="sorry, I can't comply with that",
    )
    resp = claude_cli_call_fn({"model": "x", "cli_prompt": "p"})
    chunk = Chunk(pdf_page=1, epub_file="ch.xhtml", epub_para_range=(0, 1),
                  anchor_text="")
    findings = parse_findings(resp, chunk)
    assert len(findings) == 1
    assert findings[0].severity == "info"
    assert findings[0].type == "parse_error"


def test_cli_call_fn_timeout_returns_empty_findings(monkeypatch, capsys):
    _capture_subprocess(
        monkeypatch,
        raise_exc=subprocess.TimeoutExpired(cmd="claude", timeout=120),
    )
    resp = claude_cli_call_fn({"model": "x", "cli_prompt": "p"})
    err = capsys.readouterr().err
    assert "timed out" in err
    chunk = Chunk(pdf_page=0, epub_file="", epub_para_range=(0, 0),
                  anchor_text="")
    assert parse_findings(resp, chunk) == []


def test_diff_chunk_attaches_cli_prompt(
    tiny_pair: tuple[Path, Path], tmp_path: Path, monkeypatch
):
    """``diff_chunk`` must populate ``cli_prompt`` so CLI backends can
    grab a ready-to-shell-out string without rebuilding it themselves."""
    from pdf2epub_pro.llmdiff import renderer
    monkeypatch.setattr(renderer, "_try_playwright_render",
                        lambda *a, **kw: False)

    seen: dict[str, object] = {}

    def fake_llm(req: dict) -> dict:
        seen["cli_prompt"] = req.get("cli_prompt")
        return dry_run_llm(req)

    pdf, epub = tiny_pair
    chunks = sample_chunks(pdf, epub, n=1)
    diff_chunk(chunks[0], pdf, epub, fake_llm,
               render_dir=tmp_path / "r", dpi=72)
    prompt = seen["cli_prompt"]
    assert isinstance(prompt, str) and prompt
    assert "PDF source" in prompt
    assert "EPUB rendering" in prompt
