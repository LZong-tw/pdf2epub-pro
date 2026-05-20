"""Locate external tools (docling, ebook-convert, ebook-meta) cross-platform."""
import os
import shutil
from pathlib import Path

_CALIBRE_GUESSES = [
    Path(r"C:\Program Files\Calibre2"),
    Path(r"C:\Program Files (x86)\Calibre2"),
    Path("/Applications/calibre.app/Contents/MacOS"),
    Path("/opt/calibre"),
    Path("/usr/bin"),
]


def _resolve(env_var: str, cmd: str, calibre_subdir: bool = False) -> str:
    override = os.environ.get(env_var)
    if override:
        return override
    found = shutil.which(cmd)
    if found:
        return found
    if calibre_subdir:
        for base in _CALIBRE_GUESSES:
            for ext in ("", ".exe"):
                p = base / (cmd + ext)
                if p.exists():
                    return str(p)
    raise FileNotFoundError(
        f"could not find {cmd!r} (set {env_var} or install on PATH)"
    )


def docling_path() -> str:
    return _resolve("PDF2EPUB_DOCLING", "docling")


def ebook_convert_path() -> str:
    return _resolve("PDF2EPUB_EBOOK_CONVERT", "ebook-convert", calibre_subdir=True)


def ebook_meta_path() -> str:
    return _resolve("PDF2EPUB_EBOOK_META", "ebook-meta", calibre_subdir=True)


def share_dir() -> Path:
    """Locate the bundled `share/` dir relative to the installed package."""
    here = Path(__file__).resolve().parent
    for candidate in [here.parent.parent / "share", here / "share", here.parent / "share"]:
        if candidate.is_dir():
            return candidate
    return here.parent.parent / "share"
