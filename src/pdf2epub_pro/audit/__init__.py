"""pdf2epub_pro.audit — L2 defect detector catalog.

Each known defect class is a stand-alone detector with tests.  Detectors run
against either a markdown file (pre-Calibre) or the finished EPUB.  The
``pdf2epub-audit`` CLI orchestrates them, prints findings, and exits with a
non-zero code when warnings or errors are present.
"""
from .framework import (
    EpubDetector,
    Finding,
    MarkdownDetector,
    register_epub_detector,
    register_md_detector,
    registered_epub_detectors,
    registered_md_detectors,
    run_all_epub,
    run_all_md,
)
# Importing the detector modules registers them via decorators.
from . import detectors_epub as detectors_epub  # noqa: F401
from . import detectors_md as detectors_md  # noqa: F401

__all__ = [
    "EpubDetector",
    "Finding",
    "MarkdownDetector",
    "register_epub_detector",
    "register_md_detector",
    "registered_epub_detectors",
    "registered_md_detectors",
    "run_all_epub",
    "run_all_md",
]
