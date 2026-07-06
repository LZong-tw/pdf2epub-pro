# pdf2epub-pro — Claude project notes

A public PDF→EPUB conversion pipeline (Docling + Calibre). Repo:
https://github.com/LZong-tw/pdf2epub-pro

## Every bug fix ships with a regression test — non-negotiable

This is a public package — silent regressions are not acceptable.
Running the conversion on a real PDF and eyeballing the output is an
**acceptance check**, not a substitute for an automated test.

The rule: every commit that fixes a defect must add a regression test
in the same commit.  No "I'll add the test later", no "I verified by
hand on the WAF corpus".  Future refactors WILL break unwitnessed
fixes.

How to apply:

- **Same commit** — the failing test (if pre-existing input could
  produce it) plus the fix are paired.  Reviewers should be able to
  `git revert` the fix line and watch the test fail.
- **Smallest possible fixture** — inline string for markdown / xhtml,
  on-the-fly `_build_epub()` for EPUB-level cases.  No on-disk
  corpus dependencies.
- **Name the bug in the test** — `test_X_does_not_Y` or a docstring
  starting with `# REGRESSION:` plus a one-line description of what
  shipped wrong without it.  Future-you needs to know WHY the test
  exists when it gets in the way of a refactor.
- **Commit message lists the new test names** so reviewers can
  quickly verify coverage.
- **Don't trust audit-count parity alone**.  `pdf2epub-audit`
  measures the EPUB output; many defects (broken rendering inside
  `<pre><code>`, dropped image references, fence-blind heading
  splits) don't move the audit needle but DO degrade the artifact.

If you find yourself thinking "this fix is too small / too obvious /
too hard to test" — that's a signal to push harder on the test, not
to skip it.

## This is a public package — generalize

Anyone with arbitrary PDFs can install and use this. Do NOT hardcode
publisher-specific behavior into defaults. When adding rules / detectors:

- Defaults must be conservative (no false positives on diverse content).
- Publisher-specific patterns (currently AWS-dominant in our test corpus)
  belong behind `--ruleset {aws,generic}` or as opt-in flags.
- Thresholds / wordlists must be constructor parameters or CLI flags,
  not magic constants embedded in detector classes.
- Detector / rule descriptions and docstrings must not name a specific
  book or publisher — they describe the *pattern*, not where we first saw it.

## Architecture

Pipeline: `split → tidy → restore-links → fetch-refs → cover → md2epub_{backend}`

- `split.py` — PDF chunking via Docling
- `tidy.py` — markdown cleanup (mojibake, hyphenation, list gaps, …);
  generic rulesets also rebuild the numbered-chapter hierarchy that
  ML parsers flatten to a single heading level
- `restore_links.py` — re-attach lost hyperlinks from PDF annotations
- `fetch_refs.py` — fetch external refs into appendix via trafilatura
- `make_cover.py` — cover images: PDF page-1 rasterization (default,
  `--cover-source`) or PIL procedural cover
- **`md2epub_pandoc.py`** — pandoc-based synthesizer (DEFAULT). ~30×
  faster than Calibre on large books, 0-error audit parity after the
  `+ascii_identifiers` reader extension + EPUB-level dedupe-id post-pass.
- `md2epub.py` — Calibre `ebook-convert` wrapper (fallback / reference).
- `md2epub_chunked.py` — pre-chunked Calibre alternative. Pre-renders
  markdown via markdown-it-py + hands Calibre an OPF so its 30-minute
  auto-split stage is a no-op. Wall time on the WAF corpus: ~34s,
  comparable image / TOC fidelity to baseline. Opt-in via
  `--synthesizer chunked`.
- `audit/` — L2 defect detector pack (`pdf2epub-audit`)
- `llmdiff/` — L3 LLM visual diff scaffold (`pdf2epub-llmdiff`, optional)

The top-level `pdf2epub-pro convert` CLI picks the synthesizer via
`--synthesizer {pandoc,calibre,chunked}` (default `pandoc`).

## Defect-coverage strategy

| Layer | Where                                                      | Cost           |
|-------|------------------------------------------------------------|----------------|
| L0    | Regex rules embedded in tidy / fetch_refs / restore_links  | free           |
| L1    | Structural audit (epub-audit.py, Calibre Check Book)       | seconds        |
| L2    | `pdf2epub-audit` detector pack                             | seconds        |
| L3    | `pdf2epub-llmdiff` semantic diff                           | ~$0.02–0.04/book |
| L4    | Human visual review                                        | irreducible    |

Every L4 finding should add an L2 detector — the library grows toward
asymptotic coverage.

## Testing

```
pip install -e .            # one-time editable install (tests need this)
pytest tests/               # full suite (~1s)
pytest tests/test_audit/    # detector subset
```

Tests assume the package is editable-installed; without it imports fail.

## Environment

- Windows console encoding is cp950 by default — avoid printing non-ASCII
  to stdout in scripts that may be piped; write to a file instead.
- Calibre must be on PATH for `md2epub` / `pdf2epub-pro convert`.
- `pypdfium2` + Docling are the supported ML parsers on Windows; marker
  is unstable in our environment.

## Known historical bugs (don't re-introduce)

- `[^\]]+` link-text regex bails on inner `]` from control-ID labels like
  `[[CloudTrail.1] X](./y.html)`. Use a nested-bracket-tolerant pattern.
- `_escape_placeholders_in_code` only catches ALL_CAPS single-word
  placeholders. Multi-word `<Microsoft Entra Tenant ID>` leak into XHTML
  as elements with `id=""`, triggering Calibre's `DuplicateId`.
- `normalize_relative_links` must skip image syntax `![X](path)`, otherwise
  every diagram is silently rewritten to a remote URL.
- Calibre's flag is `--markdown-extensions`, not `--md-extensions` (silent
  exit-2 if wrong; never assume success from a piped tail).
- Formula-image fallback must use an EMPTY alt `![](path)`. pandoc's
  `implicit_figures` turns a non-empty alt on a standalone image into a
  visible `<figcaption>`; the un-decodable OCR TeX put there renders as
  garbled caption clutter under every crop. Guarded by
  `test_apply_formula_image_fallback_uses_empty_alt`.

## Commit / push

The maintainer commits when a chunk of work is complete; assume `git push`
is fine on `main` unless told otherwise. Don't commit half-finished work
or `__pycache__` / build artifacts.
