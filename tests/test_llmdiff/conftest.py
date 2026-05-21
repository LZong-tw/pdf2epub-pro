"""Shared fixtures: hand-rolled tiny PDFs + EPUBs that match each other.

We avoid reportlab (not installed) and any other heavy dep by emitting
small, valid PDF byte streams ourselves. The PDFs are intentionally
minimal: one Helvetica string per page, one MediaBox, no compression.
"""
from __future__ import annotations

import textwrap
import zipfile
from pathlib import Path

import pytest


def _pdf_escape(text: str) -> str:
    """Escape ``(`` ``)`` ``\\`` for use inside a PDF literal string."""
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_tiny_pdf(out_path: Path, pages: list[str]) -> Path:
    """Write a minimal PDF with one text line per page.

    Each page renders the corresponding string from ``pages`` at 72,720 in
    the standard letter-size 612x792 media box. The text is rendered as a
    single Helvetica run; the PDF text layer (read via ``pypdfium2``) will
    return exactly that string."""
    if not pages:
        raise ValueError("need at least one page")

    objects: list[bytes] = []  # 1-indexed objects (entry 0 is the free entry)

    def add(obj_bytes: bytes) -> int:
        objects.append(obj_bytes)
        return len(objects)

    # Reserve placeholders to keep referencing simple. We know the layout:
    #   1 = Catalog
    #   2 = Pages
    #   3..3+N-1 = Page objects
    #   3+N..3+2N-1 = Content streams
    #   3+2N = Font (Helvetica)
    n = len(pages)
    font_obj_id = 3 + 2 * n

    # Object 1: Catalog
    add(f"<</Type/Catalog/Pages 2 0 R>>".encode())

    # Object 2: Pages (Kids placeholder filled below)
    page_obj_ids = list(range(3, 3 + n))
    kids = " ".join(f"{i} 0 R" for i in page_obj_ids)
    add(f"<</Type/Pages/Count {n}/Kids[{kids}]>>".encode())

    # Page objects
    for i in range(n):
        contents_id = 3 + n + i
        page_dict = (
            f"<</Type/Page/Parent 2 0 R"
            f"/MediaBox[0 0 612 792]"
            f"/Contents {contents_id} 0 R"
            f"/Resources<</Font<</F1 {font_obj_id} 0 R>>>>"
            f">>"
        )
        add(page_dict.encode())

    # Content streams
    for txt in pages:
        body = (
            f"BT\n/F1 12 Tf\n72 720 Td\n({_pdf_escape(txt)}) Tj\nET\n"
        ).encode()
        stream = b"<</Length " + str(len(body)).encode() + b">>stream\n" \
                 + body + b"endstream"
        add(stream)

    # Font object
    add(b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>")

    # Assemble the file with an xref table.
    out = bytearray()
    out += b"%PDF-1.4\n%\xE2\xE3\xCF\xD3\n"  # binary marker
    offsets = [0]  # 1-indexed offsets; entry 0 is the free entry
    for idx, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{idx} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += b"xref\n"
    out += f"0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += b"trailer\n"
    out += f"<</Size {len(objects) + 1}/Root 1 0 R>>\n".encode()
    out += b"startxref\n"
    out += f"{xref_pos}\n".encode()
    out += b"%%EOF"

    out_path.write_bytes(bytes(out))
    return out_path


_OPF_TEMPLATE = """<?xml version='1.0' encoding='utf-8'?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">test-book</dc:identifier>
    <dc:title>Tiny Book</dc:title>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    {manifest}
  </manifest>
  <spine>
    {spine}
  </spine>
</package>
"""

_CONTAINER_XML = b"""<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""


def make_tiny_epub(out_path: Path, sections: list[tuple[str, list[str]]]) -> Path:
    """Write a minimal EPUB.

    ``sections`` is a list of ``(filename, paragraphs)`` tuples. Each
    paragraph becomes a ``<p>`` block in the corresponding XHTML file.
    """
    if not sections:
        raise ValueError("need at least one section")

    manifest_items: list[str] = []
    spine_items: list[str] = []
    files: list[tuple[str, bytes]] = []
    for i, (name, paras) in enumerate(sections):
        item_id = f"chap{i}"
        manifest_items.append(
            f'<item id="{item_id}" href="{name}" '
            f'media-type="application/xhtml+xml"/>'
        )
        spine_items.append(f'<itemref idref="{item_id}"/>')
        body = "\n".join(f"<p>{p}</p>" for p in paras)
        xhtml = textwrap.dedent(f"""\
            <?xml version='1.0' encoding='utf-8'?>
            <!DOCTYPE html>
            <html xmlns="http://www.w3.org/1999/xhtml"><head><title>{name}</title></head>
            <body>
            {body}
            </body></html>
            """).encode("utf-8")
        files.append((f"OEBPS/{name}", xhtml))

    opf = _OPF_TEMPLATE.format(
        manifest="\n    ".join(manifest_items),
        spine="\n    ".join(spine_items),
    ).encode("utf-8")

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # mimetype must be first and stored without compression for spec
        # compliance — pypdfium2/lxml don't care for our tests but other
        # EPUB readers do.
        zi = zipfile.ZipInfo("mimetype")
        zi.compress_type = zipfile.ZIP_STORED
        zf.writestr(zi, "application/epub+zip")
        zf.writestr("META-INF/container.xml", _CONTAINER_XML)
        zf.writestr("OEBPS/content.opf", opf)
        for name, content in files:
            zf.writestr(name, content)
    return out_path


# -- Fixtures ---------------------------------------------------------------


def _matched_pages() -> list[str]:
    """Six pages whose text appears verbatim in the matching EPUB."""
    return [
        "Chapter one opens the discussion of widgets and their many uses today.",
        "Section two explores the historical context of widget manufacturing in detail.",
        "Page three contains technical specifications and standardized testing procedures rigorously.",
        "Chapter four covers maintenance procedures and recommended service intervals annually.",
        "Section five tabulates pricing data across vendors and procurement regions worldwide.",
        "Concluding remarks summarize widget life cycles and propose future research directions.",
    ]


@pytest.fixture
def tiny_pdf(tmp_path: Path) -> Path:
    return make_tiny_pdf(tmp_path / "tiny.pdf", _matched_pages())


@pytest.fixture
def tiny_epub(tmp_path: Path) -> Path:
    pages = _matched_pages()
    # Split into two XHTML files so we exercise the "search every member"
    # path. First file covers first three pages, second covers the rest.
    return make_tiny_epub(
        tmp_path / "tiny.epub",
        [
            ("chapter1.xhtml", pages[:3]),
            ("chapter2.xhtml", pages[3:]),
        ],
    )


@pytest.fixture
def tiny_pair(tiny_pdf: Path, tiny_epub: Path) -> tuple[Path, Path]:
    return tiny_pdf, tiny_epub
