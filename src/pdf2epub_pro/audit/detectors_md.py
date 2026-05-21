"""Markdown-level defect detectors.

Each detector subclasses :class:`MarkdownDetector`, declares a unique
``name`` + ``description``, and yields :class:`Finding` objects from
``run(path)``.  Detectors are auto-registered via ``@register_md_detector``.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from .framework import Finding, MarkdownDetector, register_md_detector


# -- Shared helpers ---------------------------------------------------------
def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def _in_fence(lines: list[str], idx: int) -> bool:
    """Return True if line ``idx`` is inside a ```` ``` ```` fenced block."""
    open_count = 0
    for i, line in enumerate(lines[:idx]):
        if line.lstrip().startswith("```"):
            open_count += 1
    return open_count % 2 == 1


def _iter_fences(lines: list[str]):
    """Yield ``(start, end, body_lines)`` triples for each fenced code block.

    ``start`` and ``end`` are 0-indexed line numbers of the opening / closing
    fence; ``body_lines`` is the slice between them (exclusive).
    """
    open_idx: int | None = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            if open_idx is None:
                open_idx = i
            else:
                yield open_idx, i, lines[open_idx + 1:i]
                open_idx = None


# -- 1. SLURM / shebang directive H1 ---------------------------------------
@register_md_detector
class SlurmDirectiveHeadingDetector(MarkdownDetector):
    """Catch ``#SBATCH ...`` / ``#PBS ...`` / ``#!/...`` interpreted as headings.

    ``tidy`` escapes these via the fenced-code promotion, so any survivor
    here means the pipeline missed one.
    """
    name = "slurm_directive_heading"
    description = "Shell/SLURM directive at column 0 looks like a markdown H1"
    default_severity = "error"

    _PAT = re.compile(r"^(#SBATCH\s|#PBS\s|#!\s*/)")

    def run(self, path: Path) -> Iterable[Finding]:
        lines = _read_lines(path)
        for i, line in enumerate(lines):
            if _in_fence(lines, i):
                continue
            if self._PAT.match(line):
                yield Finding(
                    detector=self.name,
                    severity=self.default_severity,
                    file=str(path),
                    line=i + 1,
                    message="shell/SLURM directive will render as a top-level heading",
                    snippet=line.rstrip(),
                )


# -- 2. Code-fence false positive (prose in tiny fence) --------------------
@register_md_detector
class CodeFenceFalsePositiveDetector(MarkdownDetector):
    """A short fenced block (<5 lines) that has no code-like punctuation is
    almost certainly Trafilatura wrapping a sentence in ``<code>``.
    """
    name = "code_fence_false_positive"
    description = "Tiny fenced block contains grammatical prose, not code"
    default_severity = "info"

    _CODE_MARKERS = re.compile(r"[={};<>]|\bfunction\b|\bdef\b|\bclass\b")

    def run(self, path: Path) -> Iterable[Finding]:
        lines = _read_lines(path)
        for start, end, body in _iter_fences(lines):
            if not body or len(body) >= 5:
                continue
            joined = "\n".join(body).strip()
            if not joined:
                continue
            if self._CODE_MARKERS.search(joined):
                continue
            # Must look like prose: contains at least 3 ASCII words and a space.
            words = re.findall(r"[A-Za-z]{2,}", joined)
            if len(words) < 3:
                continue
            yield Finding(
                detector=self.name,
                severity=self.default_severity,
                file=str(path),
                line=start + 1,
                message=f"fenced block (len={len(body)}) reads as prose",
                snippet=joined[:160],
            )


# -- 3. Code-fence missing (oversized inline backtick span) ---------------
@register_md_detector
class CodeFenceMissingDetector(MarkdownDetector):
    r"""Inline ``\`...\``` span >200 chars OR spans multiple lines.

    These almost always need to be promoted to fenced blocks.
    """
    name = "code_fence_missing"
    description = "Long or multiline inline backtick span — should be a fenced block"
    default_severity = "warn"

    _LONG_INLINE = re.compile(r"`([^`\n]{200,})`")

    def run(self, path: Path) -> Iterable[Finding]:
        lines = _read_lines(path)
        text = "\n".join(lines)
        for m in self._LONG_INLINE.finditer(text):
            # Convert offset → line number.
            line_no = text.count("\n", 0, m.start()) + 1
            yield Finding(
                detector=self.name,
                severity=self.default_severity,
                file=str(path),
                line=line_no,
                message=f"inline backtick span is {len(m.group(1))} chars",
                snippet=m.group(0)[:160] + ("..." if len(m.group(0)) > 160 else ""),
            )
        # Multi-line spans: scan for an unmatched backtick on one line whose
        # partner appears later (must not be inside a fenced block).
        in_fence = False
        for i, line in enumerate(lines):
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            # Count single backticks (ignore parts of `` `` `` triplets that
            # already opened a fence — handled above).
            ticks = line.count("`")
            if ticks % 2 == 1:
                # Look ahead for a closing single backtick within next 5 lines.
                for j in range(i + 1, min(i + 6, len(lines))):
                    if "`" in lines[j] and not lines[j].lstrip().startswith("```"):
                        yield Finding(
                            detector=self.name,
                            severity=self.default_severity,
                            file=str(path),
                            line=i + 1,
                            message=f"inline backtick span spans {j - i + 1} lines",
                            snippet=lines[i].rstrip()[:160],
                        )
                        break


# -- 4. Image-link with AWS docs base --------------------------------------
@register_md_detector
class AwsImageLinkDetector(MarkdownDetector):
    """``![alt](https://docs.aws.amazon.com/...)`` — images should be local."""
    name = "aws_image_link"
    description = "Image src points at docs.aws.amazon.com instead of a local artifact"
    default_severity = "error"

    _PAT = re.compile(r"!\[[^\]]*\]\((https?://docs\.aws\.amazon\.com/[^)\s]+)\)")

    def run(self, path: Path) -> Iterable[Finding]:
        lines = _read_lines(path)
        for i, line in enumerate(lines):
            for m in self._PAT.finditer(line):
                yield Finding(
                    detector=self.name,
                    severity=self.default_severity,
                    file=str(path),
                    line=i + 1,
                    message="image src is an AWS docs URL, not a local artifact",
                    snippet=m.group(0)[:160],
                )


# -- 5. Nested-bracket relative link still relative ------------------------
@register_md_detector
class NestedBracketUnresolvedLinkDetector(MarkdownDetector):
    """``[[X.N] label](./foo.html)`` after the pipeline ran.

    The absolutize regex must accept one nested level of ``[...]``; if the
    link is still relative (``./foo.html``), the bug regressed.
    """
    name = "nested_bracket_unresolved_link"
    description = "Nested-bracket link text + relative href escaped absolutization"
    default_severity = "error"

    _PAT = re.compile(
        r"\[\s*\[[^\]]+\][^\]]*\]\((\.{0,2}/[^)\s]+|[A-Za-z0-9_\-]+\.html[^)\s]*)\)"
    )

    def run(self, path: Path) -> Iterable[Finding]:
        lines = _read_lines(path)
        for i, line in enumerate(lines):
            for m in self._PAT.finditer(line):
                yield Finding(
                    detector=self.name,
                    severity=self.default_severity,
                    file=str(path),
                    line=i + 1,
                    message="nested-bracket link is still relative",
                    snippet=m.group(0)[:160],
                )


# -- 6. Mojibake patterns --------------------------------------------------
@register_md_detector
class MojibakeDetector(MarkdownDetector):
    """Detect cp1252-decoded UTF-8 artifacts."""
    name = "mojibake"
    description = "cp1252-decoded UTF-8 artifacts (ï¬, â€, Ã¨, Â§, …)"
    default_severity = "warn"

    # Order matters: more specific multi-char sequences first.
    _PATTERNS = (
        "ï¬", "â€", "Ã¨", "Ã©", "Ã¢", "Ã¤", "Â§", "Â°", "Â·", "Ã±", "Ã¼",
    )

    def run(self, path: Path) -> Iterable[Finding]:
        lines = _read_lines(path)
        for i, line in enumerate(lines):
            for pat in self._PATTERNS:
                idx = line.find(pat)
                if idx != -1:
                    yield Finding(
                        detector=self.name,
                        severity=self.default_severity,
                        file=str(path),
                        line=i + 1,
                        message=f"mojibake sequence {pat!r} present",
                        snippet=line[max(0, idx - 20):idx + 40].rstrip(),
                    )
                    break  # one finding per line is enough


# -- 7. Orphan page number -------------------------------------------------
@register_md_detector
class OrphanPageNumberDetector(MarkdownDetector):
    """Standalone 1-3 digit line with blank neighbours.

    ``tidy.strip_orphan_page_numbers`` removes these; survivor = regression.
    """
    name = "orphan_page_number"
    description = "Bare 1-3 digit line surrounded by blanks (page number residue)"
    default_severity = "warn"

    _NUM = re.compile(r"^\s*\d{1,3}\s*$")

    def run(self, path: Path) -> Iterable[Finding]:
        lines = _read_lines(path)
        for i, line in enumerate(lines):
            if not self._NUM.match(line):
                continue
            prev = lines[i - 1].strip() if i > 0 else ""
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if prev == "" and nxt == "":
                yield Finding(
                    detector=self.name,
                    severity=self.default_severity,
                    file=str(path),
                    line=i + 1,
                    message="bare digit line with blank neighbours",
                    snippet=line.rstrip(),
                )


# -- 8. Listing-page contamination -----------------------------------------
@register_md_detector
class ListingPageContaminationDetector(MarkdownDetector):
    """5+ consecutive ``label .... NN`` dotted-leader TOC lines."""
    name = "listing_page_contamination"
    description = "Dotted-leader TOC residue (label ........ page)"
    default_severity = "warn"

    _PAT = re.compile(r"^.+?\s*\.{4,}\s*\d+\s*$")

    def run(self, path: Path) -> Iterable[Finding]:
        lines = _read_lines(path)
        run_start: int | None = None
        run_len = 0
        for i, line in enumerate(lines + [""]):
            if self._PAT.match(line):
                if run_start is None:
                    run_start = i
                run_len += 1
            else:
                if run_len >= 5 and run_start is not None:
                    yield Finding(
                        detector=self.name,
                        severity=self.default_severity,
                        file=str(path),
                        line=run_start + 1,
                        message=f"{run_len} consecutive dotted-leader lines",
                        snippet=lines[run_start].rstrip()[:160],
                    )
                run_start = None
                run_len = 0


# -- 9. Hyphen-break artifact (compound rejoin) ----------------------------
@register_md_detector
class HyphenBreakArtifactDetector(MarkdownDetector):
    """``word- word`` where the joined form is a known compound."""
    name = "hyphen_break_artifact"
    description = "Hyphen-then-space split of a known compound word"
    default_severity = "warn"

    # Tiny known list — keeps false-positive rate down; extend as audit surfaces more.
    _COMPOUNDS = {
        "cloud-based", "cloud-native", "cost-effective", "cross-account",
        "cross-region", "data-driven", "end-to-end", "event-driven",
        "fault-tolerant", "fine-grained", "high-availability", "long-running",
        "low-latency", "machine-readable", "multi-account", "multi-region",
        "non-production", "on-demand", "on-premises", "real-time",
        "role-based", "self-service", "short-term", "third-party",
        "time-based", "well-architected", "well-known",
    }

    def run(self, path: Path) -> Iterable[Finding]:
        lines = _read_lines(path)
        pat = re.compile(r"\b([A-Za-z]+)-\s+([a-z]+)\b")
        for i, line in enumerate(lines):
            for m in pat.finditer(line):
                joined = f"{m.group(1).lower()}-{m.group(2).lower()}"
                if joined in self._COMPOUNDS:
                    yield Finding(
                        detector=self.name,
                        severity=self.default_severity,
                        file=str(path),
                        line=i + 1,
                        message=f"line-broken compound {joined!r} not re-joined",
                        snippet=line.strip()[:160],
                    )


# -- 10. Compound un-glue regression --------------------------------------
@register_md_detector
class CompoundUnglueRegressionDetector(MarkdownDetector):
    """Bare glued tokens (``realtime``, ``wellarchitected`` …) in prose.

    Sentinels for ``un_glue_compounds`` failing or being bypassed.
    """
    name = "compound_unglue_regression"
    description = "Glued compound token outside URL context"
    default_severity = "warn"

    _TOKENS = (
        "realtime", "thirdparty", "finegrained", "costeffective",
        "faulttolerant", "wellarchitected",
    )

    def run(self, path: Path) -> Iterable[Finding]:
        lines = _read_lines(path)
        pat = re.compile(r"(?<![/\\.\-])\b(" + "|".join(self._TOKENS) + r")\b(?![/\\.\-])")
        for i, line in enumerate(lines):
            for m in pat.finditer(line):
                # Skip if inside an obvious URL/path slice.
                # Cheap guard: a `/` exists within 20 chars on either side.
                window = line[max(0, m.start() - 20):m.end() + 20]
                if "://" in window or "/" + m.group(1) in window or m.group(1) + "/" in window:
                    continue
                yield Finding(
                    detector=self.name,
                    severity=self.default_severity,
                    file=str(path),
                    line=i + 1,
                    message=f"glued compound {m.group(1)!r} in prose",
                    snippet=line.strip()[:160],
                )


# -- 11. H1 explosion ------------------------------------------------------
@register_md_detector
class H1ExplosionDetector(MarkdownDetector):
    """More than ``threshold`` H1s (AWS WAF = 6 pillars + title + appendix)."""
    name = "h1_explosion"
    description = "Too many H1 headings — pillar promotion likely over-fired"
    default_severity = "warn"
    threshold = 8

    def run(self, path: Path) -> Iterable[Finding]:
        lines = _read_lines(path)
        h1s = [(i + 1, l) for i, l in enumerate(lines) if l.startswith("# ")]
        if len(h1s) > self.threshold:
            # Single roll-up finding; snippet shows first/last.
            sample = " / ".join(l.strip() for _, l in h1s[:3])
            yield Finding(
                detector=self.name,
                severity=self.default_severity,
                file=str(path),
                line=h1s[0][0],
                message=f"{len(h1s)} H1 headings (threshold {self.threshold})",
                snippet=sample[:200],
                extra={"count": len(h1s), "threshold": self.threshold},
            )


# -- 12. Bullet-as-H2 -----------------------------------------------------
@register_md_detector
class BulletAsHeadingDetector(MarkdownDetector):
    """``## ·`` / ``## •`` / ``## ●`` — bullet glyph promoted to heading.

    ``tidy.demote_subsections_aws`` turns these into list items; survivors mean
    a non-AWS ruleset or a pattern miss.
    """
    name = "bullet_as_heading"
    description = "Bullet glyph (·, •, ●) used as the entire heading text"
    default_severity = "error"

    _PAT = re.compile(r"^#{1,6}\s+[·•●▪◦・]\s*$")

    def run(self, path: Path) -> Iterable[Finding]:
        lines = _read_lines(path)
        for i, line in enumerate(lines):
            if self._PAT.match(line):
                yield Finding(
                    detector=self.name,
                    severity=self.default_severity,
                    file=str(path),
                    line=i + 1,
                    message="heading is a bare bullet glyph",
                    snippet=line.rstrip(),
                )


# -- 13. Empty / punctuation-only heading ---------------------------------
@register_md_detector
class EmptyHeadingDetector(MarkdownDetector):
    """``# `` (empty) or ``# -*-*`` (punctuation only)."""
    name = "empty_heading"
    description = "Heading with no alphanumeric content"
    default_severity = "error"

    _EMPTY = re.compile(r"^#+\s*$")
    _PUNCT_ONLY = re.compile(r"^#+\s+[-*–_•·●▪◦・\W]+\s*$")
    _HAS_ALNUM = re.compile(r"[A-Za-z0-9一-鿿]")

    def run(self, path: Path) -> Iterable[Finding]:
        lines = _read_lines(path)
        for i, line in enumerate(lines):
            if self._EMPTY.match(line):
                yield Finding(
                    detector=self.name,
                    severity=self.default_severity,
                    file=str(path),
                    line=i + 1,
                    message="heading has no text",
                    snippet=line.rstrip() or "(empty)",
                )
            elif line.startswith("#") and self._PUNCT_ONLY.match(line) and not self._HAS_ALNUM.search(line):
                yield Finding(
                    detector=self.name,
                    severity=self.default_severity,
                    file=str(path),
                    line=i + 1,
                    message="heading contains only punctuation/bullet glyphs",
                    snippet=line.rstrip(),
                )


# -- 14. URL with backslashes ---------------------------------------------
@register_md_detector
class UrlBackslashDetector(MarkdownDetector):
    """``http(s)://...\\...`` — Windows path slipped into a URL."""
    name = "url_backslash"
    description = "Backslash character inside an http(s) URL"
    default_severity = "error"

    _PAT = re.compile(r"\bhttps?://[^\s)]*\\[^\s)]*")

    def run(self, path: Path) -> Iterable[Finding]:
        lines = _read_lines(path)
        for i, line in enumerate(lines):
            for m in self._PAT.finditer(line):
                yield Finding(
                    detector=self.name,
                    severity=self.default_severity,
                    file=str(path),
                    line=i + 1,
                    message="URL contains backslash(es)",
                    snippet=m.group(0)[:160],
                )
