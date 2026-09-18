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

--header gives page 1 the house header: a black logo placeholder top-left and
the file name as the title top-right, kept clear of the row where the signing
buttons go. An existing logo and title are replaced, and the page content moves
down only as far as it has to. If that would push it into the bottom margin,
the file is reported and left without a header rather than squeezed.

--spellcheck reports words the dictionary doesn't know, with a suggestion. It
never changes the text. Words it shouldn't flag go in spelling_allowlist.txt.

Usage:
    python restyle_pdf.py IN.pdf -o OUT.pdf
    python restyle_pdf.py ./forms -o ./restyled --best-fit --png ./proofs
    python restyle_pdf.py ./forms -o ./restyled --report report.csv --png ./proofs
    python restyle_pdf.py ./forms --dry-run --report audit.csv
    python restyle_pdf.py ./forms -o ./restyled --best-fit --header --spellcheck \
        --report report.csv --png ./proofs

Requires: pip install pymupdf (and pyspellchecker for --spellcheck)
"""

import argparse
import csv
import os
import re
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


def fix_tounicode(doc, fonts):
    """Arial draws the space and the no-break space with one glyph, and the
    hyphen and the soft hyphen with another. When PyMuPDF embeds the font it
    maps each shared glyph back to the second of the pair, so restyled text
    copies, searches and reads aloud with no-break spaces and invisible soft
    hyphens - 'Pre-authorized' comes out 'Preauthorized'. Point those glyphs
    back at the plain characters in every font this script embedded. Returns
    issues for any mapping it couldn't fix."""
    targets = {}                                    # alias -> {glyph: (bad, good)}
    for alias, path, font in fonts.values():
        targets[alias] = {font.has_glyph(0x20): (0xA0, 0x20),
                          font.has_glyph(0x2D): (0xAD, 0x2D)}
    seen, issues = set(), []
    for page in doc:
        for xref, _, _, _, alias, *_ in page.get_fonts(full=True):
            if alias not in targets or xref in seen:
                continue
            seen.add(xref)
            kind, val = doc.xref_get_key(xref, "ToUnicode")
            if kind != "xref":
                continue
            tu = int(val.split()[0])
            lines = doc.xref_stream(tu).decode("latin-1").split("\n")
            fixes = targets[alias]
            for i, line in enumerate(lines):
                parts = line.split()
                hexes = [p[1:-1] for p in parts if p.startswith("<") and p.endswith(">")]
                if len(parts) == 2 and len(hexes) == 2:                  # bfchar
                    glyph, uni = int(hexes[0], 16), int(hexes[1], 16)
                    if glyph in fixes and uni == fixes[glyph][0]:
                        lines[i] = f"<{hexes[0]}> <{fixes[glyph][1]:04x}>"
                elif len(parts) == 3 and len(hexes) == 3:                # bfrange
                    lo, hi, first = (int(h, 16) for h in hexes)
                    for glyph, (bad, _) in fixes.items():
                        if lo <= glyph <= hi and first + glyph - lo == bad:
                            issues.append(("TOUNICODE_NOT_FIXED",
                                           f"{alias}: glyph {glyph} maps to U+{bad:04X} "
                                           "inside a range; copied text will show it"))
            doc.update_stream(tu, "\n".join(lines).encode("latin-1"))
    return issues


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


def glyph_census(page, dy=0.0, bands=40):
    """Count visible glyphs by (band, character) using get_texttrace(), which
    sees text that get_text('dict') misses (runs with broken encodings, some form
    artefacts). Used to prove nothing was erased without being redrawn, and
    nothing was drawn twice.

    Whitespace isn't counted. It has no ink, and it doesn't survive a round
    trip reliably (before fix_tounicode, redrawn spaces came back as U+00A0) -
    counting it let every redrawn line inflate the total by its number of
    spaces, enough to hide a real loss of the same size.
    Glyphs are banded by baseline, which restyling never moves, so a glyph
    lands in the same band before and after. When the header moves a page's
    content down, `dy` counts the original as if it had moved too."""
    counts = Counter()
    h = page.rect.height
    for run in page.get_texttrace():
        if run.get("type") != 0:          # 0 = normal filled text
            continue
        for c in run.get("chars", []):
            if c[0] <= 0 or chr(c[0]).isspace():
                continue
            counts[(census_band(c[2][1] + dy, h, bands), c[0])] += 1
    return counts


def verify_census(before, after, page_height, expected=None, dy=0.0, bands=40):
    """Compare two censuses glyph by glyph. Restyling changes how glyphs look,
    never which glyphs there are or which line they sit on, so every
    (band, character) count must match exactly. The only allowed differences
    are in `expected`, keyed by (baseline y before any move, character):
    underscores added or removed when a fill-in rule is rebuilt to its original
    length, and the old title taken out and the new one put in by --header.
    `dy` is how far the content moved down, applied to `expected` just as it
    was applied to `before`.

    Both directions are errors. A loss is text erased but not redrawn. A gain is
    text drawn that wasn't visible before - e.g. a label inside a form field,
    which redaction can't erase, redrawn on the page on top of the original, or
    an invisible OCR layer redrawn as visible text. Comparing per character also
    catches a loss and a gain that would cancel out in a plain total."""
    delta = Counter(after)
    delta.subtract(before)
    for (y, cp), n in (expected or {}).items():
        delta[(census_band(y + dy, page_height, bands), cp)] -= n

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
    tuples, and the glyph-count changes made on purpose, keyed by (baseline y,
    character), for the census."""
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
            expected[(o["origin"][1], ord("_"))] += len(text) - len(o["text"])
        page.insert_text(fitz.Point(*o["origin"]), text,
                         fontname=alias, fontfile=path,
                         fontsize=size, color=tuple(o["color"]), overlay=True)

    return issues, expected


# --------------------------------------------------------------------------
# Header: logo placeholder top-left, title top-right, room for signing buttons
# --------------------------------------------------------------------------

# Where Libro's finished forms put the signing buttons, in top-down page points,
# measured off the example forms in form_requirements: Phone Auth, In-Branch
# eSign and Remote eSign (80 x 18pt each, per its signingButtons.js) across
# x 151-402 at y 2-21, with the Remote eSign label and code box flush right of
# them out to x 581. The header never goes here and nothing is moved here.
BUTTON_ZONE = fitz.Rect(145, 0, 587, 24)

# Widgets the requirements tool positions itself. On a form that has already
# been through it they stay put when the page content moves down.
SIGNING_WIDGETS = {"PhoneAuthButton", "DigitalSignatureButton", "RemoteESignButton",
                   "PrintButton", "eSignLBL", "eSign_Code"}


def field_name(doc, xref):
    """A widget's field name, looking up through /Parent for a child widget."""
    for _ in range(8):
        kind, val = doc.xref_get_key(xref, "T")
        if kind == "string":
            return val
        kind, val = doc.xref_get_key(xref, "Parent")
        if kind != "xref":
            return None
        xref = int(val.split()[0])
    return None


def annot_rect(page, xref):
    """An annotation's /Rect in top-down page coordinates."""
    kind, val = page.parent.xref_get_key(xref, "Rect")
    if kind != "array":
        return None
    x0, y0, x1, y1 = (float(v) for v in val.strip()[1:-1].split())
    return fitz.Rect(x0, y0, x1, y1) * page.transformation_matrix


def content_extent(page, skip=()):
    """Bounding box of everything visible on the page - text, line art, images
    and form fields - ignoring anything centred inside a rect in `skip`. Not
    counted: white-filled shapes with no outline (invisible backgrounds), hidden
    annotations, comments and their pop-ups, and the signing widgets. Returns
    None for an empty page. Built from coordinates rather than Rect unions,
    because a horizontal rule is a zero-height rect and unions drop those."""
    rects = []
    for run in page.get_texttrace():
        if run.get("type") not in (0, 1):                     # filled or stroked
            continue
        # Glyphs with ink only: a stray space has a bbox but draws nothing, and
        # Word leaves them at the very top of the page.
        ink = [fitz.Rect(c[3]) for c in run.get("chars", [])
               if c[0] > 0 and not chr(c[0]).isspace()]
        if ink:
            rects.append(fitz.Rect(min(r.x0 for r in ink), min(r.y0 for r in ink),
                                   max(r.x1 for r in ink), max(r.y1 for r in ink)))
    for d in page.get_drawings():
        if d["type"] == "f" and d.get("fill") and min(d["fill"]) >= 0.99:
            continue
        rects.append(fitz.Rect(d["rect"]))
    rects += [fitz.Rect(i["bbox"]) for i in page.get_image_info()]
    doc = page.parent
    for xref, kind, _ in page.annot_xrefs():
        if kind in (fitz.PDF_ANNOT_POPUP, fitz.PDF_ANNOT_TEXT):
            continue
        flags = doc.xref_get_key(xref, "F")
        if flags[0] == "int" and int(flags[1]) & 2:             # Hidden
            continue
        if field_name(doc, xref) in SIGNING_WIDGETS:
            continue
        r = annot_rect(page, xref)
        if r is not None:
            rects.append(r)

    pads = [fitz.Rect(s.x0 - 2, s.y0 - 2, s.x1 + 2, s.y1 + 2) for s in skip]
    rects = [r for r in rects
             if not any(fitz.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2) in s for s in pads)]
    if not rects:
        return None
    return fitz.Rect(min(r.x0 for r in rects), min(r.y0 for r in rects),
                     max(r.x1 for r in rects), max(r.y1 for r in rects))


def find_old_header(page, zone, min_title_size):
    """The existing logo and title on page 1, to be replaced rather than pushed
    down under the new header. A title is a text line lying wholly within the
    top `zone` points and set at `min_title_size` or larger; a logo is an image
    lying wholly in the same band and no wider than half the page. This is a
    guess, so every removal is reported."""
    titles = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            spans = [s for s in line["spans"] if s["text"].strip()]
            if spans and line["bbox"][3] <= zone \
                    and max(s["size"] for s in spans) >= min_title_size:
                titles.append({"bbox": fitz.Rect(line["bbox"]), "spans": spans})
    logos = [fitz.Rect(i["bbox"]) for i in page.get_image_info()
             if i["bbox"][3] <= zone and fitz.Rect(i["bbox"]).width <= page.rect.width / 2]
    return titles, logos


def wrap_title(title, font, size, max_width):
    """Wrap onto as few lines as fit max_width, then balance them: of all the
    ways to break into that many lines, take the one whose longest line is
    shortest. Greedy wrapping alone strands a last word ('... Egg & Chick' /
    'Quota'). A word longer than max_width gets a line to itself."""
    from itertools import combinations
    words = title.split()
    greedy = []
    for word in words:
        if greedy and font.text_length(greedy[-1] + " " + word, size) <= max_width:
            greedy[-1] += " " + word
        else:
            greedy.append(word)
    n = len(greedy)
    if n < 2 or len(words) > 20:
        return greedy
    best, best_w = greedy, max(font.text_length(ln, size) for ln in greedy)
    for cuts in combinations(range(1, len(words)), n - 1):
        bounds = (0,) + cuts + (len(words),)
        lines = [" ".join(words[a:b]) for a, b in zip(bounds, bounds[1:])]
        widest = max(font.text_length(ln, size) for ln in lines)
        if widest < best_w:
            best, best_w = lines, widest
    return best


def plan_header(page, title, fonts, args):
    """Lay out the new header on page 1 and work out how far the page content
    must move down to clear it. Reads the page, changes nothing, so it runs in
    --dry-run too. Returns (plan, issues); plan is None if the header can't go
    on, with the reason in issues.

    Layout follows the reference form (Authorization to Release Financial
    Information): logo top-left on the content's left edge, title right-aligned
    to the content's right edge and centred on the logo, lines 1.25 x the size
    apart. The content keeps --header-gap points clear below the header."""
    if page.rotation:
        return None, [("HEADER_SKIPPED", f"page 1 is rotated {page.rotation} degrees; "
                                         "header placement is untested there")]
    old_titles, old_logos = find_old_header(page, args.old_header_zone,
                                            args.old_title_min_size)
    others = [fitz.Rect(i["bbox"]) for i in page.get_image_info()
              if fitz.Rect(i["bbox"]) not in old_logos]
    for r in old_logos:
        if any(r.intersects(o) for o in others):
            return None, [("HEADER_SKIPPED", f"old logo at {r} overlaps another image, "
                                             "so it can't be removed on its own")]
    ext = content_extent(page, [t["bbox"] for t in old_titles] + old_logos)
    if ext is None:
        return None, [("HEADER_SKIPPED", "page 1 has no content")]

    alias, path, font = fonts["bold"]          # wrapping measures in bold too
    size = args.title_size
    lead = size * 1.25
    asc, desc = font.ascender * size, -font.descender * size

    def layout(top_of_logo):
        logo = fitz.Rect(ext.x0, top_of_logo, ext.x0 + args.logo_size,
                         top_of_logo + args.logo_size)
        texts = wrap_title(title, font, size, args.title_width)
        block = (len(texts) - 1) * lead + asc + desc
        first = (logo.y0 + logo.y1) / 2 - block / 2 + asc
        lines = []
        for i, text in enumerate(texts):
            base, w = first + i * lead, font.text_length(text, size)
            lines.append({"text": text, "origin": fitz.Point(ext.x1 - w, base),
                          "rect": fitz.Rect(ext.x1 - w, base - asc, ext.x1, base + desc)})
        return logo, lines

    logo, lines = layout(args.logo_top)
    clash = [r for r in [logo] + [ln["rect"] for ln in lines] if r.intersects(BUTTON_ZONE)]
    if clash:          # drop the whole header below the signing-button row
        logo, lines = layout(args.logo_top + BUTTON_ZONE.y1 + 2 - min(r.y0 for r in clash))
    if min(ln["rect"].x0 for ln in lines) < logo.x1 + 12:
        return None, [("HEADER_SKIPPED", f"title {title!r} is too wide to sit beside "
                                         "the logo; shorten it or lower --title-width")]

    bottom = max([logo.y1] + [ln["rect"].y1 for ln in lines])
    dy = max(0.0, bottom + args.header_gap - ext.y0)
    free = page.rect.height - args.min_bottom_margin - ext.y1
    if dy > free:
        return None, [("HEADER_NO_ROOM",
                       f"content must move down {dy:.1f}pt to clear the header but only "
                       f"{max(free, 0):.1f}pt is free below it (bottom margin kept at "
                       f"{args.min_bottom_margin:g}pt)")]

    issues = [("header_old_title_removed",
               f"{''.join(s['text'] for s in t['spans']).strip()!r} "
               f"({t['spans'][0]['font']} {t['spans'][0]['size']:.1f}pt)") for t in old_titles]
    issues += [("header_old_logo_removed", f"image at x={r.x0:.0f}-{r.x1:.0f}, "
                                           f"y={r.y0:.0f}-{r.y1:.0f}") for r in old_logos]
    issues.append(("header", f"title {title!r} in {len(lines)} line(s) at {size:g}pt, "
                             f"{args.logo_size:g}pt logo placeholder, content moved down "
                             f"{dy:.1f}pt"))
    return {"old_titles": old_titles, "old_logos": old_logos, "logo": logo,
            "lines": lines, "bottom": bottom, "dy": dy, "size": size,
            "font": (alias, path)}, issues


def remove_old_header(page, plan, pad):
    """Take the old title and logo off page 1, in two redaction passes so the
    image pass can't touch text and the text pass can't touch images. Returns
    the removed glyphs as census allowances."""
    expected = Counter()
    if plan["old_titles"]:
        for t in plan["old_titles"]:
            b = t["bbox"]
            page.add_redact_annot(fitz.Rect(b.x0 - pad, b.y0 - pad, b.x1 + pad, b.y1 + pad),
                                  fill=None, cross_out=False)
            for sp in t["spans"]:
                for ch in sp["text"]:
                    if not ch.isspace():
                        expected[(sp["origin"][1], ord(ch))] -= 1
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                              graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                              text=fitz.PDF_REDACT_TEXT_REMOVE)
    if plan["old_logos"]:
        for r in plan["old_logos"]:
            page.add_redact_annot(r, fill=None, cross_out=False)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_REMOVE,
                              graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                              text=fitz.PDF_REDACT_TEXT_NONE)
    return expected


def shift_down(page, dy):
    """Move everything on the page down by dy points: the content stream under a
    translation, each annotation through its coordinates. A field's appearance
    is placed by its /Rect, so fields move without being redrawn. The signing
    widgets stay where the requirements tool put them. Returns issues for
    anything it couldn't move."""
    doc = page.parent
    issues = []
    # PDF space runs bottom-up, so down the page is negative y.
    head, tail = doc.get_new_xref(), doc.get_new_xref()
    for xref, data in ((head, f"q 1 0 0 1 0 {-dy:.4f} cm\n"), (tail, "\nQ\n")):
        doc.update_object(xref, "<<>>")
        doc.update_stream(xref, data.encode())
    kind, val = doc.xref_get_key(page.xref, "Contents")
    body = val.strip()[1:-1] if kind == "array" else val if kind == "xref" else ""
    doc.xref_set_key(page.xref, "Contents", f"[{head} 0 R {body} {tail} 0 R]")

    for xref, _, _ in page.annot_xrefs():
        if field_name(doc, xref) in SIGNING_WIDGETS:
            continue
        for key in ("Rect", "QuadPoints", "L", "Vertices", "CL"):     # flat x y x y ...
            kind, val = doc.xref_get_key(xref, key)
            if kind != "array":
                continue
            nums = [float(v) for v in val.strip()[1:-1].split()]
            nums = [v - dy if i % 2 else v for i, v in enumerate(nums)]
            doc.xref_set_key(xref, key, "[" + " ".join(f"{v:.4f}" for v in nums) + "]")
        if doc.xref_get_key(xref, "InkList")[0] != "null":
            issues.append(("HEADER_ANNOT_NOT_MOVED",
                           f"ink annotation xref {xref} left in place"))
    return issues


def draw_header(page, plan):
    """Draw the logo placeholder and title. Returns the title's glyphs as census
    allowances, keyed in pre-move coordinates like every other allowance."""
    alias, path = plan["font"]
    page.draw_rect(plan["logo"], color=None, fill=(0, 0, 0), overlay=True)
    expected = Counter()
    for ln in plan["lines"]:
        page.insert_text(ln["origin"], ln["text"], fontname=alias, fontfile=path,
                         fontsize=plan["size"], color=(0, 0, 0), overlay=True)
        for ch in ln["text"]:
            if not ch.isspace():
                expected[(ln["origin"].y - plan["dy"], ord(ch))] += 1
    return expected


def check_header(page, plan):
    """On the saved page 1, nothing but the header may sit above the header's
    bottom edge. Catches a move that didn't take, or a field left behind."""
    ext = content_extent(page, [plan["logo"]] + [ln["rect"] for ln in plan["lines"]])
    if ext is not None and ext.y0 < plan["bottom"]:
        return [("HEADER_OVERLAP", f"page content starts at y={ext.y0:.1f}, above the "
                                   f"header's bottom edge at y={plan['bottom']:.1f}")]
    return []


# --------------------------------------------------------------------------
# Spelling
# --------------------------------------------------------------------------

ALLOWLIST = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "spelling_allowlist.txt")
WORD_EDGE = "\"'‘’“”()[]{}<>.,;:!?*#%&$+=|~^`"


def load_speller(allow_path):
    """pyspellchecker's English and French dictionaries (Canadian forms are
    often bilingual) plus our allowlist, lower-cased."""
    try:
        from spellchecker import SpellChecker
    except ImportError:
        sys.exit("--spellcheck needs pyspellchecker: pip install pyspellchecker")
    allow = set()
    if allow_path and os.path.exists(allow_path):
        with open(allow_path, encoding="utf-8") as fh:
            for line in fh:
                word = line.split("#", 1)[0].strip().lower()
                if word:
                    allow.add(word)
    return SpellChecker(), SpellChecker(language="fr"), allow


ROMAN = re.compile(r"x{0,3}(ix|iv|v?i{0,3})", re.I)     # list numbering, i to xxxix
DATE_MASK = re.compile(r"[YMD]{4,}", re.I)              # YYYYMMDD, DDMMYYYY


def words_in(text):
    """The words in some text worth spell-checking. Skips anything with a
    digit, mail-merge fields, e-mail and web addresses, dotted abbreviations
    (O.B.H.E.C.C.), all-capital words of five letters or fewer (acronyms),
    Roman numerals and date masks."""
    text = re.sub(r"([a-z])-[ \t]*\r?\n[ \t]*([a-z])", r"\1\2", text)   # re-join hyphenated breaks
    out = []
    for raw in re.split(r"[\s/]+|[-–—]+", text):
        if not raw or re.search(r"[0-9@«»_]|www|\.(com|ca|org|net)", raw, re.I):
            continue
        word = raw.strip(WORD_EDGE)
        if word.lower().endswith(("'s", "’s")):
            word = word[:-2]
        if len(word) < 2 or not re.fullmatch(r"[A-Za-z]+(?:['’][A-Za-z]+)*", word):
            continue
        if word.isupper() and len(word) <= 5:
            continue
        if ROMAN.fullmatch(word) or DATE_MASK.fullmatch(word):
            continue
        out.append(word)
    return out


def spelling_issues(doc, title, speller):
    """(page, detail) for each word the dictionary doesn't know, once per word
    per file, with a count and a suggestion. The title (the file name) is
    checked too, because --header puts it on the form."""
    checker, french, allow = speller
    seen = {}                                       # lower -> [as written, count, page]
    sources = [("", title)] + [(pno, p.get_text("text")) for pno, p in enumerate(doc, start=1)]
    for where, text in sources:
        for word in words_in(text):
            key = word.lower()
            if key in allow:
                continue
            if key in seen:
                seen[key][1] += 1
            else:
                seen[key] = [word, 1, where]
    out = []
    for key in sorted(french.unknown(checker.unknown(seen))):
        word, count, where = seen[key]
        hint = checker.correction(key)
        hint = f"-> {hint!r}?" if hint and hint != key else "(no suggestion)"
        place = "in the file name" if where == "" else f"x{count}"
        out.append((where, f"{word!r} {place} {hint}"))
    return out


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


def process_file(src, dst, fonts, args, speller=None):
    doc = fitz.open(src)
    name = os.path.basename(src)
    title = os.path.splitext(name)[0]
    rows = []

    if speller:
        rows += [[name, where, "spelling", detail]
                 for where, detail in spelling_issues(doc, title, speller)]

    # The header is planned before anything is touched: it decides how far
    # page 1's content moves, which the census has to know up front.
    header, dys = None, [0.0] * doc.page_count
    if args.header and doc.page_count:
        header, notes = plan_header(doc[0], title, fonts, args)
        rows += [[name, 1, issue, detail] for issue, detail in notes]
        if header:
            dys[0] = header["dy"]

    # Census every page before anything is touched. It's checked against the
    # saved file, not the page in memory: after redaction the in-memory page
    # stops drawing form-field appearances, so a field label redrawn on top of
    # itself would cancel out of the count and pass.
    census_before = [] if args.dry_run else [glyph_census(p, dy) for p, dy in zip(doc, dys)]
    expected = [Counter() for _ in range(doc.page_count)]

    size = args.size
    if args.best_fit:
        size, culprit = best_fit_size(doc, fonts, args.size, args.min_size,
                                      args.right_margin, args.quantise)
        if size < args.size:
            rows.append([name, "", "size_reduced",
                         f"{args.size}pt -> {size}pt, limited by: {culprit!r}"])

    if header and not args.dry_run:
        expected[0].update(remove_old_header(doc[0], header, args.pad))

    for pno, page in enumerate(doc, start=1):
        issues, allowed = restyle_page(
                page, fonts, size, args.pad, args.centre_tol, args.min_indent,
                args.width_tolerance, args.skip_conforming, args.target_font,
                args.size_tolerance, args.dry_run, args.gap_tolerance,
                not args.no_recover)
        expected[pno - 1].update(allowed)      # update, not +=: allowances can be negative
        for issue, detail in issues:
            rows.append([name, pno, issue, detail])

    if header and not args.dry_run:
        page = doc[0]
        if header["dy"]:
            rows += [[name, 1, issue, detail] for issue, detail in shift_down(page, header["dy"])]
        expected[0].update(draw_header(page, header))

    if not args.dry_run:
        rows += [[name, "", issue, detail] for issue, detail in fix_tounicode(doc, fonts)]
        doc.save(dst, garbage=3, deflate=True)
    doc.close()

    if not args.dry_run:
        with fitz.open(dst) as out, fitz.open(src) as orig:
            if out.page_count != len(census_before):
                rows.append([name, "", "PAGE_COUNT_CHANGED",
                             f"{len(census_before)} -> {out.page_count}"])
            for pno, page in enumerate(out, start=1):
                if pno > len(census_before):
                    break
                for issue, detail in verify_census(census_before[pno - 1],
                                                   glyph_census(page),
                                                   page.rect.height, expected[pno - 1],
                                                   dys[pno - 1]):
                    rows.append([name, pno, issue, detail])
            if header:
                rows += [[name, 1, issue, detail] for issue, detail in check_header(out[0], header)]
            # Proofs come from the saved file too: the in-memory page has lost
            # its form fields by now, so it isn't what ships.
            if args.png:
                for pno in range(min(orig.page_count, out.page_count)):
                    save_proof(orig[pno].get_pixmap(dpi=args.png_dpi),
                               out[pno].get_pixmap(dpi=args.png_dpi),
                               os.path.join(args.png, f"{title}_p{pno + 1}_proof.png"))
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
    hd = ap.add_argument_group("header (--header)")
    hd.add_argument("--header", action="store_true",
                    help="give page 1 a logo placeholder top-left and the file name as the "
                         "title top-right, clear of the signing-button row")
    hd.add_argument("--logo-size", type=float, default=63.0,
                    help="side of the square logo placeholder, points (default 63, the "
                         "Libro logo's height on the reference form)")
    hd.add_argument("--logo-top", type=float, default=18.0,
                    help="top of the logo, points from the top of the page (default 18)")
    hd.add_argument("--title-size", type=float, default=14.0, help="title size (default 14)")
    hd.add_argument("--title-width", type=float, default=220.0,
                    help="wrap the title onto another line past this width (default 220)")
    hd.add_argument("--header-gap", type=float, default=8.0,
                    help="space kept between the header and the page content (default 8)")
    hd.add_argument("--min-bottom-margin", type=float, default=18.0,
                    help="content is never moved closer than this to the bottom edge; if the "
                         "header needs more room the file is reported instead (default 18)")
    hd.add_argument("--old-header-zone", type=float, default=100.0,
                    help="an existing logo or title must lie within this many points of "
                         "the top to be replaced (default 100)")
    hd.add_argument("--old-title-min-size", type=float, default=12.0,
                    help="text this size or larger in that zone counts as the old title "
                         "(default 12)")
    sc = ap.add_argument_group("spelling (--spellcheck)")
    sc.add_argument("--spellcheck", action="store_true",
                    help="report words the dictionary doesn't know; never changes text")
    sc.add_argument("--spell-allow", default=ALLOWLIST,
                    help="words never to flag, one per line (default spelling_allowlist.txt "
                         "beside this script)")
    args = ap.parse_args()

    fonts = resolve_fonts({k: getattr(args, f"font_{k}") for k in
                           ("regular", "bold", "italic", "bolditalic")
                           if getattr(args, f"font_{k}")})
    print(f"Using: {fonts['regular'][1]}")
    speller = load_speller(args.spell_allow) if args.spellcheck else None

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
            rows, used = process_file(src, dst, fonts, args, speller)
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

    if args.header:
        with_header = {r[0] for r in all_rows if r[2] == "header"}
        print(f"Header on {len(with_header)}/{len(pairs)} file(s).")
        for issue, n in sorted(Counter(r[2] for r in all_rows
                                       if r[2].startswith("HEADER_")).items()):
            print(f"  {n} {issue}")
    if args.spellcheck:
        spelled = [r for r in all_rows if r[2] == "spelling"]
        print(f"Spelling: {len(spelled)} word(s) to check in "
              f"{len({r[0] for r in spelled})} file(s).")

    clean =len(pairs) - len({r[0] for r in all_rows}) - len(failed)
    print(f"\n{len(pairs)} file(s): {clean} clean, "
          f"{len({r[0] for r in all_rows})} with flagged lines, {len(failed)} failed.")


if __name__ == "__main__":
    main()
