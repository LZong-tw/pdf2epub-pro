"""Integration tests for the ``pdf2epub-audit`` CLI."""
from __future__ import annotations

import io
import json
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from pdf2epub_pro.audit import cli as audit_cli


_MIMETYPE = "application/epub+zip"
_CONTAINER_XML = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""


def _xhtml(body: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
        '<title>D</title></head><body>' + body + '</body></html>'
    )


def _build_epub(tmp_path: Path, members: dict[str, str]) -> Path:
    out = tmp_path / "book.epub"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(zipfile.ZipInfo("mimetype"), _MIMETYPE, zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", _CONTAINER_XML)
        for k, v in members.items():
            zf.writestr(k, v)
    return out


def _run(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = audit_cli.main(argv)
    return rc, buf.getvalue()


def test_cli_clean_markdown_returns_zero(tmp_path):
    md = tmp_path / "clean.md"
    md.write_text("# Real title\n\nA body paragraph.\n", encoding="utf-8")
    rc, out = _run(["--md", str(md)])
    assert rc == 0
    assert "No defects" in out


def test_cli_text_format_reports_findings(tmp_path):
    md = tmp_path / "bad.md"
    md.write_text("#SBATCH --time=1\n", encoding="utf-8")
    rc, out = _run(["--md", str(md)])
    assert rc == 2
    assert "[ERROR]" in out
    assert "slurm_directive_heading" in out


def test_cli_json_output_is_parsable(tmp_path):
    md = tmp_path / "bad.md"
    md.write_text("![x](https://docs.aws.amazon.com/a.png)\n", encoding="utf-8")
    rc, out = _run(["--md", str(md), "--json"])
    assert rc == 2
    data = json.loads(out)
    assert isinstance(data, list)
    assert any(f["detector"] == "aws_image_link" for f in data)


def test_cli_md_report_groups_by_detector(tmp_path):
    md = tmp_path / "bad.md"
    md.write_text(
        "#SBATCH --time=1\n\n"
        "![x](https://docs.aws.amazon.com/a.png)\n\n"
        "## ·\n",
        encoding="utf-8",
    )
    rc, out = _run(["--md", str(md), "--md-report"])
    assert rc == 2
    assert "# pdf2epub-audit report" in out
    assert "## `aws_image_link`" in out
    assert "## `slurm_directive_heading`" in out


def test_cli_exit_code_warn_only_is_one(tmp_path):
    md = tmp_path / "warn.md"
    # Long inline backtick triggers `code_fence_missing` (warn).
    md.write_text("text `" + "x" * 250 + "` more\n", encoding="utf-8")
    rc, _ = _run(["--md", str(md)])
    assert rc == 1


def test_cli_only_filter_runs_subset(tmp_path):
    md = tmp_path / "bad.md"
    md.write_text(
        "#SBATCH --time=1\n\n![x](https://docs.aws.amazon.com/a.png)\n",
        encoding="utf-8",
    )
    rc, out = _run(["--md", str(md), "--only", "aws_image_link"])
    assert "aws_image_link" in out
    assert "slurm_directive_heading" not in out
    # one detector still finds an error → exit 2
    assert rc == 2


def test_cli_epub_argument_routes_correctly(tmp_path):
    epub = _build_epub(tmp_path, {
        "OEBPS/a.xhtml": _xhtml('<h1 id="3pillars">Bad ID</h1>'),
    })
    rc, out = _run(["--epub", str(epub)])
    assert rc == 2
    assert "invalid_id" in out


def test_cli_list_detectors():
    rc, out = _run(["--list-detectors"])
    assert rc == 0
    assert "slurm_directive_heading" in out
    assert "invalid_id" in out
    assert "Markdown detectors" in out
    assert "EPUB detectors" in out


def test_cli_requires_input(monkeypatch, capsys):
    with pytest.raises(SystemExit):
        audit_cli.main([])
