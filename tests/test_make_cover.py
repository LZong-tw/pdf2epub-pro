"""Tests for make_cover -- procedural cover + PDF page-1 rasterizer."""
import pypdfium2 as pdfium
from PIL import Image

from pdf2epub_pro.make_cover import make_cover, render_pdf_cover


def _blank_pdf(path, width=612, height=792, pages=1):
    doc = pdfium.PdfDocument.new()
    for _ in range(pages):
        doc.new_page(width, height)
    with open(path, "wb") as fh:
        doc.save(fh)


def test_render_pdf_cover_rasterizes_first_page(tmp_path):
    # REGRESSION: the pipeline drew a procedural cover even when the PDF
    # carried the book's real cover on page one.
    pdf = tmp_path / "book.pdf"
    _blank_pdf(pdf)
    out = render_pdf_cover(pdf, tmp_path / "cover.jpg")
    with Image.open(out) as img:
        assert img.format == "JPEG"
        assert img.width == 1600
        # 612x792pt page -> portrait aspect preserved
        assert abs(img.height - round(1600 * 792 / 612)) <= 1


def test_render_pdf_cover_custom_width(tmp_path):
    pdf = tmp_path / "book.pdf"
    _blank_pdf(pdf)
    out = render_pdf_cover(pdf, tmp_path / "cover.jpg", target_width=800)
    with Image.open(out) as img:
        assert img.width == 800


def test_make_cover_still_writes_procedural_jpeg(tmp_path):
    out = tmp_path / "cover.jpg"
    make_cover(out, super_title="", main_title=["T"], subtitle="",
               publisher="", variant="pillars")
    with Image.open(out) as img:
        assert img.format == "JPEG"
        assert (img.width, img.height) == (1600, 2400)
