#!/usr/bin/env python3
"""
restyle_pdf.py - force all static page text in a PDF to one font and size,
keeping every line exactly where it was.

Approach: for each line of text on the page, capture its spans (text, baseline
origin, colour, bold/italic), erase the original text with a redaction that
leaves line art and images untouched, then redraw each span on its ORIGINAL
baseline in the target font and size.

Because each line is redrawn on its own baseline rather than re-wrapped, the
page structure - ruled lines, boxes, tables, deliberate line breaks in
addresses - survives intact. This works well when shrinking text (e.g. 12pt to
10pt) and gets risky when growing it, so every line is width-checked and
anything suspicious is written to the report instead of being silently shipped.

If the target size doesn't fit - which happens when the original is set in a
narrow font like Times and Arial at the same size is ~20% wider - --best-fit
backs the size off just far enough that every line still fits, rather than
letting text run off the page.

Usage:
    python restyle_pdf.py IN.pdf -o OUT.pdf
    python restyle_pdf.py ./forms -o ./restyled --best-fit --png ./proofs
    python restyle_pdf.py ./forms -o ./restyled --report report.csv --png ./proofs
    python restyle_pdf.py ./forms --dry-run --report audit.csv

Requires: pip install pymupdf
"""

import argparse
import csv
import os
import sys
import glob
from collections import Counter

import pymupdf as fitz

# --------------------------------------------------------------------------
# Font resolution
# --------------------------------------------------------------------------

# Where Arial actually lives, per platform. Liberation Sans is metric-compatible
# with Arial (same advance widths) and is the fallback if Arial isn't present.
FONT_SEARCH = {
    "regular":    ["C:/Windows/Fonts/arial.ttf",
                   "/Library/Fonts/Arial.ttf",
                   "/System/Library/Fonts/Supplemental/Arial.ttf",
                   "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"],
    "bold":       ["C:/Windows/Fonts/arialbd.ttf",
                   "/Library/Fonts/Arial Bold.ttf",
                   "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                   "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"],
    "italic":     ["C:/Windows/Fonts/ariali.ttf",
                   "/Library/Fonts/Arial Italic.ttf",
                   "/System/Library/Fonts/Supplemental/Arial Italic.ttf",
                   "/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf"],
    "bolditalic": ["C:/Windows/Fonts/arialbi.ttf",
                   "/Library/Fonts/Arial Bold Italic.ttf",
                   "/System/Library/Fonts/Supplemental/Arial Bold Italic.ttf",
                   "/usr/share/fonts/truetype/liberation/LiberationSans-BoldItalic.ttf"],
}


def resolve_fonts(explicit=None):
    """Return {style: (alias, path, fitz.Font)}. explicit = dict of overrides."""
    out = {}
    for style, candidates in FONT_SEARCH.items():
        path = (explicit or {}).get(style)
        if not path:
            path = next((p for p in candidates if os.path.exists(p)), None)
        if not path:
            if style == "regular":
                sys.exit("Could not find Arial or Liberation Sans. "
                         "Pass one with --font-regular /path/to/arial.ttf")
            path = out["regular"][1]          # fall back to regular
        out[style] = (f"RS-{style}", path, fitz.Font(fontfile=path))
    return out


def style_of(flags, fonts):
    bold = bool(flags & 16)
    italic = bool(flags & 2)
    key = "bolditalic" if (bold and italic) else "bold" if bold else "italic" if italic else "regular"
    return fonts[key]


def save_proof(before, after, path):
    """Write a side-by-side before/after image for visual review."""
    from PIL import Image
    a = Image.frombytes("RGB", (before.width, before.height), before.samples)
    b = Image.frombytes("RGB", (after.width, after.height), after.samples)
    gap = 24
    canvas = Image.new("RGB", (a.width + b.width + gap,
                               max(a.height, b.height)), (210, 210, 210))
    canvas.paste(a, (0, 0))
    canvas.paste(b, (a.width + gap, 0))
    canvas.save(path)


# --------------------------------------------------------------------------
# Page processing
# --------------------------------------------------------------------------

def collect_lines(page, skip_conforming, target_font_substr, target_size, size_tol):
    """Gather every horizontal text line with its spans. Returns (lines, notes)."""
    lines, notes = [], []
    data = page.get_text("dict")

    for block in data["blocks"]:
        if block.get("type") != 0:            # 0 = text, 1 = image
            continue
        for line in block["lines"]:
            dirx, diry = line.get("dir", (1, 0))
            if abs(diry) > 1e-6 or dirx < 0:
                notes.append(("skipped_rotated_text",
                              " ".join(s["text"] for s in line["spans"])[:60]))
                continue

            spans = []
            for sp in line["spans"]:
                if not sp["text"].strip():
                    continue
                if skip_conforming and target_font_substr.lower() in sp["font"].lower() \
                        and abs(sp["size"] - target_size) <= size_tol:
                    continue
                spans.append(sp)
            if spans:
                lines.append({"bbox": fitz.Rect(line["bbox"]), "spans": spans,
                              "block_bbox": fitz.Rect(block["bbox"])})
    return lines, notes


def census_band(y, page_height, bands=40):
    """Horizontal band a glyph falls in, keyed on its baseline y."""
    return min(bands - 1, max(0, int(y / max(page_height, 1) * bands)))


def glyph_census(page, bands=40):
    """Count visible glyphs by (band, character) using get_texttrace(), which
    sees text that get_text('dict') misses (runs with broken encodings, some form
    artefacts). Used to prove nothing was erased without being redrawn, and
    nothing was drawn twice.

    Whitespace isn't counted. It has no ink, and redrawn spaces come back from
    the new font as U+00A0 - counting them let every redrawn line inflate the
    total by its number of spaces, enough to hide a real loss of the same size.
    Glyphs are banded by baseline, which restyling never moves, so a glyph
    lands in the same band before and after."""
    counts = Counter()
    h = page.rect.height
    for run in page.get_texttrace():
        if run.get("type") != 0:          # 0 = normal filled text
            continue
        for c in run.get("chars", []):
            if c[0] <= 0 or chr(c[0]).isspace():
                continue
            counts[(census_band(c[2][1], h, bands), c[0])] += 1
    return counts


def verify_census(before, after, page_height, expected=None, bands=40):
    """Compare two censuses glyph by glyph. Restyling changes how glyphs look,
    never which glyphs there are or which line they sit on, so every
    (band, character) count must match exactly. The only allowed difference is
    `expected`: underscores deliberately added or removed when a fill-in rule is
    rebuilt to its original length.

    Both directions are errors. A loss is text erased but not redrawn. A gain is
    text drawn that wasn't visible before - e.g. a label inside a form field,
    which redaction can't erase, redrawn on the page on top of the original, or
    an invisible OCR layer redrawn as visible text. Comparing per character also
    catches a loss and a gain that would cancel out in a plain total."""
    delta = Counter(after)
    delta.subtract(before)
    if expected:
        delta.subtract(expected)

    issues = []
    for issue, sign, what in (("TEXT_LOST", -1, "erased but not redrawn"),
                              ("TEXT_GAINED", 1, "drawn that weren't visible before")):
        per_char, per_band = Counter(), Counter()
        for (band, cp), n in delta.items():
            if sign * n > 0:
                per_char[cp] += sign * n
                per_band[band] += sign * n
        if not per_char:
            continue
        worst = max(per_band, key=per_band.get)
        y0 = round(worst * page_height / bands)
        y1 = round((worst + 1) * page_height / bands)
        shown = ", ".join(f"{chr(cp)!r} x{n}" for cp, n in per_char.most_common(8))
        if len(per_char) > 8:
            shown += ", ..."
        issues.append((issue, f"{sum(per_char.values())} glyph(s) {what} ({shown}); "
                              f"largest near y={y0}-{y1}"))
    return issues


def recover_orphans(page, known_rects):
    """Find text that get_text('dict') never reported, using the lower-level
    get_texttrace(). Such runs still get erased by the redaction pass, so unless
    they are redrawn they vanish silently - which is how the rule after 'FROM:'
    on a real form disappeared. Returns run dicts ready to redraw."""
    runs = []
    for tr in page.get_texttrace():
        if tr.get("type") != 0 or not tr.get("chars"):
            continue
        if abs(tr.get("dir", (1, 0))[1]) > 1e-6:
            continue

        # Keep only characters whose origin isn't inside a span we already know.
        loose = [c for c in tr["chars"]
                 if c[0] not in (0, 32)
                 and not any(fitz.Point(c[2]) in r for r in known_rects)]
        if not loose:
            continue

        # Split into contiguous runs wherever there's a gap in the original.
        group = [loose[0]]
        for prev, cur in zip(loose, loose[1:]):
            if cur[3][0] - prev[3][2] > tr.get("spacewidth", 3):
                runs.append((tr, group)); group = [cur]
            else:
                group.append(cur)
        runs.append((tr, group))

    out = []
    for tr, group in runs:
        text = "".join(chr(c[0]) for c in group)
        rect = fitz.Rect(group[0][3])
        for c in group:
            rect |= fitz.Rect(c[3])
        out.append({
            "text": text,
            "rect": rect,
            "origin": group[0][2],
            "width": group[-1][3][2] - group[0][3][0],
            "flags": tr.get("flags", 0),
            "color": tr.get("color", (0, 0, 0)),
            # A run of nothing but underscores is a fill-in rule, not prose. Its
            # length is the point of it, so it gets rebuilt to the original width
            # rather than allowed to shrink with the font.
            "is_rule": bool(text) and set(text) <= {"_"},
        })
    return out


def restyle_page(page, fonts, size, pad, centre_tol, min_indent, keep_size_ratio,
                 skip_conforming, target_font_substr, size_tol, dry_run, gap_tol=3.0,
                 recover=True):
    """Restyle one page. Returns (issues, expected): a list of (issue, detail)
    tuples, and the glyph-count changes made on purpose, for the census."""
    issues = []
    lines, notes = collect_lines(page, skip_conforming, target_font_substr, size, size_tol)
    issues.extend(notes)
    if not lines:
        return issues, Counter()

    page_centre = page.rect.width / 2
    text_left = min(l["bbox"].x0 for l in lines)

    # --- plan every line before touching the page ------------------------
    plan = []
    for ln in lines:
        old_w = ln["bbox"].width
        new_w = sum(style_of(sp["flags"], fonts)[2].text_length(sp["text"], size)
                    for sp in ln["spans"])
        text_preview = "".join(sp["text"] for sp in ln["spans"])[:60]

        # Centred-line detection: sits near the page centre and is clearly
        # indented from the text margin. Re-centre these so titles and footers
        # don't jump left when the text gets narrower.
        looks_centred = (abs((ln["bbox"].x0 + ln["bbox"].x1) / 2 - page_centre) < centre_tol
                         and (ln["bbox"].x0 - text_left) > min_indent)

        if new_w > old_w * keep_size_ratio:
            issues.append(("line_got_wider",
                           f"{text_preview!r} {old_w:.0f}pt -> {new_w:.0f}pt"))

        plan.append({"line": ln, "new_w": new_w, "old_w": old_w, "centred": looks_centred})

    if dry_run:
        return issues, Counter()

    orphans = recover_orphans(page, [ln["bbox"] for ln in lines]) if recover else []
    if orphans:
        issues.append(("recovered_hidden_text",
                       f"{len(orphans)} run(s) invisible to the text extractor, redrawn"))

    # --- erase all original text in one pass ------------------------------
    for r in [fitz.Rect(ln["bbox"]) for ln in lines] + [o["rect"] for o in orphans]:
        r = fitz.Rect(r)
        r.x0 -= pad; r.y0 -= pad; r.x1 += pad; r.y1 += pad
        page.add_redact_annot(r, fill=None, cross_out=False)

    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                          graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                          text=fitz.PDF_REDACT_TEXT_REMOVE)

    # --- redraw ------------------------------------------------------------
    for item in plan:
        ln = item["line"]
        if item["centred"]:
            # keep the visual centre, shift every span by the same amount
            dx = (page_centre - item["new_w"] / 2) - ln["bbox"].x0
        else:
            dx = 0.0

        prev_end_old = None      # right edge of previous span, original layout
        prev_end_new = None      # right edge of previous span, as redrawn
        for sp in ln["spans"]:
            alias, path, font = style_of(sp["flags"], fonts)
            x, y = sp["origin"]

            # If this span butted up against the previous one, it's continuous
            # text (e.g. a bold phrase inside a sentence) and must be pushed
            # along as the earlier spans grow. If there was a real gap, it's a
            # column or tab stop, so keep its original x.
            if prev_end_old is not None and (sp["bbox"][0] - prev_end_old) < gap_tol:
                x = prev_end_new - dx

            rgb = sp.get("color", 0)
            colour = (((rgb >> 16) & 255) / 255, ((rgb >> 8) & 255) / 255, (rgb & 255) / 255)
            page.insert_text(fitz.Point(x + dx, y), sp["text"],
                             fontname=alias, fontfile=path,
                             fontsize=size, color=colour, overlay=True)

            prev_end_old = sp["bbox"][2]
            prev_end_new = x + dx + font.text_length(sp["text"], size)

    expected = Counter()        # glyph-count changes made on purpose
    for o in orphans:
        alias, path, font = style_of(o["flags"], fonts)
        text = o["text"]
        if o["is_rule"] and o["width"] > 0:
            # rebuild the rule to its original length at the new glyph width
            unit = font.text_length("_", size) or 1
            text = "_" * max(1, int(round(o["width"] / unit)))
            band = census_band(o["origin"][1], page.rect.height)
            expected[(band, ord("_"))] += len(text) - len(o["text"])
        page.insert_text(fitz.Point(*o["origin"]), text,
                         fontname=alias, fontfile=path,
                         fontsize=size, color=tuple(o["color"]), overlay=True)

    return issues, expected


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def best_fit_size(doc, fonts, target, min_size, right_margin, quantise):
    """Largest size <= target at which no line exceeds the document's own right
    edge. Width scales linearly with point size, so this is arithmetic, not a
    search. Returns (size, binding_line_description)."""
    best, culprit = target, None
    for page in doc:
        lines = []
        for block in page.get_text("dict")["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block["lines"]:
                if abs(line.get("dir", (1, 0))[1]) > 1e-6:
                    continue
                spans = [s for s in line["spans"] if s["text"].strip()]
                if spans:
                    lines.append((fitz.Rect(line["bbox"]), spans))
        if not lines:
            continue

        # The limit is the document's own right-most text extent, so the
        # restyled page never spills past the footprint the original used.
        limit = right_margin if right_margin else max(r.x1 for r, _ in lines)

        for rect, spans in lines:
            w_at_target = sum(style_of(s["flags"], fonts)[2].text_length(s["text"], target)
                              for s in spans)
            if w_at_target <= 0:
                continue
            fits = (limit - rect.x0) / (w_at_target / target)
            if fits < best:
                best = fits
                culprit = "".join(s["text"] for s in spans)[:70]

    best = max(min_size, min(target, best))
    if quantise:
        best = int(best / quantise) * quantise      # round DOWN to a clean step
        best = max(min_size, best)
    return round(best, 2), culprit


def process_file(src, dst, fonts, args):
    doc = fitz.open(src)
    rows = []
    # Census every page before anything is touched. It's checked against the
    # saved file, not the page in memory: after redaction the in-memory page
    # stops drawing form-field appearances, so a field label redrawn on top of
    # itself would cancel out of the count and pass.
    census_before = [] if args.dry_run else [glyph_census(p) for p in doc]
    expected = []

    size = args.size
    if args.best_fit:
        size, culprit = best_fit_size(doc, fonts, args.size, args.min_size,
                                      args.right_margin, args.quantise)
        if size < args.size:
            rows.append([os.path.basename(src), "", "size_reduced",
                         f"{args.size}pt -> {size}pt, limited by: {culprit!r}"])

    for pno, page in enumerate(doc, start=1):
        before_pix = page.get_pixmap(dpi=args.png_dpi) if (args.png and not args.dry_run) else None
        issues, allowed = restyle_page(
                page, fonts, size, args.pad, args.centre_tol, args.min_indent,
                args.width_tolerance, args.skip_conforming, args.target_font,
                args.size_tolerance, args.dry_run, args.gap_tolerance,
                not args.no_recover)
        expected.append(allowed)
        for issue, detail in issues:
            rows.append([os.path.basename(src), pno, issue, detail])
        if before_pix is not None:
            save_proof(before_pix, page.get_pixmap(dpi=args.png_dpi),
                       os.path.join(args.png,
                                    f"{os.path.basename(src)[:-4]}_p{pno}_proof.png"))

    if not args.dry_run:
        doc.save(dst, garbage=3, deflate=True)
    doc.close()

    if not args.dry_run:
        with fitz.open(dst) as out:
            if out.page_count != len(census_before):
                rows.append([os.path.basename(src), "", "PAGE_COUNT_CHANGED",
                             f"{len(census_before)} -> {out.page_count}"])
            for pno, page in enumerate(out, start=1):
                if pno > len(census_before):
                    break
                for issue, detail in verify_census(census_before[pno - 1],
                                                   glyph_census(page),
                                                   page.rect.height, expected[pno - 1]):
                    rows.append([os.path.basename(src), pno, issue, detail])
    return rows, size


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="PDF file or folder of PDFs")
    ap.add_argument("-o", "--output", help="output file or folder")
    ap.add_argument("--size", type=float, default=10.0, help="target font size (default 10)")
    ap.add_argument("--target-font", default="Arial",
                    help="substring used to recognise already-correct text (default Arial)")
    ap.add_argument("--skip-conforming", action="store_true",
                    help="leave text alone if it already matches target font and size")
    ap.add_argument("--size-tolerance", type=float, default=0.2,
                    help="how close counts as already-correct size")
    ap.add_argument("--width-tolerance", type=float, default=1.0,
                    help="flag a line if new width exceeds old width times this (default 1.0)")
    ap.add_argument("--best-fit", action="store_true",
                    help="drop below the target size as far as needed so that no line "
                         "overflows, rather than overflowing at the target size")
    ap.add_argument("--min-size", type=float, default=8.0,
                    help="floor for --best-fit; below this the file is left flagged (default 8)")
    ap.add_argument("--quantise", type=float, default=0.5,
                    help="round the best-fit size down to a multiple of this (default 0.5)")
    ap.add_argument("--right-margin", type=float,
                    help="hard right limit in points; default is the document's own "
                         "right-most text extent")
    ap.add_argument("--no-recover", action="store_true",
                    help="don't redraw text that the extractor can't see (it will be lost)")
    ap.add_argument("--gap-tolerance", type=float, default=3.0,
                    help="gap in points below which adjacent spans are treated as "
                         "continuous text and flowed together (default 3)")
    ap.add_argument("--pad", type=float, default=0.5,
                    help="points added around each line before erasing (default 0.5)")
    ap.add_argument("--centre-tol", type=float, default=12.0,
                    help="how near the page centre a line must sit to count as centred")
    ap.add_argument("--min-indent", type=float, default=24.0,
                    help="how far right of the text margin a centred line must start")
    ap.add_argument("--report", help="write a CSV of flagged lines here")
    ap.add_argument("--png", help="folder for side-by-side before/after proof images")
    ap.add_argument("--png-dpi", type=int, default=110)
    ap.add_argument("--dry-run", action="store_true",
                    help="report only, write nothing")
    ap.add_argument("--font-regular"); ap.add_argument("--font-bold")
    ap.add_argument("--font-italic");  ap.add_argument("--font-bolditalic")
    args = ap.parse_args()

    fonts = resolve_fonts({k: getattr(args, f"font_{k}") for k in
                           ("regular", "bold", "italic", "bolditalic")
                           if getattr(args, f"font_{k}")})
    print(f"Using: {fonts['regular'][1]}")

    if os.path.isdir(args.input):
        srcs = sorted(glob.glob(os.path.join(args.input, "*.pdf")))
        outdir = args.output or (args.input.rstrip("/\\") + "_restyled")
        if not args.dry_run:
            os.makedirs(outdir, exist_ok=True)
        pairs = [(s, os.path.join(outdir, os.path.basename(s))) for s in srcs]
    else:
        pairs = [(args.input, args.output or args.input.replace(".pdf", "_restyled.pdf"))]

    if args.png and not args.dry_run:
        os.makedirs(args.png, exist_ok=True)

    all_rows, failed, sizes = [], [], []
    for i, (src, dst) in enumerate(pairs, 1):
        try:
            rows, used = process_file(src, dst, fonts, args)
            all_rows.extend(rows)
            sizes.append(used)
            flag = f"  [{len(rows)} flagged]" if rows else ""
            note = f"  @{used}pt" if used != args.size else ""
            print(f"[{i}/{len(pairs)}] {os.path.basename(src)}{note}{flag}")
        except Exception as e:
            failed.append((os.path.basename(src), repr(e)))
            print(f"[{i}/{len(pairs)}] {os.path.basename(src)}  FAILED: {e}")

    if args.report:
        with open(args.report, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["file", "page", "issue", "detail"])
            w.writerows(all_rows)
            w.writerows([[f, "", "file_failed", e] for f, e in failed])
        print(f"Report: {args.report}")

    if sizes:
        at_target = sum(1 for z in sizes if z >= args.size)
        print(f"\nAchieved target {args.size}pt on {at_target}/{len(sizes)} file(s).")
        for z in sorted(set(sizes)):
            if z < args.size:
                print(f"  {sum(1 for x in sizes if x == z)} file(s) capped at {z}pt")

    clean = len(pairs) - len({r[0] for r in all_rows}) - len(failed)
    print(f"\n{len(pairs)} file(s): {clean} clean, "
          f"{len({r[0] for r in all_rows})} with flagged lines, {len(failed)} failed.")


if __name__ == "__main__":
    main()
