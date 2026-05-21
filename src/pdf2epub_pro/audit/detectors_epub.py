"""EPUB-level defect detectors.

Each detector subclasses :class:`EpubDetector` and opens the .epub as a zip,
walks its xhtml spine items, and yields findings.  We use stdlib only
(``zipfile`` + ``html.parser``) so the audit CLI has no extra deps.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
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
        # (alt_or_None, src_or_None, role_or_None, lineno) — alt is None when
        # the attribute is missing entirely, "" when present but empty.
        self.images: list[tuple[str | None, str | None, str | None, int]] = []
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
        if tag == "img":
            self.images.append(
                (ad.get("alt"), ad.get("src"), ad.get("role"), line)
            )
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._current_heading = (int(tag[1]), [], line)

    def handle_startendtag(self, tag, attrs):
        # XHTML self-closing form e.g. <img alt="x" src="y"/>. HTMLParser
        # routes these through their own callback rather than start/end.
        self.handle_starttag(tag, attrs)

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


# -- 21. Empty / missing image alt ------------------------------------------
@register_epub_detector
class ImageAltEmptyDetector(EpubDetector):
    """``<img>`` whose ``alt`` attribute is missing or empty.

    Accessibility: every non-decorative image needs an ``alt``.  We can't
    perfectly tell decorative from informative without a human, so we err
    on flagging.  Two cheap heuristics suppress the most common
    decorative cases:

      * ``role="presentation"`` or ``role="none"`` — the spec's explicit
        decorative marker, treat as intentional.
      * ``src`` ending in a recognizably icon-shaped name (``*-icon.*``,
        ``icons/*``) — best-effort skip; override via subclass if too loose.
    """
    name = "image_alt_empty"
    description = "Image element has empty or missing alt attribute"
    default_severity = "warn"

    _DECORATIVE_ROLES = frozenset({"presentation", "none"})
    _ICON_HINT = re.compile(r"(?:^|/)icons?/|[-_]icon\.[a-z]+$", re.IGNORECASE)

    def run(self, path: Path) -> Iterable[Finding]:
        with zipfile.ZipFile(path) as zf:
            for member in _xhtml_members(zf):
                parser = _parse_member(zf, member)
                for alt, src, role, line in parser.images:
                    if alt is not None and alt != "":
                        continue
                    if role is not None and role.lower() in self._DECORATIVE_ROLES:
                        continue
                    if src is not None and self._ICON_HINT.search(src):
                        continue
                    missing = alt is None
                    yield Finding(
                        detector=self.name,
                        severity=self.default_severity,
                        file=member,
                        line=line,
                        message=(
                            "img has no alt attribute" if missing
                            else "img has empty alt=\"\""
                        ),
                        snippet=src[:160] if src else None,
                        extra={"src": src, "alt_missing": missing},
                    )


# -- 22. Same heading text in many spine items -----------------------------
@register_epub_detector
class HeadingTextDuplicationDetector(EpubDetector):
    """Same heading text appearing across multiple spine items.

    Genuine signal: a heading like "Step 1: Configure" repeated verbatim
    in 5 different chapters usually means the markdown splitter
    fragmented one logical section, or a copy-paste defect.  False-
    positive risk is high for legitimate generic headings ("Overview",
    "Summary", …) so we keep this detector at ``info`` severity and
    expose an allowlist + threshold for tuning.
    """
    name = "heading_text_duplication"
    description = "Same heading text appears in multiple spine items"
    default_severity = "info"

    duplication_threshold = 2
    allowlist = frozenset({
        "Overview",
        "References",
        "Introduction",
        "Summary",
        "Conclusion",
        "Appendix",
        "Glossary",
        "Contents",
        "Table of Contents",
    })

    def run(self, path: Path) -> Iterable[Finding]:
        # text -> { file -> level }
        by_text: dict[str, dict[str, int]] = defaultdict(dict)
        with zipfile.ZipFile(path) as zf:
            for member in _xhtml_members(zf):
                parser = _parse_member(zf, member)
                for level, text, _ in parser.headings:
                    norm = (text or "").strip()
                    if not norm:
                        continue
                    if norm in self.allowlist:
                        continue
                    # First occurrence in this file wins for level reporting.
                    by_text[norm].setdefault(member, level)
        for text, file_levels in sorted(by_text.items()):
            if len(file_levels) >= self.duplication_threshold:
                files = sorted(file_levels.keys())
                # Use the most common heading level seen for this text.
                level_counts = Counter(file_levels.values())
                top_level = level_counts.most_common(1)[0][0]
                yield Finding(
                    detector=self.name,
                    severity=self.default_severity,
                    file=files[0],
                    line=None,
                    message=(
                        f"heading {text!r} appears in {len(files)} files"
                    ),
                    snippet=text[:160],
                    extra={"files": files, "level": top_level},
                )


# -- 23. External-link density --------------------------------------------
@register_epub_detector
class ExternalHrefDensityDetector(EpubDetector):
    """A single spine item with more external ``http(s)://`` hrefs than threshold.

    Useful for spotting docs whose "references" or "see also" appendix has
    swelled past readability, or for catching chunks where a link-fetch
    appendix-merger glued too much content into one xhtml.
    """
    name = "external_href_density"
    description = "Single spine item has more external hrefs than the threshold"
    default_severity = "warn"

    threshold = 50

    _EXTERNAL_RE = re.compile(r"^https?://", re.IGNORECASE)

    def run(self, path: Path) -> Iterable[Finding]:
        with zipfile.ZipFile(path) as zf:
            for member in _xhtml_members(zf):
                parser = _parse_member(zf, member)
                external_count = sum(
                    1 for href, _ in parser.hrefs if self._EXTERNAL_RE.match(href)
                )
                if external_count > self.threshold:
                    yield Finding(
                        detector=self.name,
                        severity=self.default_severity,
                        file=member,
                        line=None,
                        message=(
                            f"{external_count} external hrefs "
                            f"(threshold {self.threshold})"
                        ),
                        snippet=None,
                        extra={"count": external_count,
                               "threshold": self.threshold},
                    )


# -- 24. OPF manifest/spine consistency -----------------------------------
_OPF_NS = {
    "opf": "http://www.idpf.org/2007/opf",
    "container": "urn:oasis:names:tc:opendocument:xmlns:container",
}


def _find_opf_path(zf: zipfile.ZipFile) -> str | None:
    try:
        raw = zf.read("META-INF/container.xml")
    except KeyError:
        return None
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None
    rootfile = root.find(".//container:rootfile", _OPF_NS)
    if rootfile is None:
        # Some packagers omit the namespace; fall back to local-name search.
        for el in root.iter():
            if el.tag.rsplit("}", 1)[-1] == "rootfile":
                rootfile = el
                break
    if rootfile is None:
        return None
    return rootfile.get("full-path")


def _resolve_relative(base_dir: str, href: str) -> str:
    """Join an OPF-relative href into a zip member path, collapsing ``..``."""
    pieces = ((base_dir + "/" + href) if base_dir else href).split("/")
    stack: list[str] = []
    for p in pieces:
        if p == ".." and stack:
            stack.pop()
        elif p and p != ".":
            stack.append(p)
    return "/".join(stack)


@register_epub_detector
class OpfManifestSpineConsistencyDetector(EpubDetector):
    """OPF manifest / spine internal consistency check.

    Verifies three invariants:

      (a) every ``spine`` idref points at a manifest item (no dangling idref),
      (b) every manifest item's ``href`` exists in the zip (no ghost item),
      (c) every xhtml/html file in the zip is in the manifest (no orphan).

    Orphans of class (c) are skipped when the manifest entry for some other
    file flags it with one of the standard auxiliary properties
    (``nav``, ``cover-image``, ``scripted``, ``mathml``, ``svg``) — those
    are intentionally outside the main reading order.  Files matching
    container/OPF/NCX themselves are always exempt.
    """
    name = "opf_manifest_spine_consistency"
    description = (
        "OPF manifest / spine inconsistency (orphan file, missing item, "
        "or broken idref)"
    )
    default_severity = "error"

    _AUX_PROPERTIES = frozenset({
        "nav", "cover-image", "scripted", "mathml", "svg",
    })
    _EXEMPT_NAME_RE = re.compile(
        r"^(?:mimetype|META-INF/.*|.*\.opf|.*\.ncx)$", re.IGNORECASE
    )

    def run(self, path: Path) -> Iterable[Finding]:
        with zipfile.ZipFile(path) as zf:
            opf_path = _find_opf_path(zf)
            if opf_path is None:
                yield Finding(
                    detector=self.name,
                    severity=self.default_severity,
                    file="META-INF/container.xml",
                    message="could not locate OPF rootfile",
                )
                return
            try:
                opf_raw = zf.read(opf_path)
            except KeyError:
                yield Finding(
                    detector=self.name,
                    severity=self.default_severity,
                    file="META-INF/container.xml",
                    message=f"OPF rootfile {opf_path!r} missing from zip",
                )
                return
            try:
                root = ET.fromstring(opf_raw)
            except ET.ParseError as exc:
                yield Finding(
                    detector=self.name,
                    severity=self.default_severity,
                    file=opf_path,
                    message=f"OPF is not well-formed XML: {exc!s}",
                )
                return

            opf_dir = opf_path.rsplit("/", 1)[0] if "/" in opf_path else ""

            # Walk manifest + spine without forcing a single namespace —
            # match by local-name to survive default-namespace omissions.
            manifest_items: dict[str, dict[str, str | None]] = {}
            manifest_hrefs: set[str] = set()  # zip-relative
            spine_idrefs: list[str] = []
            for el in root.iter():
                local = el.tag.rsplit("}", 1)[-1]
                if local == "item" and el.get("id") is not None:
                    item_id = el.get("id") or ""
                    href = el.get("href")
                    props = el.get("properties")
                    if not item_id or href is None:
                        continue
                    zip_path = _resolve_relative(opf_dir, href)
                    manifest_items[item_id] = {
                        "href": href,
                        "zip_path": zip_path,
                        "properties": props,
                    }
                    manifest_hrefs.add(zip_path)
                elif local == "itemref":
                    idref = el.get("idref")
                    if idref:
                        spine_idrefs.append(idref)

            zip_names = set(zf.namelist())

            # (a) broken spine idref
            for idref in spine_idrefs:
                if idref not in manifest_items:
                    yield Finding(
                        detector=self.name,
                        severity=self.default_severity,
                        file=opf_path,
                        message=f"broken spine idref {idref!r}",
                        snippet=idref,
                        extra={"kind": "broken_spine"},
                    )

            # (b) manifest href not in zip
            for item_id, info in manifest_items.items():
                if info["zip_path"] not in zip_names:
                    yield Finding(
                        detector=self.name,
                        severity=self.default_severity,
                        file=opf_path,
                        message=(
                            f"manifest href {info['href']!r} missing from zip"
                        ),
                        snippet=str(info["href"]),
                        extra={"kind": "missing_manifest_target",
                               "id": item_id},
                    )

            # (c) orphan xhtml/html in zip not referenced by manifest.
            # Any manifest item carrying an aux property (nav etc.) is
            # already considered legitimately outside the main spine
            # walk — but it's the ORPHAN we're scanning for, so skip a
            # file only if its own (or any) manifest entry mapping says
            # so.  Cheapest correct way: collect the set of zip paths
            # that any aux-property manifest entry points to and never
            # treat them as orphans.
            aux_zip_paths = {
                info["zip_path"]
                for info in manifest_items.values()
                if info["properties"]
                and any(
                    p in self._AUX_PROPERTIES
                    for p in info["properties"].split()
                )
            }
            for name in sorted(zip_names):
                if not name.lower().endswith((".xhtml", ".html", ".htm")):
                    continue
                if self._EXEMPT_NAME_RE.match(name):
                    continue
                if name in manifest_hrefs:
                    continue
                if name in aux_zip_paths:
                    continue
                yield Finding(
                    detector=self.name,
                    severity=self.default_severity,
                    file=name,
                    message=f"orphan zip file {name!r} not in manifest",
                    snippet=name,
                    extra={"kind": "orphan_file"},
                )


__all__ = [
    "BrokenInternalAnchorDetector",
    "DuplicateIdDetector",
    "EmptySpineItemDetector",
    "ExternalHrefDensityDetector",
    "HeadingDepthJumpDetector",
    "HeadingTextDuplicationDetector",
    "ImageAltEmptyDetector",
    "InvalidIdDetector",
    "OpfManifestSpineConsistencyDetector",
    "RelativeHrefSkeletonDetector",
]

_ = Counter  # keep import — used internally by HeadingTextDuplicationDetector
