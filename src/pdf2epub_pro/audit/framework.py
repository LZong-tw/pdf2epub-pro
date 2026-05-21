"""Detector framework — Finding dataclass, base classes, registry, runners."""
from __future__ import annotations

import abc
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Literal

Severity = Literal["info", "warn", "error"]
_SEVERITY_RANK = {"info": 0, "warn": 1, "error": 2}


@dataclass(frozen=True)
class Finding:
    """One defect emitted by a detector.

    ``file`` / ``line`` / ``xpath`` are all optional so the same shape covers
    markdown findings (file + line), EPUB findings (file = xhtml spine item,
    optional xpath inside it), and global findings (none set).
    """
    detector: str
    severity: Severity
    message: str
    file: str | None = None
    line: int | None = None
    xpath: str | None = None
    snippet: str | None = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def severity_rank(self) -> int:
        return _SEVERITY_RANK[self.severity]


class _BaseDetector(abc.ABC):
    """Shared metadata for both detector kinds."""
    name: str = ""
    description: str = ""
    default_severity: Severity = "warn"

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if not cls.name:
            cls.name = cls.__name__


class MarkdownDetector(_BaseDetector):
    """Detector that inspects a raw markdown file on disk."""

    @abc.abstractmethod
    def run(self, path: Path) -> Iterable[Finding]:
        """Yield Finding objects for ``path``."""


class EpubDetector(_BaseDetector):
    """Detector that inspects a finished ``.epub`` file (zip of xhtml)."""

    @abc.abstractmethod
    def run(self, path: Path) -> Iterable[Finding]:
        """Yield Finding objects for the EPUB at ``path``."""


# -- Registry ---------------------------------------------------------------
_MD_REGISTRY: list[type[MarkdownDetector]] = []
_EPUB_REGISTRY: list[type[EpubDetector]] = []


def register_md_detector(cls: type[MarkdownDetector]) -> type[MarkdownDetector]:
    """Class decorator: register a markdown detector for default runs."""
    if cls not in _MD_REGISTRY:
        _MD_REGISTRY.append(cls)
    return cls


def register_epub_detector(cls: type[EpubDetector]) -> type[EpubDetector]:
    """Class decorator: register an EPUB detector for default runs."""
    if cls not in _EPUB_REGISTRY:
        _EPUB_REGISTRY.append(cls)
    return cls


def registered_md_detectors() -> list[type[MarkdownDetector]]:
    return list(_MD_REGISTRY)


def registered_epub_detectors() -> list[type[EpubDetector]]:
    return list(_EPUB_REGISTRY)


# -- Runners ----------------------------------------------------------------
def _instantiate(detectors):
    return [d() if isinstance(d, type) else d for d in detectors]


def run_all_md(
    path: Path | str,
    detectors: Iterable[type[MarkdownDetector] | MarkdownDetector] | None = None,
) -> list[Finding]:
    """Run all (or supplied) markdown detectors against ``path``."""
    p = Path(path)
    detectors = _instantiate(detectors if detectors is not None else _MD_REGISTRY)
    findings: list[Finding] = []
    for det in detectors:
        try:
            findings.extend(det.run(p))
        except Exception as exc:  # noqa: BLE001 — defect detector must never crash run
            findings.append(Finding(
                detector=getattr(det, "name", det.__class__.__name__),
                severity="error",
                message=f"detector crashed: {exc!r}",
                file=str(p),
            ))
    return findings


def run_all_epub(
    path: Path | str,
    detectors: Iterable[type[EpubDetector] | EpubDetector] | None = None,
) -> list[Finding]:
    """Run all (or supplied) EPUB detectors against ``path``."""
    p = Path(path)
    detectors = _instantiate(detectors if detectors is not None else _EPUB_REGISTRY)
    findings: list[Finding] = []
    for det in detectors:
        try:
            findings.extend(det.run(p))
        except Exception as exc:  # noqa: BLE001
            findings.append(Finding(
                detector=getattr(det, "name", det.__class__.__name__),
                severity="error",
                message=f"detector crashed: {exc!r}",
                file=str(p),
            ))
    return findings


def exit_code_for(findings: Iterable[Finding]) -> int:
    """0 = clean, 1 = warn-only, 2 = any error."""
    worst = 0
    for f in findings:
        if f.severity_rank > worst:
            worst = f.severity_rank
            if worst == 2:
                return 2
    return worst
