"""Workspace + build-dir management.

Intermediate artifacts (Docling raw markdown, tidy/linked/refs stages,
cover JPG, extracted images, etc.) are *reproducible* — they should not
sit beside the final EPUB in whatever directory the user named with
``--output-dir``.  Mixing the two clutters Downloads / Desktop and
makes "where did my book actually land" a guessing game.

This module resolves a tool-managed cache location instead.  Final
EPUBs still go to ``--output-dir`` (user-facing artifact).  Per-stem
intermediate trees go under :func:`default_build_root`, which the CLI
exposes via ``--build-dir`` for overrides.
"""
from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path

_APP_NAME = "pdf2epub-pro"


def default_build_root() -> Path:
    """Return the per-user root for tool-managed intermediate artifacts.

    Resolution order (first hit wins):

    * ``$PDF2EPUB_BUILD_ROOT`` — explicit user override, useful for
      CI / sandboxes that need predictable locations.
    * Windows: ``%LOCALAPPDATA%\\pdf2epub-pro\\builds``.  Falls back
      to ``~/AppData/Local/pdf2epub-pro/builds`` if the env var is
      missing.
    * POSIX with ``$XDG_CACHE_HOME`` set: ``$XDG_CACHE_HOME/pdf2epub-pro/builds``.
    * POSIX fallback: ``~/.cache/pdf2epub-pro/builds``.
    """
    override = os.environ.get("PDF2EPUB_BUILD_ROOT")
    if override:
        return Path(override).expanduser().resolve()

    if platform.system() == "Windows":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / _APP_NAME / "builds"
        return Path.home() / "AppData" / "Local" / _APP_NAME / "builds"

    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg) / _APP_NAME / "builds"
    return Path.home() / ".cache" / _APP_NAME / "builds"


def build_dir_for(stem: str, *, root: Path | None = None) -> Path:
    """Resolve the per-stem build directory (create if missing).

    ``stem`` is typically the PDF filename without extension — but
    this function is paranoid about it: we strip path separators and
    refuse empty / dot-only names so a malformed input never escapes
    into a parent directory.
    """
    cleaned = stem.replace("/", "_").replace("\\", "_").strip()
    if not cleaned or cleaned in (".", ".."):
        raise ValueError(f"invalid stem for build dir: {stem!r}")
    base = (root or default_build_root()) / cleaned
    base.mkdir(parents=True, exist_ok=True)
    return base


def list_build_dirs(root: Path | None = None) -> list[Path]:
    """Enumerate per-stem subdirectories under the build root."""
    base = root or default_build_root()
    if not base.exists():
        return []
    return sorted(p for p in base.iterdir() if p.is_dir())


def clean_build_dirs(*, root: Path | None = None,
                     stem: str | None = None) -> list[Path]:
    """Delete cached build artifacts.

    With ``stem=None`` (default) clears the entire build root.  With
    a specific ``stem`` only that subtree is removed.  Returns the
    list of paths that were actually removed (the caller can print a
    summary).  Never raises on missing inputs — cleaning a non-
    existent cache is a no-op.
    """
    base = root or default_build_root()
    if not base.exists():
        return []

    removed: list[Path] = []
    if stem is None:
        for child in list_build_dirs(base):
            shutil.rmtree(child)
            removed.append(child)
        # Don't remove `base` itself — next build would recreate it
        # but leaving it makes the path stable in shell completion.
        return removed

    target = base / stem
    if target.is_dir():
        shutil.rmtree(target)
        removed.append(target)
    return removed
