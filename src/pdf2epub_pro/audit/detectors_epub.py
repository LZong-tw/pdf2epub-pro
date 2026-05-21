"""EPUB-level defect detectors.

Each detector subclasses :class:`EpubDetector` and opens the .epub as a zip,
walks its xhtml spine items, and yields findings.  We use stdlib only
(``zipfile`` + ``html.parser``) so the audit CLI has no extra deps.
"""
from __future__ import annotations

import re
import zipfile
from collections import Counter, defaultdict
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

from .framework import EpubDetector, Finding, register_epub_detector


# -- Shared parsing helpers -------------------------------------------------
def _xhtml_members(zf: zipfile.ZipFile) -> list[str]:
    """List names inside the EPUB that look like content xhtml/html files."""
    return sorted(
        n for n in zf.namelist()
        if n.lower().endswith((".xhtml", ".html", ".htm"))
        and not n.lower().endswith("toc.ncx")
    )


class _CollectingParser(HTMLParser):
    """Walks a doc and accumulates: ids, hrefs, headings, body-text presence."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids: list[tuple[str, int]] = []         # (id_value, lineno)
        self.hrefs: list[tuple[str, int]] = []        # (href, lineno)
        self.headings: list[tuple[int, str, int]] = []  # (level, text, lineno)
        self._current_heading: tuple[int, list[str], int] | None = None
        self.body_text_len: int = 0
        self._in_body = False
        self._in_script_or_style = False

    def handle_starttag(self, tag, attrs):
        ad = dict(attrs)
        line = self.getpos()[0]
        if tag == "body":
            self._in_body = True
        if tag in ("script", "style"):
            self._in_script_or_style = True
        if "id" in ad and ad["id"] is not None:
            self.ids.append((ad["id"], line))
        if tag == "a" and "href" in ad and ad["href"] is not None:
            self.hrefs.append((ad["href"], line))
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._current_heading = (int(tag[1]), [], line)

    def handle_endtag(self, tag):
        if tag == "body":
            self._in_body = False
        if tag in ("script", "style"):
            self._in_script_or_style = False
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6") and self._current_heading:
            level, parts, line = self._current_heading
            self.headings.append((level, "".join(parts).strip(), line))
            self._current_heading = None

    def handle_data(self, data):
        if self._current_heading:
            self._current_heading[1].append(data)
        if self._in_body and not self._in_script_or_style:
            stripped = data.strip()
            if stripped:
                self.body_text_len += len(stripped)


def _parse_member(zf: zipfile.ZipFile, name: str) -> _CollectingParser:
    parser = _CollectingParser()
    try:
        raw = zf.read(name).decode("utf-8", errors="replace")
    except KeyError:
        return parser
    try:
        parser.feed(raw)
    except Exception:
        # html.parser is lenient — but guard against pathological input.
        pass
    return parser


# -- 15. Invalid IDs --------------------------------------------------------
_VALID_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-:.]*$")


@register_epub_detector
class InvalidIdDetector(EpubDetector):
    """``id="X"`` where X starts with a digit or contains whitespace/punctuation."""
    name = "invalid_id"
    description = "HTML id starts with a digit or contains illegal characters"
    default_severity = "error"

    def run(self, path: Path) -> Iterable[Finding]:
        with zipfile.ZipFile(path) as zf:
            for member in _xhtml_members(zf):
                parser = _parse_member(zf, member)
                for id_val, line in parser.ids:
                    if not _VALID_ID_RE.match(id_val):
                        yield Finding(
                            detector=self.name,
                            severity=self.default_severity,
                            file=member,
                            line=line,
                            message=f"id {id_val!r} is not a valid HTML id",
                            snippet=id_val,
                        )


# -- 16. Cross-file duplicate IDs ------------------------------------------
@register_epub_detector
class DuplicateIdDetector(EpubDetector):
    """Same ``id`` appearing in 2+ xhtml files (or 2+ times in one file)."""
    name = "duplicate_id"
    description = "id value appears in multiple locations across the EPUB"
    default_severity = "error"

    def run(self, path: Path) -> Iterable[Finding]:
        seen: dict[str, list[tuple[str, int]]] = defaultdict(list)
        with zipfile.ZipFile(path) as zf:
            for member in _xhtml_members(zf):
                parser = _parse_member(zf, member)
                for id_val, line in parser.ids:
                    seen[id_val].append((member, line))
        for id_val, locations in seen.items():
            if len(locations) > 1:
                files = sorted({m for m, _ in locations})
                # Yield on the first occurrence; list others in extra.
                first_file, first_line = locations[0]
                yield Finding(
                    detector=self.name,
                    severity=self.default_severity,
                    file=first_file,
                    line=first_line,
                    message=f"id {id_val!r} appears {len(locations)} times in {len(files)} file(s)",
                    snippet=id_val,
                    extra={"locations": [list(loc) for loc in locations]},
                )


# -- 17. Broken internal anchor --------------------------------------------
@register_epub_detector
class BrokenInternalAnchorDetector(EpubDetector):
    """``href="X.xhtml#frag"`` whose ``#frag`` doesn't resolve in the target."""
    name = "broken_internal_anchor"
    description = "Internal anchor href points at a missing fragment id"
    default_severity = "error"

    def run(self, path: Path) -> Iterable[Finding]:
        with zipfile.ZipFile(path) as zf:
            members = _xhtml_members(zf)
            id_index: dict[str, set[str]] = {}
            href_records: list[tuple[str, str, int]] = []  # (source, href, line)
            for member in members:
                parser = _parse_member(zf, member)
                id_index[member] = {v for v, _ in parser.ids}
                for href, line in parser.hrefs:
                    href_records.append((member, href, line))

            def _normalize_target(source: str, href: str) -> str | None:
                if "#" not in href:
                    return None
                target, _, _ = href.partition("#")
                if not target:
                    return source
                # Resolve relative to the source xhtml's directory.
                src_dir = source.rsplit("/", 1)[0] if "/" in source else ""
                # Simple path join + ".." collapse.
                pieces = (src_dir + "/" + target if src_dir else target).split("/")
                stack: list[str] = []
                for p in pieces:
                    if p == "..":
                        if stack:
                            stack.pop()
                    elif p and p != ".":
                        stack.append(p)
                return "/".join(stack)

            for source, href, line in href_records:
                if "://" in href or href.startswith(("mailto:", "tel:")):
                    continue
                if "#" not in href:
                    continue
                target_path = _normalize_target(source, href)
                _, _, frag = href.partition("#")
                if not frag:
                    continue
                if target_path not in id_index:
                    # Unknown file — treat as broken-anchor candidate but mark info-level
                    yield Finding(
                        detector=self.name,
                        severity="warn",
                        file=source,
                        line=line,
                        message=f"anchor target file {target_path!r} not in EPUB",
                        snippet=href[:160],
                    )
                    continue
                if frag not in id_index[target_path]:
                    yield Finding(
                        detector=self.name,
                        severity=self.default_severity,
                        file=source,
                        line=line,
                        message=f"fragment #{frag} not found in {target_path}",
                        snippet=href[:160],
                    )


# -- 18. Relative href skeleton --------------------------------------------
@register_epub_detector
class RelativeHrefSkeletonDetector(EpubDetector):
    """``href="./X.html"`` — should have been absolutized in the markdown step."""
    name = "relative_href_skeleton"
    description = "href is a bare relative ./*.html path — absolutize regex missed it"
    default_severity = "warn"

    _PAT = re.compile(r"^\.{0,2}/[A-Za-z0-9_\-]+\.html(?:[#?].*)?$")

    def run(self, path: Path) -> Iterable[Finding]:
        with zipfile.ZipFile(path) as zf:
            for member in _xhtml_members(zf):
                parser = _parse_member(zf, member)
                for href, line in parser.hrefs:
                    if self._PAT.match(href):
                        yield Finding(
                            detector=self.name,
                            severity=self.default_severity,
                            file=member,
                            line=line,
                            message="href is a relative .html skeleton",
                            snippet=href[:160],
                        )


# -- 19. Heading depth jump ------------------------------------------------
@register_epub_detector
class HeadingDepthJumpDetector(EpubDetector):
    """Heading skipping levels within one spine item.

    Many real-world publications skip heading levels intentionally — e.g.
    section→subsection without an intermediate group heading, or a chunk
    file that begins at H2 because the H1 was already extracted into the
    EPUB chapter metadata by the splitter.  Flagging every single skip as
    a warning drowns out the genuine defects (real structural mis-nesting,
    typically skips of 3+ levels).

    Tunable thresholds via constructor:
      • silent_max_skip (default 1) — skips ≤ this are not reported
      • warn_min_skip   (default 3) — skips ≥ this are warn; in between is info

    The defaults are deliberately permissive ("any 2-level skip is just
    informational") because aggressive defaults flood reports on real
    books.  Lower silent_max_skip to 0 + warn_min_skip to 2 for strict
    semantic-HTML enforcement.
    """
    name = "heading_depth_jump"
    description = (
        "Heading depth skips levels (info by default, warn at 3+ skipped)"
    )
    default_severity = "warn"

    silent_max_skip = 1
    warn_min_skip = 3

    def run(self, path: Path) -> Iterable[Finding]:
        with zipfile.ZipFile(path) as zf:
            for member in _xhtml_members(zf):
                parser = _parse_member(zf, member)
                last_level: int | None = None
                for level, text, line in parser.headings:
                    if last_level is not None and level > last_level:
                        skipped = level - last_level - 1
                        if skipped <= self.silent_max_skip:
                            last_level = level
                            continue
                        severity = (
                            "warn" if skipped >= self.warn_min_skip else "info"
                        )
                        yield Finding(
                            detector=self.name,
                            severity=severity,
                            file=member,
                            line=line,
                            message=(
                                f"H{last_level} → H{level} skips {skipped} levels"
                            ),
                            snippet=(text or "")[:160],
                            extra={"from": last_level, "to": level,
                                   "skipped": skipped},
                        )
                    last_level = level


# -- 20. Empty spine item --------------------------------------------------
@register_epub_detector
class EmptySpineItemDetector(EpubDetector):
    """Spine xhtml whose body is empty (only whitespace or empty headings)."""
    name = "empty_spine_item"
    description = "Spine xhtml has no body text content"
    default_severity = "warn"

    # Skip pure metadata files: nav, ncx, cover.
    _SKIP = re.compile(r"(?:nav|toc|cover|titlepage|copyright)", re.IGNORECASE)

    def run(self, path: Path) -> Iterable[Finding]:
        with zipfile.ZipFile(path) as zf:
            for member in _xhtml_members(zf):
                if self._SKIP.search(member.rsplit("/", 1)[-1]):
                    continue
                parser = _parse_member(zf, member)
                if parser.body_text_len == 0:
                    yield Finding(
                        detector=self.name,
                        severity=self.default_severity,
                        file=member,
                        line=None,
                        message="spine item has no body text",
                        snippet=None,
                    )


# Counter is imported but exposing it from this module isn't useful;
# silence the unused warning by re-exporting under __all__.
__all__ = [
    "BrokenInternalAnchorDetector",
    "DuplicateIdDetector",
    "EmptySpineItemDetector",
    "HeadingDepthJumpDetector",
    "InvalidIdDetector",
    "RelativeHrefSkeletonDetector",
]

_ = Counter  # keep import — used by future detectors; explicit no-op so lint stays quiet
