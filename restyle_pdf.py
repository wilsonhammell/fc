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

--reflow lets prose paragraphs change their line breaks when they won't fit at
the size: each is re-wrapped from its first baseline into the space it and the
gap below it already take up, and nothing else on the page moves. Paragraphs
that can't be re-wrapped that way are reported, and with --best-fit they alone
decide how far the size comes down.

--header gives page 1 the house header: the Libro logo top-left (assets/libro_logo.png) and
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
    """Arial draws some plain characters with a glyph it shares with another
    code point: space with the no-break space, hyphen with the soft hyphen,
    semicolon with the Greek question mark. When PyMuPDF embeds the font it
    maps each shared glyph back to the later code point, so restyled text
    copies, searches and reads aloud wrongly - 'Pre-authorized' comes out
    'Preauthorized', and every ';' is a Greek letter. In every font this
    script embedded, point any glyph that plain ASCII uses back at the ASCII
    character. Returns issues for any mapping it couldn't fix."""
    targets = {}                                    # alias -> {glyph: ascii code}
    for alias, path, font in fonts.values():
        ascii_of = {}
        for code in range(0x20, 0x7F):
            glyph = font.has_glyph(code)
            if glyph and glyph not in ascii_of:
                ascii_of[glyph] = code
        targets[alias] = ascii_of
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
            ascii_of = targets[alias]
            for i, line in enumerate(lines):
                parts = line.split()
                hexes = [p[1:-1] for p in parts if p.startswith("<") and p.endswith(">")]
                if len(parts) == 2 and len(hexes) == 2:                  # bfchar
                    glyph, uni = int(hexes[0], 16), int(hexes[1], 16)
                    if glyph in ascii_of and uni != ascii_of[glyph]:
                        lines[i] = f"<{hexes[0]}> <{ascii_of[glyph]:04x}>"
                elif len(parts) == 3 and len(hexes) == 3:                # bfrange
                    lo, hi, first = (int(h, 16) for h in hexes)
                    for glyph, code in ascii_of.items():
                        if lo <= glyph <= hi and first + glyph - lo != code:
                            issues.append(("TOUNICODE_NOT_FIXED",
                                           f"{alias}: glyph {glyph} (ASCII {chr(code)!r}) maps "
                                           f"to U+{first + glyph - lo:04X} inside a range; "
                                           "copied text will show that instead"))
            doc.update_stream(tu, "\n".join(lines).encode("latin-1"))
    return issues


def drawable(text, font):
    """Whether the target font can draw every character in text. U+FFFD means
    the PDF itself doesn't know what the glyph is (a font with no text
    mapping), so nothing can redraw it."""
    return all(ch.isspace() or (ch != "\ufffd" and font.has_glyph(ord(ch))) for ch in text)


def save_proof(before, after, path, caption=""):
    """Write a side-by-side before/after image for visual review, captioned
    with the file, the page and the size the document settled on, so a
    reviewer can tell at a glance which ones are worth a closer look."""
    from PIL import Image, ImageDraw, ImageFont
    a = Image.frombytes("RGB", (before.width, before.height), before.samples)
    b = Image.frombytes("RGB", (after.width, after.height), after.samples)
    gap, bar = 24, 30 if caption else 0
    canvas = Image.new("RGB", (a.width + b.width + gap,
                               max(a.height, b.height) + bar), (210, 210, 210))
    canvas.paste(a, (0, bar))
    canvas.paste(b, (a.width + gap, bar))
    if caption:
        draw = ImageDraw.Draw(canvas)
        draw.rectangle([0, 0, canvas.width, bar], fill=(32, 32, 32))
        try:
            font = ImageFont.truetype("arial.ttf", 17)
        except OSError:                                  # bitmap fallback
            font = ImageFont.load_default()
        draw.text((10, 6), caption, fill=(255, 255, 255), font=font)
    canvas.save(path)


# --------------------------------------------------------------------------
# Page processing
# --------------------------------------------------------------------------

def printed(page):
    """The page's printed content only: a copy with its annotations dropped,
    at the same coordinates. Text extraction also reads what form fields
    display - a pre-filled value, a signature tab's SIGN - and restyling
    that onto the page would draw it a second time under the field's own
    copy. Everything that decides what gets restyled reads this instead."""
    tmp = fitz.open()
    tmp.insert_pdf(page.parent, from_page=page.number, to_page=page.number)
    tmp.xref_set_key(tmp[0].xref, "Annots", "null")
    return tmp.reload_page(tmp[0])


def collect_lines(page, skip_conforming, target_font_substr, target_size, size_tol):
    """Gather every horizontal text line with its spans. Returns (lines, notes).

    A span that's filled and also stroked is faux bold - a regular font drawn
    with an outline to thicken it, as Word does when a font has no bold - and
    is marked bold here, so everything downstream draws it in the bold face."""
    lines, notes = [], []
    data = page.get_text("dict")
    stroked = {(round(c[2][0], 1), round(c[2][1], 1)) for run in page.get_texttrace()
               if run.get("type") == 1 for c in run.get("chars", [])}

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
                if (round(sp["origin"][0], 1), round(sp["origin"][1], 1)) in stroked:
                    sp = dict(sp, flags=sp["flags"] | 16)
                spans.append(sp)
            if spans:
                lines.append({"bbox": fitz.Rect(line["bbox"]), "spans": spans,
                              "block_bbox": fitz.Rect(block["bbox"])})
    return lines, notes


def census_band(y, page_height, bands=40):
    """Horizontal band a glyph falls in, keyed on its baseline y."""
    return min(bands - 1, max(0, int(y / max(page_height, 1) * bands)))


def glyph_in(bbox, rect):
    """Whether a glyph, given its box from get_texttrace(), belongs inside rect.
    Judged by the box's centre, not the glyph's origin: the origin sits on the
    baseline, and some fonts report a line box that ends exactly on the
    baseline - where a point test counts as outside, so whole lines were taken
    for hidden text and drawn twice."""
    return fitz.Point((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2) in rect


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


def ink_right(page):
    """Right edge of the printed text's glyphs (their advance boxes), or None.
    The line boxes the extractor reports can reach further, over trailing
    spaces, so both the planner and the check use this."""
    xs = [c[3][2] for run in printed(page).get_texttrace() if run.get("type") in (0, 1)
          for c in run.get("chars", []) if c[0] > 0 and not chr(c[0]).isspace()]
    return max(xs) if xs else None


def rule_segments(page):
    """Thin drawn things text can collide with: ruled lines, box edges, and
    rules drawn as slivers of fill."""
    out = []
    for d in page.get_drawings():
        fill = d.get("fill")
        if d["type"] in ("f", "fs") and fill and min(fill) < 0.99 and d["rect"].height < 4:
            out.append(fitz.Rect(d["rect"]))
        if d["type"] not in ("s", "fs"):
            continue
        w = max(d.get("width") or 1.0, 1.0) / 2
        for item in d["items"]:
            if item[0] == "l":
                p, q = item[1], item[2]
                out.append(fitz.Rect(min(p.x, q.x) - w, min(p.y, q.y) - w,
                                     max(p.x, q.x) + w, max(p.y, q.y) + w))
            elif item[0] == "re":
                b = fitz.Rect(item[1])
                out += [fitz.Rect(b.x0 - w, b.y0 - w, b.x1 + w, b.y0 + w),
                        fitz.Rect(b.x0 - w, b.y1 - w, b.x1 + w, b.y1 + w),
                        fitz.Rect(b.x0 - w, b.y0 - w, b.x0 + w, b.y1 + w),
                        fitz.Rect(b.x1 - w, b.y0 - w, b.x1 + w, b.y1 + w)]
    return out


def rule_crossings(page, band=20.0):
    """Characters whose body (cap height down to the baseline) crosses a ruled
    line or a box edge. Descender space is left out, so an underline sitting
    just under the text doesn't count. Returns (count, worst y)."""
    rows = {}
    for seg in rule_segments(page):
        for k in range(int(seg.y0 // band), int(seg.y1 // band) + 1):
            rows.setdefault(k, []).append(seg)
    hits, where = 0, None
    for run in printed(page).get_texttrace():
        if run.get("type") not in (0, 1):
            continue
        for c in run.get("chars", []):
            if c[0] <= 0 or chr(c[0]).isspace():
                continue
            base, size = c[2][1], run["size"]
            body = fitz.Rect(c[3][0], base - 0.72 * size, c[3][2], base + 0.08 * size)
            near = rows.get(int(body.y0 // band), []) + rows.get(int(body.y1 // band), [])
            if any(body.intersects(s) for s in near):
                hits += 1
                where = where if where is not None else round(base)
    return hits, where


def verify_rules(before, after):
    """Text must not end up crossing a ruled line or box edge it didn't cross
    before: grown text can break out of a box it used to sit inside, which
    neither the census nor the right-edge check can see."""
    was, _ = rule_crossings(before)
    now, where = rule_crossings(after)
    if now <= was:
        return []
    return [("TEXT_CROSSES_RULE", f"{now - was} more character(s) sit on a ruled line or box "
                                  f"edge than before ({was} -> {now}), first near y={where}")]


def verify_right_edge(before, after, page_margin=None, tolerance=2.0):
    """On the saved page, no printed text may reach past the right edge the
    original text used (or --page-margin's, if that's further) - let alone
    off the page. The census can't see this: a glyph run off the edge still
    counts as present. Form fields are left out on both sides."""
    was, now = ink_right(before), ink_right(after)
    if was is None or now is None:
        return []
    allowed = max(was, after.rect.width - page_margin) if page_margin is not None else was
    if now <= allowed + tolerance:
        return []
    where = "off the page" if now > after.rect.width else "past the right edge"
    return [("TEXT_PAST_EDGE", f"text runs {where}: reaches x={now:.0f}, allowed "
                               f"x={allowed:.0f}, page width {after.rect.width:.0f}")]


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
                 and not any(glyph_in(c[3], r) for r in known_rects)]
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
                 recover=True, reflow=None, rules=None, fine=None):
    """Restyle one page. Returns (issues, expected): a list of (issue, detail)
    tuples, and the glyph-count changes made on purpose, keyed by (baseline y,
    character), for the census. From plan_reflow: `reflow` maps line_key ->
    layout for lines whose paragraph is re-wrapped rather than redrawn in
    place, and `rules` gives fill-in rules a new start and end. `fine` maps
    line_key -> the smaller size for footers and fine print."""
    reflow, rules, fine = reflow or {}, rules or [], fine or {}
    issues = []
    content = printed(page)
    lines, notes = collect_lines(content, skip_conforming, target_font_substr, size, size_tol)
    issues.extend(notes)
    if not lines:
        return issues, Counter()

    page_centre = page.rect.width / 2
    text_left = min(l["bbox"].x0 for l in lines)
    text_right = max(l["bbox"].x1 for l in lines)
    ink_end = dict(zip((id(l) for l in lines), ink_ends(content, lines)))

    # --- plan every line before touching the page ------------------------
    plan = []
    inline = {r["inline"]: r for r in rules if "inline" in r}
    for ln in lines:
        if line_key(ln) in reflow:
            continue            # erased with the rest, redrawn with its paragraph
        # A line ending in typed underscores is drawn as its label, then the
        # rule rebuilt apart where plan_reflow put it.
        spans, rule = ln["spans"], None
        if line_key(ln) in inline:
            spans, style, _ = split_trailing_rule(ln["spans"])
            rule = (inline[line_key(ln)], style)
        line_size = fine.get(line_key(ln), size)
        old_w = ln["bbox"].width
        new_w = sum(style_of(sp["flags"], fonts)[2].text_length(sp["text"], line_size)
                    for sp in spans)
        text_preview = "".join(sp["text"] for sp in ln["spans"])[:60]

        # Centred-line detection: sits near the page centre and is clearly
        # indented from the text margin. Re-centre these so titles and footers
        # don't jump left when the text gets narrower.
        looks_centred = (abs((ln["bbox"].x0 + ln["bbox"].x1) / 2 - page_centre) < centre_tol
                         and (ln["bbox"].x0 - text_left) > min_indent
                         and alone_on_row(ln, lines))
        flush_right = (not looks_centred and rule is None
                       and looks_right_aligned(ln["bbox"], text_left, text_right))

        if new_w > old_w * keep_size_ratio:
            issues.append(("line_got_wider",
                           f"{text_preview!r} {old_w:.0f}pt -> {new_w:.0f}pt"))

        plan.append({"line": ln, "spans": spans, "rule": rule, "size": line_size, "new_w": new_w,
                     "old_w": old_w, "centred": looks_centred, "right": flush_right})

    if dry_run:
        return issues, Counter()

    orphans = recover_orphans(content, [ln["bbox"] for ln in lines]) if recover else []
    if orphans:
        issues.append(("recovered_hidden_text",
                       f"{len(orphans)} run(s) invisible to the text extractor, redrawn"))

    # Glyphs Arial can't draw - a character it hasn't got, or one the PDF
    # can't even name - stay exactly as they were, in their own font.
    def kept(sp):
        return not drawable(sp["text"], style_of(sp["flags"], fonts)[2])

    orphans = [o for o in orphans if drawable(o["text"], style_of(o["flags"], fonts)[2])]
    left = [sp for ln in lines for sp in ln["spans"] if kept(sp)]
    if left:
        shown = ", ".join(sorted({repr(ch) for sp in left for ch in sp["text"] if not ch.isspace()}))
        issues.append(("kept_original_glyphs", f"{len(left)} span(s) Arial can't draw left in "
                                               f"their own font: {shown[:80]}"))

    # --- erase all original text in one pass ------------------------------
    rects = [o["rect"] for o in orphans]
    for ln in lines:
        if any(kept(sp) for sp in ln["spans"]):
            rects += [fitz.Rect(sp["bbox"]) for sp in ln["spans"] if not kept(sp)]
        else:
            b = fitz.Rect(ln["bbox"])
            rects.append(fitz.Rect(b.x0 - pad, b.y0 - pad, b.x1 + pad, b.y1 + pad))
    for r in rects:
        page.add_redact_annot(r, fill=None, cross_out=False)

    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                          graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                          text=fitz.PDF_REDACT_TEXT_REMOVE)

    # --- redraw ------------------------------------------------------------
    expected = Counter()        # glyph-count changes made on purpose
    for item in plan:
        ln = item["line"]
        if item["centred"]:
            # keep the visual centre, shift every span by the same amount
            dx = (page_centre - item["new_w"] / 2) - ln["bbox"].x0
        elif item["right"]:
            # keep the right edge where it was, and grow to the left
            end = redrawn_end(item["spans"], fonts, item["size"], gap_tol)
            dx = ink_end[id(ln)] - end if end is not None else 0.0
        else:
            dx = 0.0

        prev_end_old = None      # right edge of previous span, original layout
        prev_end_new = None      # right edge of previous span, as redrawn
        line_max = max((sp["size"] for sp in item["spans"]), default=item["size"])
        for sp in item["spans"]:
            alias, path, font = style_of(sp["flags"], fonts)
            at = span_size(sp, line_max, item["size"])
            x, y = sp["origin"]

            # If this span butted up against the previous one, it's continuous
            # text (e.g. a bold phrase inside a sentence) and must be pushed
            # along as the earlier spans grow. If there was a real gap, it's a
            # column or tab stop, so keep its original x.
            if prev_end_old is not None and (sp["bbox"][0] - prev_end_old) < gap_tol:
                x = prev_end_new - dx

            if kept(sp):                    # left where it was, in its own font
                prev_end_old = prev_end_new = sp["bbox"][2]
                continue
            page.insert_text(fitz.Point(x + dx, y), sp["text"],
                             fontname=alias, fontfile=path,
                             fontsize=at, color=span_colour(sp), overlay=True)

            prev_end_old = sp["bbox"][2]
            prev_end_new = x + dx + font.text_length(sp["text"], at)

        if item["rule"]:
            cut, style = item["rule"]
            alias, path, font = style_of(style["flags"], fonts)
            n = max(1, int(round((cut["x1"] - cut["x0"])
                                 / (font.text_length("_", item["size"]) or 1))))
            page.insert_text(fitz.Point(cut["x0"], cut["y"]), "_" * n, fontname=alias,
                             fontfile=path, fontsize=item["size"], color=span_colour(style),
                             overlay=True)
            expected[(cut["y"], ord("_"))] += n - cut["count"]

    # Re-wrapped paragraphs. Their glyphs are all still there, just on other
    # baselines, so the census is told where each one left and arrived.
    for layout in {id(v): v for v in reflow.values()}.values():
        for x, y, text, sp, at in layout["pieces"]:
            alias, path, font = style_of(sp["flags"], fonts)
            page.insert_text(fitz.Point(x, y), text, fontname=alias, fontfile=path,
                             fontsize=at, color=span_colour(sp), overlay=True)
            for ch in text:
                if not ch.isspace():
                    expected[(y, ord(ch))] += 1
        for sp in layout["spans"]:
            for ch in sp["text"]:
                if not ch.isspace():
                    expected[(sp["origin"][1], ord(ch))] -= 1

    for o in orphans:
        alias, path, font = style_of(o["flags"], fonts)
        text, origin = o["text"], fitz.Point(*o["origin"])
        if o["is_rule"] and o["width"] > 0:
            # rebuild the rule to its original length at the new glyph width,
            # or to the span plan_reflow gave it when its label needed room
            width = o["width"]
            for cut in rules:
                if "inline" in cut:
                    continue
                if abs(cut["y"] - origin.y) < 0.5 and abs(cut["old_x0"] - o["rect"].x0) < 1:
                    origin, width = fitz.Point(cut["x0"], origin.y), cut["x1"] - cut["x0"]
            unit = font.text_length("_", size) or 1
            text = "_" * max(1, int(round(width / unit)))
            expected[(o["origin"][1], ord("_"))] += len(text) - len(o["text"])
        page.insert_text(origin, text,
                         fontname=alias, fontfile=path,
                         fontsize=size, color=tuple(o["color"]), overlay=True)

    return issues, expected


# --------------------------------------------------------------------------
# Footers and fine print: a second, smaller size
# --------------------------------------------------------------------------

FOOTER_TEXT = re.compile(r"\bpage\s*\d+\s*(of\s*\d+)?\b|\brev(ised|ision)?\.?\s*\d|©", re.I)


def find_fine_print(doc, args):
    """Footers and fine print, per page: {line_key: (kind, size, text)}, where
    kind is "footer" or "fine".

    Only printed text counts, not what form fields display. The body size is the document's commonest size by
    character count. A footer sits in the bottom tenth of the page and
    either repeats at the same spot on another page, reads like a page
    number, revision or copyright line, or is set smaller than the body.
    Fine print is anything else set at 85% of the body size or less
    (captions under signature lines, notes). Every one is listed in the
    report, since this is a judgement from layout alone."""
    def text(ln):
        return "".join(s["text"] for s in ln["spans"]).strip()

    def norm(t):                          # 'Page 1 of 3' and 'Page 2 of 3' match
        return re.sub(r"\s+", " ", re.sub(r"\d+", "#", t)).strip().lower()

    pages, sizes = [], Counter()
    for page in doc:
        lines, _ = collect_lines(printed(page), False, args.target_font, args.size,
                                 args.size_tolerance)
        for ln in lines:
            for s in ln["spans"]:
                sizes[round(s["size"] * 2) / 2] += len(s["text"].strip())
        pages.append((page.rect.height, lines))
    if not sizes:
        return [{} for _ in pages]
    body = sizes.most_common(1)[0][0]

    spots = {}                            # normalised text -> [(page, y0)]
    for pi, (_, lines) in enumerate(pages):
        for ln in lines:
            spots.setdefault(norm(text(ln)), []).append((pi, ln["bbox"].y0))
    out = []
    for pi, (height, lines) in enumerate(pages):
        found = {}
        for ln in lines:
            t, y0 = text(ln), ln["bbox"].y0
            size = max(s["size"] for s in ln["spans"])
            small = size <= body * 0.85
            running = len({p for p, y in spots[norm(t)] if abs(y - y0) <= 6}) >= 2
            if y0 >= height * 0.9 and (running or FOOTER_TEXT.search(t) or small):
                found[line_key(ln)] = ("footer", size, t)
            elif small:
                found[line_key(ln)] = ("fine", size, t)
        out.append(found)
    return out


# --------------------------------------------------------------------------
# Reflow: re-wrap prose paragraphs inside the space they already occupy
# --------------------------------------------------------------------------

# The first word of a list item: 1, 1., (a), a), (iv), a bullet, NOTE:. When a
# gap sets it apart it stays put, and the text wraps at the indent after it.
MARKER = re.compile(r"\(?[0-9]{1,2}[.)]?|\(?[A-Za-z][.)]|\([ivxIVX]{1,4}\)|[•·▪*–-]|NOTE:?|Note:?")


def line_key(line):
    """A text line's identity across re-extraction: its first span's origin."""
    x, y = line["spans"][0]["origin"]
    return (round(x, 1), round(y, 1))


def span_colour(sp):
    rgb = sp.get("color", 0)
    return (((rgb >> 16) & 255) / 255, ((rgb >> 8) & 255) / 255, (rgb & 255) / 255)


def line_width(spans, fonts, size):
    """Width of a line's text up to its last ink: trailing spaces don't count,
    since they draw nothing (Word pads lines with them)."""
    spans = list(spans)
    while spans and not spans[-1]["text"].strip():
        spans.pop()
    if not spans:
        return 0.0
    line_max = max(s["size"] for s in spans)
    last = dict(spans[-1], text=spans[-1]["text"].rstrip())
    return sum(style_of(s["flags"], fonts)[2].text_length(s["text"], span_size(s, line_max, size))
               for s in spans[:-1] + [last])


def span_size(sp, line_max, size):
    """The size a span is set at. One set smaller than the rest of its line -
    a superscript, a subscript, small print inside a sentence - keeps that
    proportion instead of being blown up to the body size."""
    if line_max <= 0 or sp["size"] >= line_max * 0.9:
        return size
    return round(size * sp["size"] / line_max, 2)


def alone_on_row(line, lines):
    """Whether nothing else shares this line's baseline. A line in one column
    of a two-column block can straddle the page centre by accident, and
    re-centring it would drag it over its neighbour."""
    base = line["spans"][0]["origin"][1]
    return not any(other is not line and abs(other["spans"][0]["origin"][1] - base) <= 2
                   for other in lines)


def looks_right_aligned(bbox, text_left, text_right):
    """A short line set flush against the right edge of the text - a page
    number, a date, 'rev. 05/12'. It keeps its right edge and grows to the
    left; growing right, it would run past the edge on its own."""
    column = text_right - text_left
    return (abs(bbox.x1 - text_right) <= 4 and bbox.width < 0.5 * column
            and bbox.x0 - text_left > 0.4 * column)


def ink_ends(page, lines):
    """Where each line's glyphs actually end. The line box can reach further,
    over trailing spaces, so a flush-right line is anchored on this."""
    chars = [c for run in page.get_texttrace() if run.get("type") in (0, 1)
             for c in run.get("chars", []) if c[0] > 0 and not chr(c[0]).isspace()]
    ends = []
    for ln in lines:
        xs = [c[3][2] for c in chars if glyph_in(c[3], ln["bbox"])]
        ends.append(max(xs) if xs else ln["bbox"].x1)
    return ends


def redrawn_end(spans, fonts, size, gap_tol, dx=0.0):
    """Where restyle_page's redraw puts the end of a line's last glyph: spans
    that touched stay touching and are pushed along, spans after a gap keep
    their own x. Must follow restyle_page's placement exactly. None if the
    spans draw nothing."""
    prev_old = prev_new = end = None
    line_max = max((sp["size"] for sp in spans), default=size)
    for sp in spans:
        font = style_of(sp["flags"], fonts)[2]
        at = span_size(sp, line_max, size)
        x = sp["origin"][0]
        if prev_old is not None and sp["bbox"][0] - prev_old < gap_tol:
            x = prev_new - dx
        if sp["text"].strip():
            end = x + dx + font.text_length(sp["text"].rstrip(), at)
        prev_old, prev_new = sp["bbox"][2], x + dx + font.text_length(sp["text"], at)
    return end


def split_trailing_rule(spans):
    """Split a line's spans into its label and the run of underscores that ends
    it ('Borrower's Initials _________'). Returns (label spans, the span the
    underscores were set in or None, how many underscores came off)."""
    out, removed, style = [dict(s) for s in spans], 0, None
    while out:
        s = out[-1]
        body = s["text"].rstrip()
        label = body.rstrip("_")
        n = len(body) - len(label)
        if n == 0 and body:
            break                                   # ends in ordinary text
        if n:
            removed += n
            style = style or s
        if label.strip():
            s["text"] = label.rstrip()
            break
        out.pop()
    return out, style, removed


def paragraph_words(para_lines):
    """A paragraph's text as words to wrap. A word is a list of (text, span)
    pieces, so one that changes style part-way (O.B.H.E.C.C. then a bold 's)
    stays whole. Spans separated by a visible gap are separate words even
    when the space between them was its own (dropped) span. A line ending in
    '-' runs into the next without a space; those joins are returned so the
    report can show them."""
    words, joins = [], []
    for li, line in enumerate(para_lines):
        space = not (li and words and words[-1][-1][0].endswith("-"))
        if not space:
            joins.append(len(words) - 1)
        prev_end = None
        for sp in line["spans"]:
            if prev_end is not None and sp["bbox"][0] - prev_end > 0.15 * sp["size"]:
                space = True
            for part in re.split(r"(\s+)", sp["text"]):
                if not part:
                    continue
                if part.isspace():
                    space = True
                    continue
                if space or not words:
                    words.append([(part, sp)])
                else:
                    words[-1].append((part, sp))
                space = False
            prev_end = sp["bbox"][2]
    return words, ["".join(t for t, _ in words[k]) for k in joins]


def find_paragraphs(page, lines, limit, args):
    """Group text lines into prose paragraphs, conservatively. A line runs on
    into the next when it reached at least three-quarters of the way to the
    right edge, and the next is the first line below it in the paragraph's
    column, within three line-heights, the same size, starting at a steady
    indent no more than 60pt in from the first line, and not a list marker.
    Paragraphs are kept even when they can't be re-wrapped - tab gaps (so
    columns, not prose), fill-in rules, or text --skip-conforming leaves alone
    - with the reason in "bad", so the report can say why."""
    per_line = [[] for _ in lines]
    for w in page.get_text("words"):
        c = fitz.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2)
        for i, ln in enumerate(lines):
            if c in ln["bbox"]:
                per_line[i].append(w)
                break
    for ws in per_line:
        ws.sort(key=lambda w: w[0])

    def base(i):
        return lines[i]["spans"][0]["origin"][1]

    def size(i):
        return max(s["size"] for s in lines[i]["spans"])

    order = sorted(range(len(lines)), key=lambda i: (base(i), lines[i]["bbox"].x0))
    text_left = min(ln["bbox"].x0 for ln in lines)
    centre = page.rect.width / 2
    used, paras = set(), []
    for i in order:
        if i in used or not per_line[i]:
            continue
        group, first = [i], lines[i]["bbox"]
        used.add(i)
        while True:
            last = lines[group[-1]]["bbox"]
            if last.x1 < last.x0 + 0.75 * (limit - last.x0):
                break                                       # a short line ends it
            below = [j for j in order if base(j) > base(group[-1]) + 0.5
                     and lines[j]["bbox"].x1 > first.x0 and lines[j]["bbox"].x0 < limit]
            if not below:
                break
            j = below[0]
            nb = lines[j]["bbox"]
            if (j in used or not per_line[j]
                    or base(j) - base(group[-1]) > 3 * size(group[-1])
                    or abs(size(j) - size(i)) > 0.6
                    # a hanging indent or a first-line indent, up to 60pt either way
                    or not first.x0 - 60 <= nb.x0 <= first.x0 + 60
                    or (len(group) > 1 and abs(nb.x0 - lines[group[1]]["bbox"].x0) > 3)
                    or MARKER.fullmatch(per_line[j][0][4])):
                break
            group.append(j)
            used.add(j)

        pl = [lines[k] for k in group]
        ws0 = per_line[i]
        marker = (len(ws0) > 1 and MARKER.fullmatch(ws0[0][4]) is not None
                  and ws0[1][0] - ws0[0][2] >= 0.4 * size(i))
        bad = None
        for n, k in enumerate(group):
            gaps = [b[0] - a[2] for a, b in zip(per_line[k], per_line[k][1:])]
            if n == 0 and marker:
                gaps = gaps[1:]
            if any(g > 1.2 * size(k) for g in gaps):
                bad = "it has tab gaps, so it's probably columns rather than prose"
        if any("___" in s["text"] for ln in pl for s in ln["spans"]):
            bad = "it holds a fill-in rule"
        if args.skip_conforming and any(
                args.target_font.lower() in s["font"].lower()
                and abs(s["size"] - args.size) <= args.size_tolerance
                for ln in pl for s in ln["spans"]):
            bad = "--skip-conforming leaves part of it as it is"
        words, joins = paragraph_words(pl)
        if marker and "".join(t for t, _ in words[0]) != ws0[0][4]:
            marker = False
        text_x = ws0[1][0] if marker else ws0[0][0]
        bases = [base(k) for k in group]
        pitches = sorted(b - a for a, b in zip(bases, bases[1:]))
        paras.append({
            "lines": group, "words": words, "joins": joins, "marker": int(marker),
            "marker_x": ws0[0][0], "text_x": text_x,
            "cont_x": per_line[group[1]][0][0] if len(group) > 1 else text_x,
            "base": bases[0], "pitch": pitches[len(pitches) // 2] if pitches else None,
            "centred": (all(abs((ln["bbox"].x0 + ln["bbox"].x1) / 2 - centre) < args.centre_tol
                            for ln in pl) and first.x0 - text_left > args.min_indent),
            "bad": bad, "olds": [(base(k), lines[k]["bbox"]) for k in group],
            "bbox": fitz.Rect(min(ln["bbox"].x0 for ln in pl), min(ln["bbox"].y0 for ln in pl),
                              max(ln["bbox"].x1 for ln in pl), max(ln["bbox"].y1 for ln in pl)),
            "preview": " ".join("".join(t for t, _ in w) for w in words)[:50],
            "spans": [s for ln in pl for s in ln["spans"]],
        })
    return paras


def reflow_obstacles(page, lines):
    """What a re-wrapped line mustn't run into, apart from other text lines
    (those depend on the size, so they're added per attempt): ruled lines and
    box edges, filled shapes, images, and text the extractor can't see, like
    fill-in rules. Form fields don't count - they get placed afresh in the
    requirements tool, so only what's printed on the page constrains the
    layout. Each is (rect, what, can_contain): a filled shape or image that
    wholly contains a paragraph is its background, and the paragraph has to
    stay inside it."""
    obs = []
    for d in page.get_drawings():
        fill = d.get("fill")
        if d["type"] in ("f", "fs") and fill and min(fill) < 0.99:
            obs.append((fitz.Rect(d["rect"]), "a filled shape", True))
        if d["type"] in ("s", "fs"):
            w = max(d.get("width") or 1.0, 1.0) / 2
            for item in d["items"]:
                if item[0] == "l":
                    segs = [(item[1], item[2])]
                elif item[0] == "re":
                    r = fitz.Rect(item[1])
                    segs = [(r.tl, r.tr), (r.bl, r.br), (r.tl, r.bl), (r.tr, r.br)]
                else:                                   # curves, quads: their box
                    obs.append((fitz.Rect(d["rect"]), "a drawn shape", False))
                    continue
                for p, q in segs:
                    obs.append((fitz.Rect(min(p.x, q.x) - w, min(p.y, q.y) - w,
                                          max(p.x, q.x) + w, max(p.y, q.y) + w),
                                "a ruled line", False))
    obs += [(fitz.Rect(i["bbox"]), "an image", True) for i in page.get_image_info()]
    known = [ln["bbox"] for ln in lines]
    for run in page.get_texttrace():
        if run.get("type") not in (0, 1):
            continue
        ink = [fitz.Rect(c[3]) for c in run.get("chars", [])
               if c[0] > 0 and not chr(c[0]).isspace()
               and not any(glyph_in(c[3], k) for k in known)]
        if ink:
            obs.append((fitz.Rect(min(r.x0 for r in ink), min(r.y0 for r in ink),
                                  max(r.x1 for r in ink), max(r.y1 for r in ink)),
                        "text the extractor can't see (a fill-in rule?)", False))
    return obs


def layout_paragraph(para, size, fonts, limit, obstacles, cap, min_leading, centre):
    """Wrap `para` at `size` with its first line on its original baseline and
    the rest below it, as far apart as the original's lines where there's
    room and never closer than min_leading x size (or the original spacing,
    if that was already tighter). Every new line is checked against
    `obstacles` and must end above `cap`. Returns (layout, None) or
    (None, why not)."""
    reg = fonts["regular"][2]
    space = reg.text_length(" ", size)
    asc, desc = reg.ascender * size, -reg.descender * size

    para_max = max(sp["size"] for sp in para["spans"])

    def width(word):
        return sum(style_of(sp["flags"], fonts)[2].text_length(t, span_size(sp, para_max, size))
                   for t, sp in word)

    if para["centred"]:
        # stay inside the band the centred text spanned, wrapping rather
        # than spreading out towards whatever sits either side of it
        half = min(limit - centre, max(centre - para["bbox"].x0, para["bbox"].x1 - centre))
        starts = (centre - half, centre - half)
        limit = centre + half
    else:
        starts = (para["text_x"], para["cont_x"])
    rows, cur, used = [], [], 0.0
    for word in para["words"][para["marker"]:]:
        w = width(word)
        start = starts[1] if rows else starts[0]
        if cur and start + used + space + w > limit:
            rows.append((cur, used))
            cur, used, start = [], 0.0, starts[1]
        if not cur and start + w > limit:
            return None, f"{''.join(t for t, _ in word)!r} is too long for a line"
        used = used + space + w if cur else w
        cur.append((word, w))
    rows.append((cur, used))

    def row_x(n, used):
        return centre - used / 2 if para["centred"] else starts[1 if n else 0]

    containers = [o for o, _, c in obstacles
                  if c and para["bbox"] in fitz.Rect(o.x0 - 1, o.y0 - 1, o.x1 + 1, o.y1 + 1)]
    others = [(o, what) for o, what, _ in obstacles if not any(o is c for c in containers)]

    def new_ground(x, y, used):
        """The part of a line that no original line of the paragraph covered.
        A line on an original baseline only claims what reaches past that
        line's ends; whatever the original already sat close to (a field
        box 1pt under it, say) is the form's own layout, not a new clash."""
        r = fitz.Rect(x, y - asc - 1, x + used, y + desc + 1)
        old = next((b for base, b in para["olds"] if abs(base - y) < 0.5), None)
        if old is None:
            return [r]
        parts = []
        if r.x0 < old.x0:
            parts.append(fitz.Rect(r.x0, r.y0, old.x0, r.y1))
        if r.x1 > old.x1:
            parts.append(fitz.Rect(old.x1, r.y0, r.x1, r.y1))
        return parts

    def blocked(pitch):
        for n, (_, used) in enumerate(rows):
            for r in new_ground(row_x(n, used), para["base"] + n * pitch, used):
                if r.y1 > cap:
                    return "the bottom of the page's content"
                for c in containers:
                    if r not in c:
                        return "the edge of the box it sits in"
                for o, what in others:
                    if r.intersects(o):
                        return what
        return None

    prefer = para["pitch"] or 1.2 * size
    floor = min(min_leading * size, prefer)
    pitch = prefer
    while True:
        why = blocked(pitch)
        if why is None:
            break
        pitch -= 0.25
        if len(rows) == 1 or pitch < floor - 1e-9:
            return None, f"as {len(rows)} line(s) it runs into {why}"

    pieces = []
    if para["marker"]:
        x = para["marker_x"]
        for t, sp in para["words"][0]:
            at = span_size(sp, para_max, size)
            pieces.append((x, para["base"], t, sp, at))
            x += style_of(sp["flags"], fonts)[2].text_length(t, at)
    for n, (row, used) in enumerate(rows):
        x, y = row_x(n, used), para["base"] + n * pitch
        for k, (word, _) in enumerate(row):
            if k:
                x += space
            for t, sp in word:
                at = span_size(sp, para_max, size)
                pieces.append((x, y, t, sp, at))
                x += style_of(sp["flags"], fonts)[2].text_length(t, at)
    return {"pieces": pieces, "spans": para["spans"], "rows": len(rows), "pitch": pitch}, None


def fill_ins(page, lines):
    """The printed blanks a form wants written on: fill-in rules drawn as
    underscores the extractor can't see. Each is {"rect", "kind", "origin"}.
    Rules typed as part of a line's own text are found in line_room."""
    return [{"rect": fitz.Rect(o["rect"]), "kind": "rule", "origin": o["origin"]}
            for o in recover_orphans(page, [ln["bbox"] for ln in lines]) if o["is_rule"]]


def after_on_row(line, r):
    """Whether r sits on the line's row and follows its text: it overlaps the
    band around the baseline, starts in the line's right half (a rule that
    starts just before the text ends still counts) and runs past its end."""
    base = line["spans"][0]["origin"][1]
    size = max(s["size"] for s in line["spans"])
    b = line["bbox"]
    return (r.y1 > base - 0.7 * size and r.y0 < base + 0.3 * size
            and r.x0 > b.x0 + 0.5 * b.width and r.x1 > b.x1 + 1)


def line_room(page, lines, static, right, args):
    """How far each line may run at any size. A line stops before the next
    thing on its row - text in another column, a box edge, a checkbox - or
    at `right` if nothing follows it. A blank after a label (an underscore
    rule) isn't a stop: the label may push into it, shortening it from the
    left, so each line also gets that blank. Form fields are ignored."""
    blanks = fill_ins(page, lines)
    trace = [(c[0], c[2], c[3]) for run in page.get_texttrace() if run.get("type") in (0, 1)
             for c in run.get("chars", [])]
    text_left = min(ln["bbox"].x0 for ln in lines)
    text_right = max(ln["bbox"].x1 for ln in lines)
    ends = ink_ends(page, lines)
    centre = page.rect.width / 2
    out = []
    for i, ln in enumerate(lines):
        b = ln["bbox"]
        base = ln["spans"][0]["origin"][1]
        size = max(s["size"] for s in ln["spans"])

        def before_on_row(r):
            return r.y1 > base - 0.7 * size and r.y0 < base + 0.3 * size and r.x1 <= b.x0 + 1

        left_stop = max([text_left] + [o.x1 for o, _, _ in static if before_on_row(o)]
                        + [other["bbox"].x1 for j, other in enumerate(lines)
                           if j != i and before_on_row(other["bbox"])])
        mine = [f for f in blanks if after_on_row(ln, f["rect"])]
        # Underscores typed as part of the line itself are a blank as well:
        # the label is measured without them and the rule is rebuilt apart.
        label, _, removed = split_trailing_rule(ln["spans"])
        label_end, inline = b.x1, None
        if removed >= 3:
            chars = sorted((c for c in trace if glyph_in(c[2], b)), key=lambda c: c[1][0])
            k, run = len(chars) - 1, []
            while k >= 0 and chr(chars[k][0]).isspace():
                k -= 1
            while k >= 0 and chars[k][0] == 95:
                run.append(fitz.Rect(chars[k][2]))
                k -= 1
            while k >= 0 and chr(chars[k][0]).isspace():
                k -= 1
            if len(run) >= 3:
                label_end = chars[k][2][2] if k >= 0 else b.x0
                inline = fitz.Rect(min(r.x0 for r in run), min(r.y0 for r in run),
                                   max(r.x1 for r in run), max(r.y1 for r in run))
                mine = [f for f in mine if not f["rect"].intersects(inline)] + [
                    {"rect": inline, "kind": "inline", "key": line_key(ln),
                     "count": removed, "base": ln["spans"][0]["origin"][1]}]
        if mine:        # the first blank after the text, and whatever overlaps it
            first = min(mine, key=lambda f: f["rect"].x0)
            mine = [f for f in mine if f["rect"].x0 <= first["rect"].x1]
        in_blank = [f["rect"] for f in mine]
        stops = [o for o, _, _ in static if after_on_row(ln, o)
                 and not any(o.intersects(r) for r in in_blank)]
        stops += [other["bbox"] for j, other in enumerate(lines)
                  if j != i and after_on_row(ln, other["bbox"])]
        stop = min([right] + [o.x0 for o in stops])
        hard = stop - max(0.0, min(3.0, stop - b.x1)) if stop < right else right
        blank = None
        if mine:
            bx0, bx1 = min(r.x0 for r in in_blank), max(r.x1 for r in in_blank)
            gap = bx0 - label_end               # as the form had it, unless it overlapped
            blank = {"x0": bx0, "x1": bx1, "reach": max(bx1, hard), "parts": mine,
                     "gap": min(4.0, gap) if gap >= 0 else 1.5}
        centred = (abs((b.x0 + b.x1) / 2 - centre) < args.centre_tol
                   and b.x0 - text_left > args.min_indent and alone_on_row(ln, lines))
        out.append({"hard": hard, "blank": blank, "left_stop": left_stop, "ink_end": ends[i],
                    "label": label if inline else ln["spans"], "centred": centred,
                    "right": (not centred and not inline
                              and looks_right_aligned(b, text_left, text_right))})
    return out


def plan_reflow(doc, fonts, args, fine=None):
    """Best fit with structural changes allowed. Steps down from --size by
    --quantise to --min-size and takes the first size at which every line
    fits its room (see line_room): where it is, by shortening the blank
    after it, or by re-wrapping its paragraph in the space the paragraph
    already takes up. Lines that fit as they are keep their line breaks.
    Without --best-fit only --size is tried. Reads the document, changes
    nothing. Returns (size, plans, rows): plans[page index] holds "reflow"
    (line_key -> layout) and "rules" for restyle_page; rows are (page, issue,
    detail) for the report. Footers and fine print (`fine`, per page:
    line_key -> their size) are measured at their own size and never hold the
    body size down."""
    fine = fine or [{} for _ in doc]
    pages = []
    for page in doc:
        page = printed(page)
        lines, _ = collect_lines(page, False, args.target_font, args.size, args.size_tolerance)
        if not lines:
            pages.append(None)
            continue
        right = args.right_margin or ink_right(page) or max(ln["bbox"].x1 for ln in lines)
        if args.page_margin is not None and not args.right_margin:
            right = max(right, page.rect.width - args.page_margin)
        static = reflow_obstacles(page, lines)
        ext = content_extent(page)
        pages.append({"lines": lines, "limit": right, "static": static,
                      "paras": find_paragraphs(page, lines, right, args),
                      "room": line_room(page, lines, static, right, args),
                      # never reach lower than the page's content already does,
                      # so --header's room check still holds
                      "cap": min(ext.y1 if ext else page.rect.height,
                                 page.rect.height - args.min_bottom_margin),
                      "centre": page.rect.width / 2})

    def preview(line):
        return "".join(s["text"] for s in line["spans"]).strip()[:50]

    def attempt(size):
        plans = [{"reflow": {}, "rules": [], "cuts": []} for _ in pages]
        blockers = []
        for pi, pg in enumerate(pages):
            if pg is None:
                continue
            lines, limit = pg["lines"], pg["limit"]
            small = fine[pi]
            widths = [line_width(room["label"], fonts, small.get(line_key(ln), size))
                      for ln, room in zip(lines, pg["room"])]
            over, why = set(), {}
            for i, ln in enumerate(lines):
                if line_key(ln) in small:
                    continue                # its size doesn't depend on this one
                room = pg["room"][i]
                if room["right"] and room["label"]:
                    # anchored on the right: it may only grow to the left
                    span = (redrawn_end(room["label"], fonts, size, args.gap_tolerance)
                            - room["label"][0]["origin"][0])
                    if room["ink_end"] - span < room["left_stop"] + 2:
                        over.add(i)
                        why[i] = "it's right-aligned and would grow into what's to its left"
                    continue
                if room["centred"]:
                    end = pg["centre"] + widths[i] / 2
                else:
                    end = (redrawn_end(room["label"], fonts, size, args.gap_tolerance)
                           if room["label"] else None) or ln["spans"][0]["origin"][0]
                blank = room["blank"]
                if blank is None:
                    if end > room["hard"] + 0.01:
                        over.add(i)
                    continue
                width = blank["x1"] - blank["x0"]
                if end + blank["gap"] <= blank["x0"] + 0.01:
                    start, stop = blank["x0"], blank["x1"]        # clears it as it is
                else:
                    # Push the blank's start right. Its end may move right too,
                    # into free room, but only as far as it takes to win back
                    # its width.
                    start = end + blank["gap"]
                    stop = min(blank["reach"], max(blank["x1"], start + width))
                    keep, need = stop - start, max(36.0, args.min_fill * width)
                    if keep < need:
                        over.add(i)
                        why[i] = (f"the blank after it would shrink to {max(keep, 0):.0f}pt "
                                  f"(it may go down to {need:.0f}pt)")
                        continue
                    plans[pi]["cuts"].append((preview(ln), width, keep, start - blank["x0"]))
                for part in blank["parts"]:
                    r = part["rect"]
                    new = {"x0": max(start, r.x0), "x1": max(r.x1, stop)}
                    if part["kind"] == "inline":
                        # always rebuilt apart from its label, so it keeps its
                        # ends instead of changing length with the font
                        plans[pi]["rules"].append(dict(new, inline=part["key"],
                                                       count=part["count"], y=part["base"]))
                    elif start > blank["x0"]:
                        plans[pi]["rules"].append(dict(new, y=part["origin"][1], old_x0=r.x0))
            if not over:
                continue
            owner = {k: p for p in pg["paras"] for k in p["lines"]}
            for i in sorted(over):
                if i in why or i not in owner:
                    blockers.append((pi + 1, preview(lines[i]),
                                     why.get(i, "it isn't part of a paragraph")))
            todo = {id(owner[i]): owner[i] for i in over if i in owner and i not in why}
            moving = {k for p in todo.values() for k in p["lines"]}
            text_obs = []
            for j, ln in enumerate(lines):
                b = ln["bbox"]
                if j in moving:           # may end up anywhere across the column
                    r = fitz.Rect(owner[j]["bbox"].x0, b.y0, limit, b.y1)
                elif pg["room"][j]["right"]:        # grows to the left
                    r = fitz.Rect(min(b.x0, b.x1 - widths[j]), b.y0, b.x1, b.y1)
                else:                     # redrawn in place, possibly wider
                    r = fitz.Rect(b.x0, b.y0, max(b.x1, b.x0 + widths[j]), b.y1)
                text_obs.append((j, r, "".join(s["text"] for s in ln["spans"]).strip()[:30]))
            for para in todo.values():
                if not para["bad"] and not all(
                        drawable(sp["text"], style_of(sp["flags"], fonts)[2]) for sp in para["spans"]):
                    para["bad"] = "it holds characters Arial can't draw, which have to stay put"
                if para["bad"]:
                    blockers.append((pi + 1, para["preview"], para["bad"]))
                    continue
                obst = pg["static"] + [(r, f"the line {t!r}", False)
                                       for j, r, t in text_obs if j not in para["lines"]]
                layout, why = layout_paragraph(para, size, fonts, limit, obst, pg["cap"],
                                               args.min_leading, pg["centre"])
                if layout is None:
                    blockers.append((pi + 1, para["preview"], why))
                    continue
                layout["para"] = para
                for k in para["lines"]:
                    plans[pi]["reflow"][line_key(lines[k])] = layout
        return plans, blockers

    step = args.quantise or 0.1
    sizes = [args.size]
    if args.best_fit:
        while sizes[-1] - step >= args.min_size - 1e-9:
            sizes.append(round(sizes[-1] - step, 2))
        if sizes[-1] > args.min_size:
            sizes.append(args.min_size)
    tried = []
    for size in sizes:
        plans, blockers = attempt(size)
        tried.append((size, blockers))
        if not blockers:
            break

    rows = []
    if size < args.size or blockers:
        failed_at, why = next((s, b) for s, b in reversed(tried) if b)
        summary = (f"{args.size}pt -> {size}pt" if size < args.size else f"{size}pt")
        rows.append(("", "size_reduced" if size < args.size else "reflow_incomplete",
                     f"{summary}; at {failed_at}pt {len(why)} line(s) or paragraph(s) had no "
                     "room" + ("" if not blockers else f", and {len(blockers)} still "
                               f"don't fit at {size}pt")))
        rows += [(pg, "reflow_blocked", f"at {failed_at}pt: {text!r} - {reason}")
                 for pg, text, reason in why]
    for pi, plan in enumerate(plans):
        rows += [(pi + 1, "blank_moved" if new >= old - 0.5 else "blank_shortened",
                  f"{text!r}: the blank after it starts {moved:.0f}pt later"
                  + (f" and keeps its {old:.0f}pt width" if new >= old - 0.5
                     else f", {old:.0f} -> {new:.0f}pt wide"))
                 for text, old, new, moved in plan["cuts"]]
        for layout in {id(v): v for v in plan["reflow"].values()}.values():
            p = layout["para"]
            spacing = (f", line spacing {p['pitch']:.1f} -> {layout['pitch']:.1f}pt"
                       if p["pitch"] and layout["rows"] > 1 else "")
            joined = f", joined across a line break: {', '.join(p['joins'])}" if p["joins"] else ""
            rows.append((pi + 1, "reflowed", f"{p['preview']!r}: {len(p['lines'])} -> "
                                             f"{layout['rows']} line(s){spacing}{joined}"))
    return size, plans, rows


# --------------------------------------------------------------------------
# Header: logo top-left, title top-right, room for signing buttons
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
    top `zone` points and set at `min_title_size` or larger. A logo is an image
    that *starts* within that band, no wider than half the page and no taller
    than a fifth of it - a letterhead logo often hangs below the band, and
    requiring it to fit entirely left the old logo on the page under the new
    one. This is a guess, so every removal is reported."""
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
             if i["bbox"][1] <= zone and fitz.Rect(i["bbox"]).width <= page.rect.width / 2
             and fitz.Rect(i["bbox"]).height <= page.rect.height / 5]
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
    # The title ends where the text column does, not at the furthest drawing
    # (a box edge can stand out past any text and past the page margin).
    ink = [c[3][2] for run in printed(page).get_texttrace() if run.get("type") in (0, 1)
           for c in run.get("chars", []) if c[0] > 0 and not chr(c[0]).isspace()
           and not any(glyph_in(c[3], t["bbox"]) for t in old_titles)]
    right = max(ink) if ink else ext.x1
    if args.page_margin is not None:
        right = max(right, page.rect.width - args.page_margin)
    right = min(right, ext.x1)
    size = args.title_size
    lead = size * 1.25
    logo_file = args.logo if args.logo and os.path.exists(args.logo) else None
    aspect = 1.0                                    # the placeholder is square
    if logo_file:
        pix = fitz.Pixmap(logo_file)
        aspect = pix.width / pix.height
    asc, desc = font.ascender * size, -font.descender * size

    def layout(top_of_logo):
        logo = fitz.Rect(ext.x0, top_of_logo, ext.x0 + args.logo_size * aspect,
                         top_of_logo + args.logo_size)
        texts = wrap_title(title, font, size, args.title_width)
        block = (len(texts) - 1) * lead + asc + desc
        first = (logo.y0 + logo.y1) / 2 - block / 2 + asc
        lines = []
        for i, text in enumerate(texts):
            base, w = first + i * lead, font.text_length(text, size)
            lines.append({"text": text, "origin": fitz.Point(right - w, base),
                          "rect": fitz.Rect(right - w, base - asc, right, base + desc)})
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
    what = (f"logo {os.path.basename(logo_file)} at {logo.width:.0f}x{logo.height:.0f}pt"
            if logo_file else f"{args.logo_size:g}pt logo placeholder")
    issues.append(("header", f"title {title!r} in {len(lines)} line(s) at {size:g}pt, "
                             f"{what}, content moved down {dy:.1f}pt"))
    return {"old_titles": old_titles, "old_logos": old_logos, "logo": logo,
            "logo_file": logo_file, "lines": lines, "bottom": bottom, "dy": dy,
            "size": size, "font": (alias, path)}, issues


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
        # Line art lying wholly inside the title goes too: an underlined title
        # is drawn as text plus a separate rule, and the rule on its own would
        # be left floating under the new header.
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                              graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
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
    """Draw the logo (or its black placeholder) and title. Returns the title's
    glyphs as census allowances, keyed in pre-move coordinates like every
    other allowance."""
    alias, path = plan["font"]
    if plan["logo_file"]:
        page.insert_image(plan["logo"], filename=plan["logo_file"], overlay=True)
    else:
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
    bottom edge, and no second logo may be left on the page. Catches a move
    that didn't take, a field left behind, or an old letterhead logo that
    wasn't recognised and now sits under ours."""
    issues = []
    ext = content_extent(page, [plan["logo"]] + [ln["rect"] for ln in plan["lines"]])
    if ext is not None and ext.y0 < plan["bottom"]:
        issues.append(("HEADER_OVERLAP", f"page content starts at y={ext.y0:.1f}, above the "
                                         f"header's bottom edge at y={plan['bottom']:.1f}"))
    others = [fitz.Rect(i["bbox"]) for i in page.get_image_info()
              if fitz.Rect(i["bbox"]).width <= page.rect.width / 2
              and fitz.Rect(i["bbox"]).height <= page.rect.height / 5
              and not fitz.Rect(i["bbox"]).intersects(plan["logo"])
              and fitz.Rect(i["bbox"]).y0 <= page.rect.height / 3]
    for r in others:
        issues.append(("LOGO_LEFT_ON_PAGE",
                       f"an image is still on page 1 at x={r.x0:.0f}-{r.x1:.0f}, "
                       f"y={r.y0:.0f}-{r.y1:.0f}; if it's the old logo there are now two"))
    return issues


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

def best_fit_size(doc, fonts, target, min_size, right_margin, quantise, fine=None):
    """Largest size <= target at which no line exceeds the document's own right
    edge. Width scales linearly with point size, so this is arithmetic, not a
    search. Footers and fine print (`fine`, per page) have their own size and
    don't take part. Returns (size, binding_line_description)."""
    best, culprit = target, None
    for pi, page in enumerate(doc):
        skip = fine[pi] if fine else {}
        lines = []
        for block in printed(page).get_text("dict")["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block["lines"]:
                if abs(line.get("dir", (1, 0))[1]) > 1e-6:
                    continue
                spans = [s for s in line["spans"] if s["text"].strip()]
                if spans and line_key({"spans": spans}) not in skip:
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

    # Footers and fine print get the smaller size, or keep their own if that
    # was smaller still; they're found before anything is touched.
    found = find_fine_print(doc, args) if args.fine_size else [{} for _ in doc]
    fine = [{k: min(s, args.fine_size) for k, (_, s, _) in f.items()} for f in found]
    for kind, label in (("footer", "footer"), ("fine", "fine_print")):
        hits = [(pi + 1, t) for pi, f in enumerate(found) for k, _, t in f.values() if k == kind]
        if hits:
            pages = sorted({p for p, _ in hits})
            examples = list(dict.fromkeys(re.sub(r"\d+", "#", t) for _, t in hits))
            rows.append([name, "", label,
                         f"{len(hits)} line(s) on page(s) {','.join(map(str, pages))} set at up "
                         f"to {args.fine_size:g}pt: " + "; ".join(repr(e[:40]) for e in examples[:5])
                         + (" ..." if len(examples) > 5 else "")])
    size, plans = args.size, [{} for _ in range(doc.page_count)]
    if args.reflow:
        size, plans, notes = plan_reflow(doc, fonts, args, fine)
        rows += [[name, where, issue, detail] for where, issue, detail in notes]
    elif args.best_fit:
        size, culprit = best_fit_size(doc, fonts, args.size, args.min_size,
                                      args.right_margin, args.quantise, fine)
        if size < args.size:
            rows.append([name, "", "size_reduced",
                         f"{args.size}pt -> {size}pt, limited by: {culprit!r}"])
    # Fine print is never set larger than the body it sits under.
    fine = [{k: min(s, size) for k, s in f.items()} for f in fine]

    if header and not args.dry_run:
        expected[0].update(remove_old_header(doc[0], header, args.pad))

    for pno, page in enumerate(doc, start=1):
        issues, allowed = restyle_page(
                page, fonts, size, args.pad, args.centre_tol, args.min_indent,
                args.width_tolerance, args.skip_conforming, args.target_font,
                args.size_tolerance, args.dry_run, args.gap_tolerance,
                not args.no_recover, plans[pno - 1].get("reflow"), plans[pno - 1].get("rules"),
                fine[pno - 1])
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
                for issue, detail in verify_right_edge(orig[pno - 1], page, args.page_margin):
                    rows.append([name, pno, issue, detail])
                for issue, detail in verify_rules(orig[pno - 1], page):
                    rows.append([name, pno, issue, detail])
            if header:
                rows += [[name, 1, issue, detail] for issue, detail in check_header(out[0], header)]
            # Proofs come from the saved file too: the in-memory page has lost
            # its form fields by now, so it isn't what ships.
            if args.png:
                for pno in range(min(orig.page_count, out.page_count)):
                    save_proof(orig[pno].get_pixmap(dpi=args.png_dpi),
                               out[pno].get_pixmap(dpi=args.png_dpi),
                               os.path.join(args.png,
                                            f"{title} [{size:g}pt] p{pno + 1}_proof.png"),
                               f"{name}   page {pno + 1} of {out.page_count}   "
                               f"set at {size:g}pt   (original left, restyled right)")
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
    ap.add_argument("--min-size", type=float, default=6.0,
                    help="the smallest size --best-fit may pick for the whole document; "
                         "text that still doesn't fit at it is reported as TEXT_PAST_EDGE "
                         "(default 6)")
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
    ap.add_argument("--fine-size", type=float, default=8.0,
                    help="footers (page numbers, revision lines, running footers) and fine "
                         "print (text set well below the body size) get this size, or keep "
                         "their own if it was smaller; they don't hold the body size down "
                         "(default 8; 0 treats them like any other text)")
    rf = ap.add_argument_group("reflow (--reflow)")
    rf.add_argument("--reflow", action="store_true",
                    help="re-wrap prose paragraphs that won't fit at the size, inside the "
                         "space they already take up, rather than shrinking the whole "
                         "document for them; with --best-fit, shrink only as far as the "
                         "paragraphs that can't be re-wrapped need")
    rf.add_argument("--min-leading", type=float, default=1.1,
                    help="tightest line spacing in a re-wrapped paragraph, as a multiple "
                         "of the size (default 1.1)")
    rf.add_argument("--page-margin", type=float,
                    help="let lines with nothing after them run out to this many points "
                         "from the page's right edge (36 = half an inch); by default they "
                         "stop where the document's own text did")
    rf.add_argument("--min-fill", type=float, default=0.5,
                    help="a blank after a label (a fill-in rule) may be shortened "
                         "from the left to make room for the label, down to this fraction of "
                         "its width and never under 36pt (default 0.5)")
    hd = ap.add_argument_group("header (--header)")
    hd.add_argument("--header", action="store_true",
                    help="give page 1 the logo top-left and the file name as the "
                         "title top-right, clear of the signing-button row")
    hd.add_argument("--logo", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                   "assets", "libro_logo.png"),
                    help="logo image for the top-left (default assets/libro_logo.png beside "
                         "this script); if the file doesn't exist, or --logo \"\" is given, a "
                         "black square placeholder is drawn instead")
    hd.add_argument("--logo-size", type=float, default=63.0,
                    help="height of the logo, points; its width follows the image's "
                         "proportions (default 63, the Libro logo's height on the reference form)")
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
            failed.append((os.path.basename(src), f"{type(e).__name__}: {e}"))
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
