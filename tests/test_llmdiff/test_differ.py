"""Unit tests for pdf2epub_pro.llmdiff.differ."""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from PIL import Image

from pdf2epub_pro.llmdiff import differ
from pdf2epub_pro.llmdiff.chunker import Chunk, sample_chunks
from pdf2epub_pro.llmdiff.differ import (
    DEFAULT_MODEL,
    DEFAULT_SYSTEM_PROMPT,
    Finding,
    build_request,
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
