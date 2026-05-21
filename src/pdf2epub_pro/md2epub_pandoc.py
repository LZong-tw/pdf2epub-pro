"""Run pandoc to synthesize an EPUB — sibling synthesizer to md2epub.py.

This is the "Option 3" build path for the head-to-head benchmark vs
Calibre's ebook-convert.  The function signature and CLI surface mirror
md2epub.py exactly so call sites can swap implementations by import.

The pandoc reader we use is `markdown` (python-markdown-compatible
flavor) plus the explicit extension list that matches what Calibre's
`--markdown-extensions` enables.  See the feature parity matrix in the
docstring of `md2epub_pandoc()` for what does and doesn't survive the
round trip.
"""
import argparse
import subprocess
import tempfile
from pathlib import Path

from ._tools import pandoc_path, share_dir


# Pandoc's `markdown` reader has every python-markdown extension we need
# except `abbreviations` (off by default — turn it on).  We also DISABLE
# the math-mode and raw-tex extensions: book content here is prose and
# code, never LaTeX, and pandoc's default `markdown` flavor otherwise
# greedily interprets `$...$` and `\foo{...}` spans inside JSON / YAML
# code as TeX — turning quoted JSON keys into malformed math markup.
# `raw_html` is intentionally left ON: shipped tables contain inline
# `<br/>` for line wrap, and tidy emits a few HTML fragments we need
# pandoc to pass through verbatim.
_READER_EXTS = (
    "markdown"
    "+pipe_tables"
    "+fenced_code_blocks"
    "+fenced_code_attributes"
    "+header_attributes"
    "+link_attributes"
    "+definition_lists"
    "+smart"
    "+abbreviations"
    # Force auto-generated heading IDs to pure ASCII.  Without this
    # pandoc keeps Unicode characters (and any mojibake leftovers like
    # `â` from em-dashes) inside heading slugs, producing IDs like
    # `amazon-routeâ-53` or `sulamérica-seguros`.  Those are technically
    # valid HTML5 but our audit (mirroring Calibre's stricter behavior)
    # flags them as `invalid_id` and they're harder to reference from
    # external tools.  `ascii_identifiers` ASCII-fies the slug while
    # preserving link target consistency — pandoc updates every internal
    # href against the new IDs in one pass.
    "+ascii_identifiers"
    "-tex_math_dollars"
    "-tex_math_single_backslash"
    "-tex_math_double_backslash"
    "-raw_tex"
    "-latex_macros"
)


def _build_metadata_yaml(*,
                         title: str | None,
                         authors: str | None,
                         language: str,
                         publisher: str | None,
                         tags: str | None,
                         book_producer: str) -> str:
    """Render a pandoc-compatible YAML metadata block.

    Pandoc reads `creator`, `publisher`, `subject`, `rights` etc. and
    writes them into the EPUB OPF.  `creator` accepts a list, which is
    how multiple authors get serialized as separate <dc:creator> nodes.
    `book-producer` is not a Dublin Core term; pandoc surfaces arbitrary
    OPF metadata via `belongs-to-collection` / `creator role` patterns
    but the cleanest cross-reader-friendly slot for "who built this
    EPUB" is a tagged `<dc:contributor opf:role="bkp">` — which pandoc
    emits when we set `contributor` with a `role: bkp`.
    """
    def _yaml_str(s: str) -> str:
        # Conservative YAML quoting: wrap in double quotes and escape
        # backslashes / quotes / newlines.  Metadata values are short
        # human strings; this is enough.
        escaped = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'

    lines = ["---"]
    if title:
        lines.append(f"title: {_yaml_str(title)}")
    if authors:
        # Split on common author separators (comma / " and " / " & ")
        # the same way Calibre does, so output OPF has one
        # <dc:creator> per author.
        parts = [a.strip() for a in
                 authors.replace(" & ", ",").replace(" and ", ",").split(",")
                 if a.strip()]
        if len(parts) > 1:
            lines.append("creator:")
            for a in parts:
                lines.append(f"  - role: author")
                lines.append(f"    text: {_yaml_str(a)}")
        else:
            lines.append(f"author: {_yaml_str(parts[0] if parts else authors)}")
    lines.append(f"lang: {_yaml_str(language)}")
    if publisher:
        lines.append(f"publisher: {_yaml_str(publisher)}")
    if tags:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        if tag_list:
            lines.append("subject:")
            for t in tag_list:
                lines.append(f"  - {_yaml_str(t)}")
    # Book producer: emit as a contributor with the OPF role "bkp"
    # (book producer).  Pandoc's epub writer honors a `role:` key on
    # contributor entries and maps it onto opf:role.
    if book_producer:
        lines.append("contributor:")
        lines.append(f"  - role: bkp")
        lines.append(f"    text: {_yaml_str(book_producer)}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def md2epub_pandoc(md_in: Path, epub_out: Path, *,
                   title: str | None = None,
                   authors: str | None = None,
                   language: str = "en",
                   publisher: str | None = None,
                   tags: str | None = None,
                   cover: Path | None = None,
                   extra_css: Path | None = None,
                   book_producer: str = "pdf2epub-pro") -> Path:
    """Convert markdown to EPUB via pandoc. Returns epub_out.

    Feature parity vs md2epub.py (Calibre):

    | Calibre flag                  | Pandoc handling                       |
    |-------------------------------|---------------------------------------|
    | --chapter "/" --chapter-mark  | implicit — pandoc never auto-splits   |
    | tables                        | +pipe_tables                          |
    | fenced_code                   | +fenced_code_blocks                   |
    | attr_list                     | +header_attributes, +link_attributes, |
    |                               | +fenced_code_attributes               |
    | abbr                          | +abbreviations (definitions parsed —  |
    |                               | but pandoc strips them; no <abbr>     |
    |                               | wrap is emitted, see KNOWN GAPS)      |
    | smarty                        | +smart                                |
    | def_list                      | +definition_lists                     |
    | --level1-toc, level2, level3  | --toc --toc-depth=3                   |
    | --epub-version 3              | -t epub3                              |
    | --output-profile tablet       | n/a — pandoc has no profile concept;  |
    |                               | layout is driven by extra-css         |
    | --pretty-print                | n/a — pandoc always emits clean XHTML |
    | --minimum-line-height 130     | embedded in epub.css line-height: 1.55|
    | --smarten-punctuation         | +smart (overlaps with reader flag)    |
    | --language                    | metadata lang:                        |
    | --book-producer               | contributor with role: bkp            |
    | --title / --authors / etc     | YAML metadata block                   |
    | --extra-css                   | --css=<file>                          |
    | --cover                       | --epub-cover-image=<file>             |

    KNOWN GAPS vs Calibre:

    1. Abbreviation definitions (`*[HTML]: ...`) are consumed by
       +abbreviations so they don't leak into body text, but pandoc
       does not wrap the corresponding HTML tokens in <abbr>.
       The visible loss is the tooltip on hover for screen readers —
       inline text reads identically.  Our shipped docs do not rely
       on abbr wrapping (no live use found in tidy.py output).

    2. "Output profile" (Calibre's tablet/kindle/phone bucket of
       layout tweaks) has no pandoc equivalent.  Since we override
       Calibre's profile-driven CSS with extra-css anyway, the loss
       here is effectively nil.

    3. Pandoc emits a single XHTML file per top-level chunk it sees;
       it does NOT silently auto-split on H1.  That's intentional and
       is the whole point of preferring pandoc over Calibre for this
       benchmark — Calibre's auto-split was the ~30-minute hot spot
       on the WAF input.
    """
    if extra_css is None:
        candidate = share_dir() / "epub.css"
        if candidate.exists():
            extra_css = candidate

    # Pandoc accepts metadata via --metadata-file as YAML, which lets
    # us avoid editing the 12 MB markdown body in place.
    yaml_block = _build_metadata_yaml(
        title=title,
        authors=authors,
        language=language,
        publisher=publisher,
        tags=tags,
        book_producer=book_producer,
    )

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as meta_fp:
        meta_fp.write(yaml_block)
        meta_path = Path(meta_fp.name)

    try:
        # Pandoc resolves relative image/resource paths against CWD,
        # not against the source markdown's directory.  Mirror Calibre's
        # behavior by adding the md_in parent to --resource-path so
        # `![](AWS-WAF_artifacts/foo.png)` references resolve.
        resource_path = str(md_in.resolve().parent)

        cmd = [
            pandoc_path(),
            "--from", _READER_EXTS,
            "--lua-filter", str(share_dir() / "pandoc" / "dedup-ids.lua"),
            "--to", "epub3",
            "-o", str(epub_out),
            "--metadata-file", str(meta_path),
            "--resource-path", resource_path,
            "--toc",
            "--toc-depth=3",
            # `--split-level=1` ensures each <h1> starts a new EPUB
            # spine file — matching how Calibre/our CSS treat H1 as a
            # chapter boundary.  This is pandoc 3.x syntax; on older
            # builds the equivalent was --epub-chapter-level.
            "--split-level=1",
            "--standalone",
            str(md_in),
        ]
        if extra_css:
            cmd += ["--css", str(extra_css)]
        if cover:
            cmd += [f"--epub-cover-image={cover}"]

        subprocess.run(cmd, check=True)
    finally:
        try:
            meta_path.unlink()
        except OSError:
            pass

    _dedupe_epub_ids(epub_out)
    return epub_out


def _dedupe_epub_ids(epub_path: Path) -> int:
    """Rename cross-file duplicate IDs in the EPUB and update hrefs.

    Pandoc's auto_identifiers rule disambiguates duplicate IDs within
    the parsed AST but does NOT cover sections the EPUB writer
    synthesises later (e.g. the bodymatter "preamble" chunk pandoc
    generates from `--metadata title` when the markdown has content
    before its first H1).  That leaves us with two
    ``<section id="aws-well-architected-framework">`` elements in two
    XHTML files — a real EPUB-spec violation that our audit (rightly)
    reports as `duplicate_id`.

    This pass walks every XHTML in the EPUB once, finds IDs that occur
    in 2+ files, keeps the first occurrence intact, and renames the
    later occurrences with a `-1`, `-2`, … suffix.  Any ``href``
    elsewhere that pointed at the renamed (file, id) pair is updated to
    the new id so internal navigation stays intact.

    Returns the number of IDs renamed (zero on clean input).
    """
    import re
    import zipfile
    from collections import defaultdict

    id_re = re.compile(rb'id="([^"]+)"')
    href_re = re.compile(rb'href="([^"#]*)#([^"]+)"')

    with zipfile.ZipFile(epub_path, "r") as zin:
        members = zin.namelist()
        contents = {n: zin.read(n) for n in members}

    xhtml = [n for n in members if n.lower().endswith((".xhtml", ".html"))]

    # Map id -> ordered list of files where it first appears.
    id_files: dict[bytes, list[str]] = defaultdict(list)
    for fn in xhtml:
        seen_in_file: set[bytes] = set()
        for m in id_re.finditer(contents[fn]):
            ident = m.group(1)
            if ident not in seen_in_file:
                seen_in_file.add(ident)
                if fn not in id_files[ident]:
                    id_files[ident].append(fn)

    # Collect ALL ids in use across the EPUB so we can pick suffixes
    # that don't collide with pandoc's own auto-disambiguated `-1`,
    # `-2`, … forms.  Otherwise dedupe could rename
    # `aws-well-architected-framework` to `-1` when pandoc already
    # produced a different heading with that same `-1` suffix.
    all_ids: set[bytes] = set(id_files.keys())

    def pick_suffix(base: bytes) -> bytes:
        i = 1
        while True:
            candidate = base + b"-" + str(i).encode()
            if candidate not in all_ids:
                all_ids.add(candidate)
                return candidate
            i += 1

    # rename plan: (file, old_id) -> new_id
    renames: dict[tuple[str, bytes], bytes] = {}
    for ident, files in id_files.items():
        if len(files) <= 1:
            continue
        for fn in files[1:]:
            renames[(fn, ident)] = pick_suffix(ident)

    if not renames:
        return 0

    # Build a global (file_basename, old_id) -> new_id lookup for href
    # resolution.  Hrefs use just the file basename (no directory) when
    # they're in the same directory as the target, but pandoc-generated
    # EPUBs sometimes use relative paths like "../text/ch002.xhtml".
    file_basename_renames: dict[tuple[bytes, bytes], bytes] = {}
    for (fn, old_id), new_id in renames.items():
        base = fn.rsplit("/", 1)[-1].encode()
        file_basename_renames[(base, old_id)] = new_id

    def rename_id_attrs(file_name: str, content: bytes) -> bytes:
        targets = {old_id: new_id for (f, old_id), new_id in renames.items()
                   if f == file_name}
        if not targets:
            return content

        def repl_id(m):
            ident = m.group(1)
            new = targets.get(ident)
            return b'id="' + new + b'"' if new else m.group(0)

        return id_re.sub(repl_id, content)

    def rewrite_hrefs(file_name: str, content: bytes) -> bytes:
        # Update href="...#X" pointing at a file whose X was renamed.
        # We use the basename of the href target to look up.
        def repl_href(m):
            href_file, frag = m.group(1), m.group(2)
            base = href_file.rsplit(b"/", 1)[-1] if href_file else None
            # When href_file is empty, the anchor is within the current file.
            if base is None or base == b"":
                base = file_name.rsplit("/", 1)[-1].encode()
            new = file_basename_renames.get((base, frag))
            if new is None:
                return m.group(0)
            return b'href="' + href_file + b"#" + new + b'"'

        return href_re.sub(repl_href, content)

    for fn in xhtml:
        contents[fn] = rename_id_attrs(fn, contents[fn])
        contents[fn] = rewrite_hrefs(fn, contents[fn])

    # Repack the EPUB in place.  Preserve the original member order and
    # keep `mimetype` stored uncompressed as the first entry per spec.
    tmp = epub_path.with_suffix(".tmp.epub")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        if "mimetype" in members:
            zout.writestr(
                zipfile.ZipInfo("mimetype"),
                contents["mimetype"],
                zipfile.ZIP_STORED,
            )
        for n in members:
            if n == "mimetype":
                continue
            zout.writestr(n, contents[n])
    tmp.replace(epub_path)
    return len(renames)


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="pdf2epub-build-pandoc",
        description="Run pandoc with the curated EPUB flags (alt to Calibre).",
    )
    p.add_argument("md_in")
    p.add_argument("epub_out")
    p.add_argument("--title")
    p.add_argument("--authors")
    p.add_argument("--language", default="en")
    p.add_argument("--publisher")
    p.add_argument("--tags")
    p.add_argument("--cover")
    p.add_argument("--extra-css")
    args = p.parse_args(argv)

    md2epub_pandoc(
        Path(args.md_in),
        Path(args.epub_out),
        title=args.title,
        authors=args.authors,
        language=args.language,
        publisher=args.publisher,
        tags=args.tags,
        cover=Path(args.cover) if args.cover else None,
        extra_css=Path(args.extra_css) if args.extra_css else None,
    )


if __name__ == "__main__":
    main()
