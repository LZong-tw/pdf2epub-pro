# pdf2epub-pro — Claude project notes

A public PDF→EPUB conversion pipeline (Docling + Calibre). Repo:
https://github.com/LZong-tw/pdf2epub-pro

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

Pipeline: `split → tidy → restore-links → fetch-refs → cover → md2epub`

- `split.py` — PDF chunking via Docling
- `tidy.py` — markdown cleanup (mojibake, hyphenation, list gaps, …)
- `restore_links.py` — re-attach lost hyperlinks from PDF annotations
- `fetch_refs.py` — fetch external refs into appendix via trafilatura
- `make_cover.py` — PIL procedural cover
- `md2epub.py` — Calibre `ebook-convert` wrapper
- `audit/` — L2 defect detector pack (`pdf2epub-audit`)
- `llmdiff/` — L3 LLM visual diff scaffold (`pdf2epub-llmdiff`, optional)

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

## Commit / push

The maintainer commits when a chunk of work is complete; assume `git push`
is fine on `main` unless told otherwise. Don't commit half-finished work
or `__pycache__` / build artifacts.
