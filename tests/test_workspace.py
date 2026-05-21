"""Tests for the build-dir / cache management helpers."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from pdf2epub_pro import workspace


def test_default_build_root_honours_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("PDF2EPUB_BUILD_ROOT", str(tmp_path / "explicit"))
    assert workspace.default_build_root() == (tmp_path / "explicit").resolve()


def test_default_build_root_windows_uses_localappdata(monkeypatch, tmp_path):
    monkeypatch.delenv("PDF2EPUB_BUILD_ROOT", raising=False)
    monkeypatch.setattr(workspace.platform, "system", lambda: "Windows")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    expected = tmp_path / "appdata" / "pdf2epub-pro" / "builds"
    assert workspace.default_build_root() == expected


def test_default_build_root_posix_uses_xdg_when_set(monkeypatch, tmp_path):
    monkeypatch.delenv("PDF2EPUB_BUILD_ROOT", raising=False)
    monkeypatch.setattr(workspace.platform, "system", lambda: "Linux")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    expected = tmp_path / "xdg" / "pdf2epub-pro" / "builds"
    assert workspace.default_build_root() == expected


def test_default_build_root_posix_falls_back_to_dotcache(monkeypatch):
    monkeypatch.delenv("PDF2EPUB_BUILD_ROOT", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr(workspace.platform, "system", lambda: "Linux")
    expected = Path.home() / ".cache" / "pdf2epub-pro" / "builds"
    assert workspace.default_build_root() == expected


def test_build_dir_for_creates_and_returns_per_stem_path(tmp_path):
    root = tmp_path / "root"
    p = workspace.build_dir_for("My Book", root=root)
    assert p == root / "My Book"
    assert p.is_dir()


@pytest.mark.parametrize("bad", ["", ".", "..", "  "])
def test_build_dir_for_rejects_dangerous_stem(bad, tmp_path):
    # REGRESSION: empty / dot stems would otherwise resolve to the
    # build root itself or its parent — clean --stem '' must not
    # delete the entire build root.
    with pytest.raises(ValueError):
        workspace.build_dir_for(bad, root=tmp_path)


def test_build_dir_for_sanitizes_path_separators(tmp_path):
    # Stem coming from arbitrary input must not navigate the FS.
    p = workspace.build_dir_for("book/with/slashes", root=tmp_path)
    assert p == tmp_path / "book_with_slashes"
    assert p.parent == tmp_path


def test_list_build_dirs_empty_when_root_missing(tmp_path):
    assert workspace.list_build_dirs(tmp_path / "ghost") == []


def test_list_build_dirs_returns_only_subdirs(tmp_path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "bravo").mkdir()
    (tmp_path / "stray.txt").write_text("not a dir")
    listed = workspace.list_build_dirs(tmp_path)
    assert listed == [tmp_path / "alpha", tmp_path / "bravo"]


def test_clean_build_dirs_specific_stem(tmp_path):
    workspace.build_dir_for("alpha", root=tmp_path)
    workspace.build_dir_for("bravo", root=tmp_path)
    removed = workspace.clean_build_dirs(root=tmp_path, stem="alpha")
    assert removed == [tmp_path / "alpha"]
    # other stem untouched
    assert (tmp_path / "bravo").is_dir()


def test_clean_build_dirs_all_stems(tmp_path):
    workspace.build_dir_for("alpha", root=tmp_path)
    workspace.build_dir_for("bravo", root=tmp_path)
    removed = workspace.clean_build_dirs(root=tmp_path)
    assert set(removed) == {tmp_path / "alpha", tmp_path / "bravo"}
    # build root itself stays (shell-completion stability)
    assert tmp_path.is_dir()


def test_clean_build_dirs_noop_on_missing_root(tmp_path):
    assert workspace.clean_build_dirs(root=tmp_path / "ghost") == []


def test_clean_build_dirs_noop_on_missing_stem(tmp_path):
    workspace.build_dir_for("alpha", root=tmp_path)
    removed = workspace.clean_build_dirs(root=tmp_path, stem="nonexistent")
    assert removed == []
    assert (tmp_path / "alpha").is_dir()
