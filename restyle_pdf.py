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
import json
import os
import re
import unicodedata
import sys
import glob
from collections import Counter
from types import SimpleNamespace

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


MARK_COLOURS = {"swap": ((255, 232, 66, 80), (196, 148, 0)),      # wording replaced
                "gone": ((255, 96, 96, 70), (200, 40, 40)),       # taken off the page
                "join": ((86, 190, 255, 80), (0, 118, 186)),      # rule run back to its caption
                "field": ((110, 210, 120, 80), (30, 140, 50)),    # merge tag now a text box
                "sign": ((190, 140, 255, 60), (120, 60, 200))}    # signing area replaced


def proof_font(size):
    """A readable face for the proof furniture, whatever the machine has."""
    from PIL import ImageFont
    for name in ("arialbd.ttf", "arial.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def mark_up(img, marks, dpi):
    """Shade the places where the words themselves changed, so a reviewer
    scrolling through proofs can see what to check without hunting for it.
    Rects are in points, the image is in pixels.

    A mark may carry a number. The same number is put on both sides of the
    proof for the same change, so a reviewer can tell at a glance which
    highlight over here answers which highlight over there - on a page with
    nine swaps, matching them up by eye is the slow part."""
    from PIL import Image, ImageDraw
    if not marks:
        return img
    scale = dpi / 72.0
    over = Image.new("RGBA", img.size, (0, 0, 0, 0))
    pen = ImageDraw.Draw(over)
    tags = []
    for mark in marks:
        rect, kind = mark[0], mark[1]
        tag = mark[2] if len(mark) > 2 else None
        fill, edge = MARK_COLOURS.get(kind, MARK_COLOURS["swap"])
        box = [rect.x0 * scale - 2, rect.y0 * scale - 2,
               rect.x1 * scale + 2, rect.y1 * scale + 2]
        pen.rectangle(box, fill=fill, outline=edge + (255,), width=2)
        if tag is not None:
            tags.append((box, str(tag), edge, kind))
    # The numbers go on last, over every shaded box, so one highlight
    # overlapping another cannot bury the other's number. They sit just
    # outside the box, not in the middle of it: a badge in the centre lands
    # squarely on the words being checked, and "Owner Signature" reads
    # "Owner (1)nature".
    dot = max(8, int(7 * scale))
    font = proof_font(max(11, int(10 * scale)))
    for box, tag, edge, kind in tags:
        mid_y = (box[1] + box[3]) / 2
        # A gap closed is bounded by its caption on the left, so its badge
        # goes on the right - put it on the left and it lands on the very
        # words the reviewer is being asked to read.
        if kind == "join":
            at = box[2] + dot + 3
            if at + dot > img.width:
                at = box[0] - dot - 3
        else:
            at = box[0] - dot - 3
            if at - dot < 0:                   # no room on the left margin
                at = box[2] + dot + 3
        at = min(max(at, dot + 1), img.width - dot - 1)
        pen.ellipse([at - dot, mid_y - dot, at + dot, mid_y + dot],
                    fill=edge + (255,), outline=(255, 255, 255, 255), width=2)
        pen.text((at, mid_y), tag, fill=(255, 255, 255, 255), font=font,
                 anchor="mm")
    return Image.alpha_composite(img.convert("RGBA"), over).convert("RGB")


def save_proof(before, after, path=None, caption="", before_marks=(),
               after_marks=(), dpi=110, legend=()):
    """Build a before/after image for one page - the form on the left, what
    the tool made on the right - captioned with the file, the page and the
    size the document settled on, so a reviewer can tell at a glance which
    ones are worth a closer look.

    Saved to `path` when one is given, and returned either way, so a caller
    can hand the pages to `join_down` and get the whole form in one file
    instead of a folder to click through.

    `legend` is the numbered list of what changed, printed under the pages so
    the proof says what each highlight is without anyone opening the CSV."""
    from PIL import Image, ImageDraw
    a = Image.frombytes("RGB", (before.width, before.height), before.samples)
    b = Image.frombytes("RGB", (after.width, after.height), after.samples)
    a, b = mark_up(a, before_marks, dpi), mark_up(b, after_marks, dpi)
    gap, bar = 24, 30 if caption else 0
    line = 21
    foot = (len(legend) * line + 18) if legend else 0
    canvas = Image.new("RGB", (a.width + b.width + gap,
                               max(a.height, b.height) + bar + foot), (210, 210, 210))
    canvas.paste(a, (0, bar))
    canvas.paste(b, (a.width + gap, bar))
    draw = ImageDraw.Draw(canvas)
    if caption:
        draw.rectangle([0, 0, canvas.width, bar], fill=(32, 32, 32))
        draw.text((10, 6), caption, fill=(255, 255, 255), font=proof_font(17))
    if legend:
        top = canvas.height - foot
        draw.rectangle([0, top, canvas.width, canvas.height], fill=(38, 38, 38))
        small = proof_font(15)
        for i, note in enumerate(legend):
            draw.text((12, top + 9 + i * line), note,
                      fill=(240, 240, 240), font=small)
    if path:
        canvas.save(path)
    return canvas


def join_down(pages, path, gap=16):
    """One tall image holding every page of a form, in order.

    A form of five pages is five files to open, and comparing page 4 means
    finding page 4. Joined, the whole thing is one scroll: the form down the
    left, what it became down the right, page under page. Each page keeps the
    caption bar it was built with, so the join never loses which page is
    which.

    Pages can differ in width - a legal sheet among letter ones - so the
    canvas takes the widest and the rest sit flush left."""
    from PIL import Image
    if not pages:
        raise ValueError("join_down was given no pages")
    width = max(p.width for p in pages)
    canvas = Image.new("RGB", (width, sum(p.height for p in pages)
                               + gap * (len(pages) - 1)), (140, 140, 140))
    y = 0
    for p in pages:
        canvas.paste(p, (0, y))
        y += p.height + gap
    canvas.save(path)
    return canvas


# --------------------------------------------------------------------------
# Page processing
# --------------------------------------------------------------------------

def read_once(page, kind, make):
    """Remember an expensive read of a page.

    A page is asked for the same things over and over in a run - its text, its
    glyph runs, its drawings, a copy of itself without annotations - and each
    ask re-parses the whole content stream. Page objects don't survive from
    one `doc[n]` to the next, so the answers are kept on the document, by page
    number. Anything that changes a page drops what was remembered about it
    (see `forget`)."""
    doc = page.parent
    if kind != "printed" and not getattr(doc, "_no_annots", False):
        # Only a page with its annotations stripped reads the same every
        # time. On a page that still has them, the extractor fills in a
        # widget's appearance the first time something asks for it, so an
        # early read and a later one differ - and remembering the early one
        # had the signing tabs' SIGN redrawn as text nobody had accounted for.
        return make()
    store = getattr(doc, "_reads", None)
    if store is None:
        store = {}
        try:
            doc._reads = store
        except Exception:                 # a document that won't hold one
            return make()
    key = (page.number, kind)
    if key not in store:
        store[key] = make()
    return store[key]


def forget(page):
    """Drop what was remembered about a page, because it has just changed."""
    store = getattr(page.parent, "_reads", None)
    if store:
        for key in [k for k in store if k[0] == page.number]:
            del store[key]


TICKS = (fitz.PDF_WIDGET_TYPE_CHECKBOX, fitz.PDF_WIDGET_TYPE_RADIOBUTTON)


def tick_boxes(page):
    """Where the page's tick boxes and radio buttons are: (field name, rect)."""
    if getattr(page.parent, "_no_annots", False):
        return list(getattr(page.parent, "_tick_boxes", ()))
    return [(w.field_name, fitz.Rect(w.rect)) for w in page.widgets()
            if w.field_type in TICKS]


def printed(page):
    """The page's printed content only: a copy with its annotations dropped,
    at the same coordinates. Text extraction also reads what form fields
    display - a pre-filled value, a signature tab's SIGN - and restyling
    that onto the page would draw it a second time under the field's own
    copy. Everything that decides what gets restyled reads this instead."""
    def build():
        tmp = fitz.open()
        tmp.insert_pdf(page.parent, from_page=page.number, to_page=page.number)
        tmp.xref_set_key(tmp[0].xref, "Annots", "null")
        tmp._no_annots = True             # so its reads can be remembered
        # A tick box or a radio button is a field, but it is also a box a
        # member sees printed on the page, and words set under it are hidden.
        # The copy keeps where they are, so layout can stop in front of them.
        tmp._tick_boxes = tick_boxes(page)
        return tmp.reload_page(tmp[0])

    return read_once(page, "printed", build)


LIGATURE_PARTS = ("ti", "ft", "fi", "fl", "ff", "ffi", "ffl", "tt", "st")
_WORDS = None


def word_frequency():
    """The English and French word lists, as one frequency lookup. Used to
    work out what an unnameable glyph really draws. Returns None if
    pyspellchecker isn't installed, and the glyph is then left alone."""
    global _WORDS
    if _WORDS is None:
        try:
            from spellchecker import SpellChecker
            en, fr = SpellChecker(), SpellChecker(language="fr")
            _WORDS = lambda w: max(en.word_frequency[w], fr.word_frequency[w])
        except ImportError:
            _WORDS = False
    return _WORDS or None


def unicode_ligature(ch):
    """'fi' as one character -> the two letters it stands for. These are
    presentation forms whose decomposition Unicode itself defines, so this
    needs no guessing - and Arial has none of them."""
    if not 0xFB00 <= ord(ch) <= 0xFB06:
        return None
    parts = unicodedata.normalize("NFKD", ch)
    return parts if parts != ch else None


def ligature_candidates(word, i, freq):
    """Which letters could stand where word[i] is, for the word to be a word.
    Returns them best first, by how common the resulting word is.

    The glyph has to sit inside a word for any of this to mean anything. A
    glyph standing on its own is a symbol - a checkbox, a bullet - and must be
    left alone, which takes saying, because "ti" by itself is in the word list
    and every lone symbol would otherwise come out as those two letters."""
    if sum(ch.isalpha() for ch in word) < 2:
        return []
    hits = []
    for cand in LIGATURE_PARTS:
        whole = (word[:i] + cand + word[i + 1:]).strip("'’.,;:()[]").lower()
        n = freq(whole) if whole.isalpha() else 0
        if n:
            hits.append((n, cand))
    hits.sort(reverse=True)
    return hits


def resolve_ligatures(page, unnamed, freq):
    """What each unnameable glyph on the page actually draws, as
    {(font, glyph id): letters}.

    A PDF written by Word can map a ligature to the wrong character - the page
    shows "execution" while the text layer says "execuƟon" - so the text layer
    can't be trusted and the extractor reports the glyph as U+FFFD. What it
    draws is worked out from the word it sits in: the letters that turn that
    word into a real word. A glyph is only resolved when every occurrence on
    the page that resolves at all agrees, and an occurrence with more than one
    possible answer is settled by which word is commoner - "after" over
    "aster" - and only by a wide margin. Anything unresolved keeps its own
    glyph, which is right but leaves the odd gap in a word."""
    if not unnamed or freq is None:
        return {}
    votes, seen = {}, {}
    for block in read_once(page, "raw", lambda: page.get_text("rawdict"))["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            chars = [c for sp in line["spans"] for c in sp["chars"]]
            text = "".join(unicode_ligature(c["c"]) or c["c"] for c in chars)
            # index in `text` of each character, once ligatures are spelt out
            at, pos = [], 0
            for c in chars:
                at.append(pos)
                pos += len(unicode_ligature(c["c"]) or c["c"])
            for i, c in enumerate(chars):
                key = next((k for u, k in unnamed if glyph_in(c["bbox"], u)), None)
                if key is None:
                    continue
                start = text.rfind(" ", 0, at[i]) + 1
                end = text.find(" ", at[i])
                word = text[start:end if end > 0 else len(text)]
                hits = ligature_candidates(word, at[i] - start, freq)
                seen.setdefault(key, []).append(word)
                if hits and (len(hits) == 1 or hits[0][0] >= 10 * hits[1][0]):
                    votes.setdefault(key, set()).add(hits[0][1])
    return {k: next(iter(v)) for k, v in votes.items() if len(v) == 1}


def split_unnamed(raw_line, unnamed, stroked=(), slanted=()):
    """Rebuild a line's spans so glyphs that have to be treated differently sit
    in spans of their own: glyphs the PDF can't name, and glyphs drawn with an
    outline to thicken them.

    The extractor hands back one span per run of same font, same size - and a
    heading thickened by stroking is exactly that, so "Amendments to the Plan:
    The Depositary may from time to time..." arrives as a single span with
    only its first words stroked. Taking the span's first glyph for the whole
    span set entire sentences in bold."""
    out = []
    for sp in raw_line["spans"]:
        group, was = [], None
        for ch in sp["chars"] + [None]:
            this = None if ch is None else (
                any(glyph_in(ch["bbox"], u) for u in unnamed),
                (round(ch["origin"][0], 1), round(ch["origin"][1], 1)) in stroked,
                (round(ch["origin"][0], 1), round(ch["origin"][1], 1)) in slanted)
            # an unnameable glyph also gets a span to itself, so it can be
            # spelt out or kept on its own
            if group and (this != was or was[0]):
                box = fitz.Rect(group[0]["bbox"])
                for c in group[1:]:
                    box |= fitz.Rect(c["bbox"])
                out.append({"text": "".join(c["c"] for c in group), "origin": group[0]["origin"],
                            "bbox": tuple(box), "font": sp["font"], "size": sp["size"],
                            "flags": sp["flags"] | (16 if was[1] else 0)
                                     | (2 if was[2] else 0),
                            "color": sp.get("color", 0), "unnamed": was[0]})
                group = []
            if ch is not None:
                group.append(ch)
                was = this
    return out


def keep_spaces(spans):
    """Drop spans that hold nothing but whitespace, folding what they held
    into the words before them. A space can end up in a span of its own -
    in its own font, or on the far side of a stroked/unstroked boundary - and
    simply dropping it ran the words either side together once they were
    close enough to be flowed as continuous text. A space starting a line has
    nothing to join, and the next span's origin already allows for it."""
    out = []
    for sp in spans:
        if sp["text"].strip():
            out.append(sp)
        elif out:
            # the box takes in the space too: left as it was, the next span
            # seemed to start 3pt after this one ended, read as a tab stop
            # and kept its x - and on a squeezed line HBP's "or helping" came
            # back on top of the superscript "3" before it
            # - only an ordinary space, though: a wide run of them is a tab
            # stop, and the gap after it is what keeps the next label at its
            # own x (Transfer In's second "Sub #:" slid out of its column)
            a, b = out[-1]["bbox"], sp["bbox"]
            if b[2] - b[0] <= 0.5 * sp.get("size", 10.0):
                out[-1] = dict(out[-1], text=out[-1]["text"] + sp["text"],
                               bbox=(a[0], min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])))
            else:
                out[-1] = dict(out[-1], text=out[-1]["text"] + sp["text"])
    return out


def spell_out(sp, unnamed, spelt):
    """A span with its ligatures written out as plain letters, so the new font
    can set them like the rest of the word. `was` records the glyphs that were
    there before, because the census counts glyphs and this changes how many
    there are. A glyph nothing could identify is left exactly as it is."""
    if sp.get("unnamed"):
        key = next((k for u, k in unnamed if glyph_in(sp["bbox"], u)), None)
        letters = spelt.get(key)
        if not letters:
            return sp
        # the extractor saw U+FFFD here, so that's what the census counted
        return dict(sp, text=letters, unnamed=False, was="�" * len(sp["text"]))
    text = "".join(unicode_ligature(ch) or ch for ch in sp["text"])
    return sp if text == sp["text"] else dict(sp, text=text, was=sp["text"])


LIST_TAB = re.compile(r"\(?(?:\d{1,3}(?:\.\d{1,3})*|[A-Za-z]|[ivxIVX]{1,4})[.)]")


def split_tabs(spans, exact, gap):
    """Split a span where the form typed a run of spaces as a tab stop.

    "Incorrect amount paid.   Paid:            $" arrives as a single span.
    Arial's space is wider than the space it was set in, so re-drawn as one
    run the whole thing stretches and the "$" on the end drifts away from
    the blank it labels. Split at the wide run, the part after it counts as
    a column of its own and keeps the x the form gave it.

    The spaces themselves are dropped rather than left on the first piece:
    kept, they would put its box right up against the second piece, and the
    gap that says "this is a tab stop, leave it where it is" would vanish.
    The census ignores whitespace, so nothing is owed for them."""
    if not exact:
        return spans
    raw = exact.get("spans") or []
    if len(raw) != len(spans) or any(
            "".join(c["c"] for c in r["chars"]) != sp["text"]
            for r, sp in zip(raw, spans)):
        return spans                      # the two readings disagree
    out = []
    for r, sp in zip(raw, spans):
        chars = r["chars"]
        cuts, run, start = [], [], 0
        for i, c in enumerate(chars):
            if c["c"].isspace():
                run.append(c)
                continue
            # Word can write the tab after a list number as ONE space with a
            # wide advance - "2.<tab>Beneficiary Bank:" on the wire
            # instructions. Redrawn as a space it pulled the heading 6pt left,
            # off the underline drawn for it. Width alone does not tell such a
            # tab from a space (a pre-authorized credit form sets ordinary
            # spaces 6.2pt wide, and splitting there ran its words together),
            # so a lone space is a tab only straight after a list marker.
            if run:
                wide = fitz.Rect(run[-1]["bbox"]).x1 - fitz.Rect(run[0]["bbox"]).x0
                head = "".join(c["c"] for c in chars[:i - len(run)]).strip()
                marker_tab = (len(run) == 1 and LIST_TAB.fullmatch(head) is not None
                              and wide >= 0.45 * r["size"])
                # two spaces are the gap after a full stop, which a justified
                # line stretches to 8pt and more ("30 day period.  If the" on
                # the RLIF agreement); only a wider pair is a tab stop
                pair_ok = len(run) >= 3 or (len(run) == 2 and wide >= 1.2 * r["size"])
                if wide >= gap and (pair_ok or marker_tab):
                    cuts.append((start, i - len(run)))
                    start = i
            run = []
        if not cuts:
            out.append(sp)
            continue
        cuts.append((start, len(chars)))
        for a, b in cuts:
            part = [c for c in chars[a:b] if not c["c"].isspace()]
            if not part:
                continue
            box = fitz.Rect(part[0]["bbox"])
            for c in part[1:]:
                box |= fitz.Rect(c["bbox"])
            out.append(dict(sp, text="".join(c["c"] for c in chars[a:b]).strip(),
                            bbox=tuple(box), origin=part[0]["origin"], tab=a > 0))
    return out


def collect_lines(page, skip_conforming, target_font_substr, target_size, size_tol):
    """Gather every horizontal text line with its spans. Returns (lines, notes).

    A span that's filled and also stroked is faux bold - a regular font drawn
    with an outline to thicken it, as Word does when a font has no bold - and
    is marked bold here, so everything downstream draws it in the bold face.
    Where only part of a span is stroked, which is how a run-in heading comes
    out of the extractor, the line is re-read glyph by glyph so the bold stops
    where the heading does."""
    lines, notes = [], []
    data = read_once(page, "dict", lambda: page.get_text("dict"))
    raw = None
    # a space draws no ink, so an outlined one says nothing: a pre-authorized
    # credit form outlines the tab spaces after "$", and counted they made
    # "starting on" and "at a frequency of" bold
    stroked = {(round(c[2][0], 1), round(c[2][1], 1)) for run in read_once(page, "trace", page.get_texttrace)
               if run.get("type") == 1 for c in run.get("chars", []) if c[0] > 32}
    # where those glyphs are, by baseline, so a span can be asked whether only
    # part of it is stroked
    stroked_rows = {}
    for x, y in stroked:
        stroked_rows.setdefault(y, []).append(x)

    # Word's italic for a font that has none: the regular face set through a
    # sheared text matrix. Neither the font name nor the flags say so, so
    # "Income Tax Act" and "(if two or more signatures are required)" were
    # re-set upright. looks.py reads the shear; it is asked glyph by glyph,
    # because the sheared words share a span with the upright ones round them.
    slanted = set()
    slant_at, upright_at = _looks().slanted_starts(page)
    if slant_at:
        for run in read_once(page, "trace", page.get_texttrace):
            for c in run.get("chars", []):
                if _looks().is_slanted(c[2], slant_at, upright_at):
                    slanted.add((round(c[2][0], 1), round(c[2][1], 1)))
    slanted_rows = {}
    for x, y in slanted:
        slanted_rows.setdefault(y, []).append(x)

    def slanted_run(sp):
        xs = slanted_rows.get(round(sp["origin"][1], 1), ())
        return sum(1 for x in xs if sp["bbox"][0] - 0.5 <= x <= sp["bbox"][2] + 0.5)

    def stroked_run(sp):
        """How many of a span's glyphs are drawn with an outline."""
        xs = stroked_rows.get(round(sp["origin"][1], 1), ())
        return sum(1 for x in xs if sp["bbox"][0] - 0.5 <= x <= sp["bbox"][2] + 0.5)
    # Glyphs the PDF itself can't name. Some files map a ligature to the wrong
    # character - the page shows "after execution" while the text layer says
    # "Ōer execuƟon" - and redrawing what the text layer claims would print
    # that. The extractor reports these as U+FFFD, so the span is left alone.
    unnamed = [(fitz.Rect(c[3]), (run["font"], c[1])) for run in read_once(page, "trace", page.get_texttrace)
               if run.get("type") in (0, 1)
               for c in run.get("chars", []) if c[0] == 0xFFFD]
    spelt = resolve_ligatures(page, unnamed, word_frequency())
    boxes = [u for u, _ in unnamed]
    for (font, gid), letters in sorted(spelt.items()):
        notes.append(("ligature_spelt_out",
                      f"a glyph {font} can't name (#{gid}) draws {letters!r}, "
                      f"so it's set in the new font like the rest of its word"))

    seen_rules = []

    def gap_rules():
        if not seen_rules:
            seen_rules.append([r for r in rule_segments(page) if r.height < 2.5])
        return seen_rules[0]

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
                    spans.append(sp)              # kept by keep_spaces below
                    continue
                if skip_conforming and target_font_substr.lower() in sp["font"].lower() \
                        and abs(sp["size"] - target_size) <= size_tol:
                    continue
                thick = stroked_run(sp)
                if thick:
                    sp = dict(sp, flags=sp["flags"] | 16,
                              part_thick=thick < len(sp["text"].replace(" ", "")))
                leaning = slanted_run(sp)
                if leaning:
                    sp = dict(sp, flags=sp["flags"] | 2,
                              part_slant=leaning < len(sp["text"].replace(" ", "")))
                if any(glyph_in(u, fitz.Rect(sp["bbox"])) for u in boxes):
                    sp = dict(sp, unnamed=True)
                spans.append(sp)
            spans = keep_spaces(spans)
            if spans and any(sp.get("unnamed") or sp.get("part_thick") or sp.get("part_slant")
                             for sp in spans):
                # re-read this line glyph by glyph, so only the glyphs that
                # can't be named keep their own font
                if raw is None:
                    raw = {tuple(round(v, 1) for v in l["bbox"]): l
                           for b in read_once(page, "raw", lambda: page.get_text("rawdict"))["blocks"] if b.get("type") == 0
                           for l in b["lines"]}
                exact = raw.get(tuple(round(v, 1) for v in line["bbox"]))
                if exact:
                    spans = [spell_out(sp, unnamed, spelt)
                             for sp in keep_spaces(split_unnamed(exact, boxes, stroked,
                                                                 slanted))]
            spans = [spell_out(sp, unnamed, spelt) for sp in spans]
            if spans and any("  " in sp["text"]
                             or " " in sp["text"].strip()
                             for sp in spans):
                if raw is None:
                    raw = {tuple(round(v, 1) for v in l["bbox"]): l
                           for b in read_once(page, "raw", lambda: page.get_text("rawdict"))["blocks"]
                           if b.get("type") == 0 for l in b["lines"]}
                spans = split_tabs(
                    spans, raw.get(tuple(round(v, 1) for v in line["bbox"])), 6.0)
            # A tab stop parts a line into pieces that each keep their own x.
            # Kept as one line, a piece was given the room of the whole line,
            # and grown in Arial "Change to automatic transfer required" ran
            # into the "No" a tab stop after it. As lines of their own, each
            # stops before the next, as two columns on one row already do.
            pieces, cur = [], []
            for sp in spans:
                # (only before words: a typed blank stays with its label,
                # which may push into it - "Account #   ______" on its own
                # had no room)
                # (nor across a gap with a drawn line in it: "Safe Deposit
                # Box #______ was accessed" is a label, its blank and the
                # words after, and the label may push into the blank)
                if (cur and sp["bbox"][0] - cur[-1]["bbox"][2] >= 6.0 and sp.get("tab")
                        and sp["text"].lstrip()[:1].isalnum()
                        and not any(r.x1 > cur[-1]["bbox"][2] and r.x0 < sp["bbox"][0]
                                    and r.y0 < sp["bbox"][3] + 3 and r.y1 > sp["bbox"][1]
                                    for r in gap_rules())):
                    pieces.append(cur)
                    cur = []
                cur.append(sp)
            if cur:
                pieces.append(cur)
            for piece in pieces:
                box = fitz.Rect(piece[0]["bbox"])
                for sp in piece[1:]:
                    box |= fitz.Rect(sp["bbox"])
                if len(pieces) == 1:
                    box = fitz.Rect(line["bbox"])
                lines.append({"bbox": box, "spans": piece,
                              "block_bbox": fitz.Rect(block["bbox"])})

    # The printer's ligatures get spelt out too. Unicode defines what they
    # stand for, so there's nothing to work out - but it still changes how
    # many glyphs the page holds, so it's reported like any other change.
    shown = Counter(ch for ln in lines for sp in ln["spans"]
                    if sp.get("was") and "�" not in sp["was"]
                    for ch in sp["was"] if unicode_ligature(ch))
    for ch, n in sorted(shown.items()):
        notes.append(("ligature_spelt_out",
                      f"{ch!r} set as {unicode_ligature(ch)!r} in {n} place(s)"))
    return lines, notes


def census_band(y, page_height, bands=40):
    """Horizontal band a glyph falls in, keyed on its baseline y."""
    return min(bands - 1, max(0, int(y / max(page_height, 1) * bands)))


def glyph_in(bbox, rect):
    """Whether a glyph, given its box from get_texttrace(), belongs inside rect.
    Judged by the box's centre, not the glyph's origin: the origin sits on the
    baseline, and some fonts report a line box that ends exactly on the
    baseline - where a point test counts as outside, so whole lines were taken
    for hidden text and drawn twice.

    This is asked several million times in a run, so the test is done in
    plain arithmetic: building a Point and handing it to the rect's own
    `in` costs three layers of Python per glyph, which came to most of the
    time the whole tool spent."""
    x = (bbox[0] + bbox[2]) / 2
    y = (bbox[1] + bbox[3]) / 2
    return rect.x0 <= x < rect.x1 and rect.y0 <= y < rect.y1


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
    for run in read_once(page, "trace", page.get_texttrace):
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
    for d in read_once(page, "draw", page.get_drawings):
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


def uprights(page):
    """The upright rules a page draws - table walls - as thin rects. Word
    draws many of them as slivers of fill rather than stroked lines."""
    out = [r for r in rule_segments(page) if r.height > r.width and r.height >= 6]
    for d in read_once(page, "draw", page.get_drawings):
        fill = d.get("fill")
        if d["type"] in ("f", "fs") and fill and min(fill) < 0.99:
            for item in d["items"]:
                if item[0] == "re":
                    b = fitz.Rect(item[1])
                    if b.width <= 3 and b.height >= 6:      # (Collection Request draws 2.2pt sides)
                        out.append(b)
    return out


def cell_centre(bbox, walls):
    """The middle of the table cell a line was centred in, or None.

    Arial is wider or narrower than the face a form was set in, so a caption
    centred in its cell ("Accrued Interest", "DD/MM/YY" on RRSP Withdrawal)
    redrawn from its old left edge leans to one side of the cell. The test is
    looks.py's: walls either side reaching the line's full height, well in
    from the left one, and the two gaps agreeing."""
    left = [w.x1 for w in walls if w.x1 <= bbox.x0 + 1
            and w.y0 <= bbox.y0 + 2 and w.y1 >= bbox.y1 - 2]
    right = [w.x0 for w in walls if w.x0 >= bbox.x1 - 1
             and w.y0 <= bbox.y0 + 2 and w.y1 >= bbox.y1 - 2]
    if not (left and right):
        return None
    lo, hi = max(left), min(right)
    if bbox.x0 - lo < 12 or hi - bbox.x1 < 12:
        return None
    mid = (lo + hi) / 2
    if abs((bbox.x0 + bbox.x1) / 2 - mid) > max(4.0, 0.02 * (hi - lo)):
        return None
    return mid, lo, hi


def rule_crossings(page, band=20.0):
    """Which lines have characters whose body (cap height down to the
    baseline) crosses a ruled line or a box edge, keyed by baseline. Descender
    space is left out, so an underline just under the text doesn't count.
    Counting per line keeps a caption that already sat on its rule from
    reading as a regression when one more character touches it. Only printed
    rules count: a field's own border is the requirements tool's to place,
    and the layout ignores fields."""
    page = printed(page)
    rows = {}
    for seg in rule_segments(page):
        for k in range(int(seg.y0 // band), int(seg.y1 // band) + 1):
            rows.setdefault(k, []).append(seg)
    hits = Counter()
    for run in read_once(page, "trace", page.get_texttrace):
        if run.get("type") not in (0, 1):
            continue
        for c in run.get("chars", []):
            if c[0] <= 0 or chr(c[0]).isspace():
                continue
            base, size = c[2][1], run["size"]
            # the descender band is trimmed by a third of a point: a glyph
            # whose tail grazes an underline by a hundredth of a point isn't
            # sitting on it, and at these sizes that happens on rounding alone
            body = fitz.Rect(c[3][0], base - 0.72 * size, c[3][2], base + 0.08 * size - 0.3)
            near = rows.get(int(body.y0 // band), []) + rows.get(int(body.y1 // band), [])
            if any(body.intersects(s) for s in near):
                hits[round(base)] += 1
    return hits


def text_clashes(page):
    """Pairs of lines that share a row and whose boxes overlap, by baseline."""
    rows = []
    for block in read_once(printed(page), "dict",
                           lambda: printed(page).get_text("dict"))["blocks"]:
        for line in block.get("lines", []):
            text = "".join(s["text"] for s in line["spans"]).strip()
            # Real words only. A fill-in rule is rebuilt to its own length and
            # a checkbox is one glyph in a box of its own: both sit right up
            # against their neighbours by design, and reading that as text
            # running into text would cry wolf on every form that has them.
            if sum(c.isalpha() for c in text) >= 3 and "_" not in text:
                rows.append((fitz.Rect(line["bbox"]), line["spans"][0]["origin"][1]))
    hits = Counter()
    for i, (ra, base) in enumerate(rows):
        for rb, other in rows[i + 1:]:
            if abs(base - other) <= 2 and ra.x1 > rb.x0 + 0.5 and rb.x1 > ra.x0 + 0.5:
                hits[round(min(base, other))] += 1
    return hits


def inside(r, z, slack=0.01):
    """Is `r` within `z`, testing the edges rather than asking the rect?

    A hairline is a rect of no height, and a rect of no height is empty, and
    an empty rect is reported as inside nothing at all. Every zone here is a
    zone drawn around rules, so the question has to be asked in coordinates
    or the answer is always no."""
    return (z.x0 - slack <= r.x0 and r.x1 <= z.x1 + slack
            and z.y0 - slack <= r.y0 and r.y1 <= z.y1 + slack)


def line_art(page, ignore=()):
    """Every rule, box and filled shape the page prints, counted by what it
    looks like rather than where it is: its kind, its colour, and its size to
    a tenth of a point. Shapes inside `ignore` are left out.

    Counting shape rather than position is deliberate - a rule that moves is
    still that rule, and the point of this is to notice one that stops
    existing, or comes back a different size."""
    out = Counter()
    for d in read_once(printed(page), "draw", printed(page).get_drawings):
        r = fitz.Rect(d["rect"])
        if any(inside(r, fitz.Rect(z)) for z in ignore):
            continue
        colour = d.get("color") or d.get("fill") or (0, 0, 0)
        # to the half point: moving a page's content by a fraction of a point
        # rounds a shape's measured size differently at a finer grain than
        # that, and a tenth of a point either way is not a shape changing
        out[(d["type"], tuple(round(c, 2) for c in colour),
             round(r.width * 2) / 2, round(r.height * 2) / 2)] += 1
    return out


def art_shape(kind, colour, width, height):
    """The key `line_art` counts a shape under, so a shape this tool means to
    draw can be declared by what it will look like rather than by blanking
    out the strip of page it lands on - which would hide anything else that
    happens to be there."""
    return (kind, tuple(round(c, 2) for c in colour),
            round(width * 2) / 2, round(height * 2) / 2)


def verify_line_art(before, after, gone=(), added=(), expect=None):
    """Every rule and box the form prints must still be there afterwards.

    This is the one thing no other check watches. The census counts glyphs,
    the word check reads words, the edge and rule checks measure where text
    sits - all of them are about text. Erasing vector art and putting it back
    somewhere else is the one operation that can take a form apart without a
    single glyph going missing, so it gets a check of its own."""
    was, now = line_art(before, gone), line_art(after, added)
    if expect:
        was = was + Counter(expect)   # art this tool drew on purpose
    lost, gained = was - now, now - was

    def show(counts):
        return ", ".join(f"{w:g}x{h:g}pt {kind}" + (f" x{n}" if n > 1 else "")
                         for (kind, _, w, h), n in counts.most_common(4))

    issues = []
    if lost:
        issues.append(("LINE_ART_LOST",
                       f"{sum(lost.values())} ruled line(s) or shape(s) the form printed "
                       f"are no longer there: {show(lost)}"))
    if gained:
        issues.append(("LINE_ART_GAINED",
                       f"{sum(gained.values())} ruled line(s) or shape(s) appeared that "
                       f"the form didn't print: {show(gained)}"))
    return issues


def verify_overlap(before, after, dy=0.0):
    """No line may end up sitting on top of another that it cleared before.

    Text running into text is its own kind of wrong: the census still counts
    every glyph, the words are all still there, nothing has left the page -
    it just can't be read. One form had "account number." and "Correct
    account:" set a tab apart on the same line, and the first grew into the
    second."""
    was = {y + dy for y in text_clashes(before)}
    now = text_clashes(after)
    fresh = sorted(y for y in now if not any(abs(y - w) <= 2 for w in was))
    if not fresh:
        return []
    return [("TEXT_OVERLAPS_TEXT",
             f"{len(fresh)} line(s) now run into text beside them that they cleared "
             f"before, first near y={fresh[0]:.0f}")]


def verify_words(before, after):
    """Words that ran together, or came apart, between the two files.

    The census counts glyphs and ignores whitespace on purpose - a space
    doesn't survive a round trip reliably enough to count - so a lost space
    is invisible to it, though "the Lendernoted above" is as wrong as a lost
    letter. This reads the words themselves: any word in the restyled file
    that is two of the original's words stuck together, or two that are one
    of the original's split in half, is reported. Re-wrapping moves words
    between lines, which this doesn't mind, and joining a hyphenated word
    across a line break leaves a word that matches neither test."""
    def prose(w):
        """Worth comparing: real words, not a fill-in rule or a figure. A
        blank is rebuilt to its own length and a superscript ("4th") is set
        as a piece of its own, so both come apart by design."""
        return sum(c.isalpha() for c in w) >= 3 and "_" not in w

    was = [w[4] for w in printed(before).get_text("words")]
    now = [w[4] for w in printed(after).get_text("words")]
    glued = {was[i] + was[i + 1] for i in range(len(was) - 1)}
    torn = {now[i] + now[i + 1] for i in range(len(now) - 1)}
    spare = Counter(was)
    merged = []
    for w in now:
        if spare[w]:
            spare[w] -= 1
        elif w in glued and prose(w):
            merged.append(w)
    split = [w for w, n in spare.items() if n > 0 and w in torn and prose(w)]
    issues = []
    if merged:
        issues.append(("WORDS_RUN_TOGETHER",
                       f"{len(merged)} word(s) lost the space before them: "
                       + ", ".join(repr(w[:28]) for w in merged[:5])
                       + (" ..." if len(merged) > 5 else "")))
    if split:
        issues.append(("WORD_BROKEN_UP",
                       f"{len(split)} word(s) came apart: "
                       + ", ".join(repr(w[:28]) for w in split[:5])
                       + (" ..." if len(split) > 5 else "")))
    return issues


def verify_rules(before, after, dy=0.0):
    """No line may end up sitting on a ruled line or box edge it cleared
    before: text grown inside a fixed box can break out of it, which neither
    the census nor the right-edge check can see. Lines that already crossed
    one don't count again - that's the form's own layout."""
    was = {y + dy for y in rule_crossings(before)}
    now = rule_crossings(after)
    fresh = sorted(y for y in now if not any(abs(y - w) <= 2 for w in was))
    if not fresh:
        return []
    return [("TEXT_CROSSES_RULE", f"{len(fresh)} line(s) now sit on a ruled line or box edge "
                                  f"that cleared it before, first near y={fresh[0]}")]


def under_ticks(page):
    """How many letters sit under each tick box or radio button, by field."""
    boxes = tick_boxes(page)
    out = Counter()
    if not boxes:
        return out, {}
    said = {}
    for block in printed(page).get_text("rawdict")["blocks"]:
        for ln in block.get("lines", ()):
            for sp in ln["spans"]:
                for c in sp["chars"]:
                    if c["c"].isspace():
                        continue
                    g = fitz.Rect(c["bbox"])
                    for name, r in boxes:
                        # most of the letter under the box - a letter that only
                        # touches its edge is still read
                        if (g & r).get_area() > 0.5 * max(g.get_area(), 0.01):
                            out[name] += 1
                            said[name] = said.get(name, "") + c["c"]
    return out, said


def verify_tick_boxes(before, after):
    """TEXT_UNDER_TICK_BOX where the restyle set words under a tick box that
    the form had clear of it. Fields are not in the glyph census or the
    overlap check, which is how a box sitting on "attached" went unreported."""
    was, _ = under_ticks(before)
    now, said = under_ticks(after)
    return [("TEXT_UNDER_TICK_BOX", f"{n - was[name]} letter(s) now under the box "
                                    f"{name!r}: {said[name]!r}")
            for name, n in now.items() if n > was[name]]


FIELD_SCRIPTS = ("script", "script_calc", "script_format", "script_stroke",
                 "script_change", "script_blur", "script_focus")


def field_props(w):
    """What a field does beyond its name and type - its scripts (the T4
    worksheets add up their own boxes; dates and dollar amounts format
    themselves), a dropdown's choices, its tooltip, a length limit, and a
    tick box's export value and whether the form ships it ticked."""
    out = {a: getattr(w, a, None) for a in FIELD_SCRIPTS if getattr(w, a, None)}
    if w.field_label:
        out["field_label"] = w.field_label
    if w.text_maxlen:
        out["text_maxlen"] = w.text_maxlen
    if w.text_format:
        out["text_format"] = w.text_format
    if w.choice_values:
        out["choice_values"] = list(w.choice_values)
    if w.field_type == fitz.PDF_WIDGET_TYPE_BUTTON:
        # a push button's face: Federal RLIF's "Print" and "Reset" came back
        # as two blank buttons, ruled under like a blank to write on
        if w.button_caption:
            out["button_caption"] = w.button_caption
            # (its caption is drawn only at a size: left at 0 it came back blank)
            out["text_fontsize"] = w.text_fontsize or 8.0
        for a in ("fill_color", "border_color", "text_color"):
            if getattr(w, a, None):
                out[a] = list(getattr(w, a))
    if w.field_type in TICKS:
        on = w.on_state()
        if on not in (True, None, "Yes"):
            out["on_state"] = str(on)
        if w.field_value not in (None, "", "Off", False):
            out["checked"] = True
    return out


def field_behaviour(doc):
    """{field name: {what: set of values}} for everything field_props reads,
    over every widget of the document."""
    out = {}
    for p in doc:
        for w in p.widgets():
            props = field_props(w)
            if w.field_type in TICKS and "on_state" not in props:
                props["on_state"] = "Yes"
            mine = out.setdefault(w.field_name or "", {})
            for what, v in props.items():
                if what in ("text_maxlen", "text_format", "checked"):
                    continue          # set with the rest; scripts are what break
                # "$" and "$" are the same character to JavaScript, and
                # a script can come back written either way
                v = (tuple(map(str, v)) if isinstance(v, list)
                     else " ".join(re.sub(r"\\u([0-9a-fA-F]{4})",
                                          lambda m: chr(int(m.group(1), 16)),
                                          str(v)).split()))
                mine.setdefault(what, set()).add(v)
    return out


def verify_field_behaviour(before, after):
    """FIELD_BEHAVIOUR_LOST where a field kept its name and type but lost what
    it did: a calculation, a format, a dropdown's list, a tick box's export
    value, a tooltip. The field check counts names and types only, so a
    rebuilt field that forgot its script passed it."""
    was, now = field_behaviour(before), field_behaviour(after)
    lost, example = Counter(), {}
    for name, props in was.items():
        for what, vals in props.items():
            gone = vals - now.get(name, {}).get(what, set())
            if gone and name in now:
                lost[what] += len(gone)
                example.setdefault(what, name)
    names = {"script": "action", "script_calc": "calculation", "script_format": "format",
             "script_stroke": "keystroke check", "script_change": "validation",
             "script_blur": "on-exit script", "script_focus": "on-enter script",
             "choice_values": "dropdown list", "on_state": "tick-box export value",
             "field_label": "tooltip", "button_caption": "button caption"}
    return [("FIELD_BEHAVIOUR_LOST", f"{n} {names.get(what, what)}(s) gone, first on "
                                     f"{example[what]!r}")
            for what, n in lost.most_common()]


def signing_extent(rec, width):
    """All the paper a standard signing area covers: from the top of the area
    it replaced to the foot of the block that replaced it, which grows a row
    per signer and can run well past where the old one ended."""
    box = fitz.Rect(rec["box"])
    return fitz.Rect(0, box.y0, width, max(box.y1, rec.get("bottom", box.y1)))


def _looks():
    """looks.py, which sits beside this script but is not always on the path
    - the sabotage tests load this file from wherever it is."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import looks
    return looks


def verify_looks(before, after, header=None, signed=(), dys=()):
    """Does the restyled file still look like the form - its bold, italic and
    underlines, its colours and shading, its tick boxes beside their words,
    its edges and its centring? See looks.py. Everything else here asks
    whether a thing is still there; that is how a sentence come out bold, a
    grey row gone white and italics set upright all passed.

    The parts replaced on purpose are left out on both sides: the old title
    and logo and the new header band, and each signing area brought to the
    standard - its labels lose their bold by the client's own rule."""
    looks = _looks()
    # this module's own reads, handed over rather than imported back: run as
    # a script or loaded by path, this file is not importable by its name
    looks.R = SimpleNamespace(printed=printed, rule_segments=rule_segments)
    before_skip, after_skip = {}, {}
    if header:
        before_skip[0] = ([fitz.Rect(t["bbox"]) + (-2, -2, 2, 2) for t in header["old_titles"]]
                          + [fitz.Rect(r) + (-2, -2, 2, 2) for r in header["old_logos"]])
        after_skip[0] = [fitz.Rect(0, 0, after[0].rect.width, header["bottom"] + 4)]
    for pno, done in enumerate(signed):
        slack = abs(dys[pno]) + 6 if pno < len(dys) else 6
        for rec in done:
            box = signing_extent(rec, after[0].rect.width) + (0, -slack, 0, slack)
            before_skip.setdefault(pno, []).append(box)
            after_skip.setdefault(pno, []).append(box)
    return looks.compare(before, after, before_skip, after_skip)


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
    for tr in read_once(page, "trace", page.get_texttrace):
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
                 recover=True, reflow=None, rules=None, fine=None, relabel=None, drop=None,
                 squeeze=None, push=None):
    """Restyle one page. Returns (issues, expected): a list of (issue, detail)
    tuples, and the glyph-count changes made on purpose, keyed by (baseline y,
    character), for the census. From plan_reflow: `reflow` maps line_key ->
    layout for lines whose paragraph is re-wrapped rather than redrawn in
    place, and `rules` gives fill-in rules a new start and end. `fine` maps
    line_key -> the smaller size for footers and fine print, and `relabel`
    line_key -> the house wording from plan_relabels. `drop` holds lines to
    erase with the rest but not redraw - the old title --header replaces."""
    reflow, rules, fine, relabel = reflow or {}, rules or [], fine or {}, relabel or {}
    drop = drop or []
    squeeze = squeeze or {}
    moved_glyphs = []          # (baseline it left, baseline it went to, text)

    def lower(y):
        """Where a baseline ends up. Everything below the line a push was made
        at goes down with the furniture that was moved out from under it."""
        return y + drop_at(push or (), y)

    def put(x, y, text, **kw):
        at = lower(y)
        page.insert_text(fitz.Point(x, at), text, **kw)
        if at != y:
            moved_glyphs.append((y, at, text))
        return at
    issues = []
    content = printed(page)
    lines, notes = collect_lines(content, skip_conforming, target_font_substr, size, size_tol)
    issues.extend(notes)
    if not lines:
        return issues, Counter()

    # Glyphs Arial can't draw - a character it hasn't got, or one the PDF
    # can't even name - stay exactly as they were, in their own font.
    def kept(sp):
        return sp.get("unnamed", False) or not drawable(sp["text"], style_of(sp["flags"], fonts)[2])

    page_centre = page.rect.width / 2
    text_left = min(l["bbox"].x0 for l in lines)
    text_right = max(l["bbox"].x1 for l in lines)
    starts = [l["bbox"].x0 for l in lines]
    flush = right_aligned_flags(lines, text_left, text_right, starts)
    ink_end = dict(zip((id(l) for l in lines), ink_ends(content, lines)))
    walls = uprights(content)
    flats = [r for r in rule_segments(content) if r.height < 2.5 and r.width >= 4]

    def before_rule(ln):
        base_, end_ = ln["spans"][0]["origin"][1], ink_end[id(ln)]
        return any(end_ - 2 <= r.x0 <= end_ + 8 and base_ - 3 <= (r.y0 + r.y1) / 2 <= base_ + 5
                   for r in flats)
    # where labels that end at a line start: a column of them sharing a left
    # edge (Estate Account's "Telephone:" x4) is set flush left, and keeps it
    labelled_starts = [ln["bbox"].x0 for ln in lines if before_rule(ln)]

    # --- plan every line before touching the page ------------------------
    plan = []
    inline = {r["inline"]: r for r in rules if "inline" in r}
    dropped = Counter()
    for ln in lines:
        if in_drop(line_key(ln), drop):
            for sp in ln["spans"]:        # erased with the rest, never redrawn
                if kept(sp):              # except glyphs that stay in their own font
                    continue
                for ch in sp["text"]:
                    if not ch.isspace():
                        dropped[(sp["origin"][1], ord(ch))] -= 1
            continue
        if line_key(ln) in reflow:
            continue            # erased with the rest, redrawn with its paragraph
        # A line ending in typed underscores is drawn as its label, then the
        # rule rebuilt apart where plan_reflow put it.
        spans, rule = ln["spans"], None
        if line_key(ln) in relabel:
            # the house wording, set in the label's own style
            spans = [dict(ln["spans"][0], text=relabel[line_key(ln)]["text"])]
            if relabel[line_key(ln)].get("pushed") and line_key(ln) in inline:
                # Wording too long for where its blank started pushed the
                # blank along (plan_relabels): the blank goes where that left
                # it. Typed on after the wording it moved whole, into
                # "Telephone:" beside it. Wording that fits keeps its
                # underscores typed on straight after it, as it always has.
                label, style, _ = split_trailing_rule(spans)
                if style is not None:
                    spans, rule = label, (inline[line_key(ln)], style)
        elif line_key(ln) in inline:
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
        looks_centred = (centred_shape(ln["bbox"], page_centre, text_left, text_right,
                                       centre_tol, min_indent)
                         and alone_on_row(ln, lines)
                         and not in_left_block(ln, lines))
        flush_right = not looks_centred and rule is None and flush[lines.index(ln)]
        in_cell = None
        if not looks_centred and rule is None and line_key(ln) not in relabel:
            got = cell_centre(ln["bbox"], walls)
            if got is not None:
                # alone in its own cell - the next cell along the row shares
                # its baseline, and that is fine
                mid, lo, hi = got
                base = ln["spans"][0]["origin"][1]
                crowded = any(o is not ln and abs(o["spans"][0]["origin"][1] - base) <= 2
                              and o["bbox"].x1 > lo and o["bbox"].x0 < hi for o in lines)
                if not crowded and not in_left_block(ln, lines):
                    in_cell = mid
            if in_cell is not None:
                flush_right = False
        # A label set to end where its fill-in line begins keeps that end and
        # grows to the left: grown to the right, FHSA Withdrawal's "TOTAL
        # PROCEEDS" ran out onto the line it labels.
        ends_at_rule = None
        if (not looks_centred and in_cell is None and rule is None
                and line_key(ln) not in relabel and new_w > old_w + 0.5):
            if (before_rule(ln) and sum(1 for x in labelled_starts
                                        if abs(x - ln["bbox"].x0) <= 2) < 2
                    and not in_left_block(ln, lines)):
                ends_at_rule = ink_end[id(ln)]

        if new_w > old_w * keep_size_ratio:
            issues.append(("line_got_wider",
                           f"{text_preview!r} {old_w:.0f}pt -> {new_w:.0f}pt"))

        plan.append({"line": ln, "spans": spans, "rule": rule, "size": line_size, "new_w": new_w,
                     "relabelled": line_key(ln) in relabel,
                     "keep_centre": (relabel[line_key(ln)]["centre"] if line_key(ln) in relabel
                                     else in_cell),
                     "keep_right": (relabel[line_key(ln)].get("right_at")
                                    if line_key(ln) in relabel else ends_at_rule),
                     "old_w": old_w, "centred": looks_centred, "right": flush_right})

    if dry_run:
        return issues, Counter()

    orphans = recover_orphans(content, [ln["bbox"] for ln in lines]) if recover else []
    if orphans:
        issues.append(("recovered_hidden_text",
                       f"{len(orphans)} run(s) invisible to the text extractor, redrawn"))

    orphans = [o for o in orphans if drawable(o["text"], style_of(o["flags"], fonts)[2])]
    # Text the form draws inside another line's box that is not that line's
    # words - Ln Renewal's "(months)" printed under "Total Monthly Payment",
    # clipped out of sight - is erased with the line it hides under and was
    # never seen, so it is not drawn again. The census is told, and the
    # report says what it was, so a person can check it was meant to be hidden.
    hidden_under = []
    line_text = [(fitz.Rect(ln["bbox"]), " ".join("".join(sp["text"] for sp in ln["spans"]).split()))
                 for ln in lines]
    for tr in read_once(content, "trace", content.get_texttrace):
        if tr.get("type") != 0:
            continue
        chars = [c for c in tr.get("chars", []) if c[0] > 32]
        text = "".join(chr(c[0]) for c in chars)
        if len(text) < 3 or not text.isascii():
            continue
        homes = [t for r, t in line_text if all(glyph_in(c[3], r) for c in chars)]
        if homes and not any(text in t.replace(" ", "") for t in homes):
            hidden_under.append((chars, text, homes[0]))
    for chars, text, home in hidden_under:
        issues.append(("hidden_text_left_out",
                       f"{text!r}, drawn under {home[:40]!r} where it could not be seen, "
                       f"left out"))
    left = [sp for ln in lines for sp in ln["spans"] if kept(sp)]
    if left:
        shown = ", ".join(sorted({repr(ch) for sp in left for ch in sp["text"] if not ch.isspace()}))
        issues.append(("kept_original_glyphs", f"{len(left)} span(s) Arial can't draw left in "
                                               f"their own font: {shown[:80]}"))

    # --- erase all original text in one pass ------------------------------
    keeps = [fitz.Rect(sp["bbox"]) for ln in lines for sp in ln["spans"] if kept(sp)]
    rects = [piece for o in orphans for piece in clip_out(o["rect"], keeps)]
    for ln in lines:
        if any(kept(sp) for sp in ln["spans"]):
            for sp in ln["spans"]:
                if not kept(sp):
                    rects += clip_out(fitz.Rect(sp["bbox"]), keeps)
        else:
            b = fitz.Rect(ln["bbox"])
            rects += clip_out(fitz.Rect(b.x0 - pad, b.y0 - pad, b.x1 + pad, b.y1 + pad), keeps)
    forget(page)                       # the page is about to change
    for r in rects:
        page.add_redact_annot(r, fill=None, cross_out=False)

    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                          graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                          text=fitz.PDF_REDACT_TEXT_REMOVE)

    # --- redraw ------------------------------------------------------------
    expected = Counter()        # glyph-count changes made on purpose
    expected.update(dropped)
    for chars, _text, _home in hidden_under:
        for c in chars:
            expected[(c[2][1], c[0])] -= 1
    # A ligature spelt out is one glyph before and two letters after, wherever
    # the line ends up: every path draws the span's text and accounts for it
    # at its own baseline, so declaring the swap once here balances all of them.
    for ln in lines:
        for sp in ln["spans"]:
            if sp.get("was") is not None:
                for ch in sp["was"]:
                    if not ch.isspace():
                        expected[(sp["origin"][1], ord(ch))] -= 1
                for ch in sp["text"]:
                    if not ch.isspace():
                        expected[(sp["origin"][1], ord(ch))] += 1
    for item in plan:
        ln = item["line"]
        if item["keep_centre"] is not None:
            # a relabelled caption stays centred under its signature line, and
            # a caption centred in its table cell stays centred in it - on its
            # ink, not counting the space the form left after the last word
            ink_w = line_width(item["spans"], fonts, item["size"])
            dx = (item["keep_centre"] - ink_w / 2) - ln["bbox"].x0
        elif item.get("keep_right") is not None:
            # and one that finished with its rule still finishes there
            dx = (item["keep_right"] - line_width(item["spans"], fonts, item["size"])) - ln["bbox"].x0
        elif item["centred"]:
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
        # A line set narrower is squeezed horizontally about the point it
        # starts from, so it still begins exactly where it did. Every span of
        # the line takes the same squeeze, which also pulls their positions in
        # - that's what makes the whole line narrower rather than just its
        # glyphs thinner.
        narrow = squeeze.get(line_key(ln))
        morph = ((fitz.Point(ln["bbox"].x0 + dx, ln["spans"][0]["origin"][1]),
                  fitz.Matrix(narrow, 1)) if narrow else None)
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
            # and a column that the span before it has now grown into starts
            # after it, a space clear: kept at its own x, Ln Renewal's "Sub"
            # was drawn 10pt inside "«Customer_Number»", and taking the tag
            # off the page took "Su" with it
            elif prev_end_new is not None and x + dx < prev_end_new + at * 0.25:
                x = prev_end_new + at * 0.25 - dx

            if kept(sp):                    # left where it was, in its own font
                prev_end_old = prev_end_new = sp["bbox"][2]
                continue
            put(x + dx, y, sp["text"], fontname=alias, fontfile=path, morph=morph,
                fontsize=at, color=span_colour(sp), overlay=True)

            prev_end_old = sp["bbox"][2]
            prev_end_new = x + dx + font.text_length(sp["text"], at)

        if item["rule"]:
            cut, style = item["rule"]
            alias, path, font = style_of(style["flags"], fonts)
            n = max(1, int(round((cut["x1"] - cut["x0"])
                                 / (font.text_length("_", item["size"]) or 1))))
            put(cut["x0"], cut["y"], "_" * n, fontname=alias,
                fontfile=path, fontsize=item["size"], color=span_colour(style),
                overlay=True)
            # a relabelled line has already had all its old glyphs taken off
            # below, underscores and all
            expected[(cut["y"], ord("_"))] += n - (0 if item["relabelled"] else cut["count"])

        if item["relabelled"]:
            # The census is told exactly which glyphs the new wording swaps,
            # so anything else that changed on this line still shows up.
            for sp in ln["spans"]:
                for ch in sp["text"]:
                    if not ch.isspace():
                        expected[(sp["origin"][1], ord(ch))] -= 1
            for sp in item["spans"]:
                for ch in sp["text"]:
                    if not ch.isspace():
                        expected[(sp["origin"][1], ord(ch))] += 1

    # Re-wrapped paragraphs. Their glyphs are all still there, just on other
    # baselines, so the census is told where each one left and arrived.
    for layout in {id(v): v for v in reflow.values()}.values():
        narrow = layout.get("squeeze")
        # a one-line "paragraph" that also had to come down a size to fit its
        # room takes that size: set at the body size, FHSA Withdrawal's
        # "Yes" ran into the tick box after it
        own = None
        if layout.get("rows") == 1 and layout.get("spans"):
            ox, oy = layout["spans"][0]["origin"]
            own = fine.get((round(ox, 1), round(oy, 1)))
        first_x = min((p_[0] for p_ in layout["pieces"]), default=0.0)
        for x, y, text, sp, at in layout["pieces"]:
            if own is not None and own < at:
                # the words close up with the type, about where the line starts
                x = first_x + (x - first_x) * own / at
                at = own
            alias, path, font = style_of(sp["flags"], fonts)
            morph = ((fitz.Point(layout["pivot"], y), fitz.Matrix(narrow, 1))
                     if narrow else None)
            put(x, y, text, fontname=alias, fontfile=path,
                fontsize=at, color=span_colour(sp), morph=morph, overlay=True)
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
        put(origin.x, origin.y, text,
                         fontname=alias, fontfile=path,
                         fontsize=size, color=tuple(o["color"]), overlay=True)

    # Everything that went down with the furniture moved from one baseline to
    # another, and the census counts by baseline, so it is told each one.
    for was, now, text in moved_glyphs:
        for ch in text:
            if not ch.isspace():
                expected[(was, ord(ch))] -= 1
                expected[(now, ord(ch))] += 1
    return issues, expected


# --------------------------------------------------------------------------
# Footers and fine print: a second, smaller size
# --------------------------------------------------------------------------

# ...or a form code with its revision date in brackets: "EST 107 (06/15)",
# "500-091 Libro Credit Union (01/23)"
FOOTER_TEXT = re.compile(r"\bpage\s*\d+\s*(of\s*\d+)?\b|\brev(ised|ision)?\.?\s*\d|©"
                         r"|\(\d{1,2}/\d{2,4}\)", re.I)


def flat_rules(page):
    """Every horizontal rule the page draws, as (y, x0, x1, width, colour).

    Both spellings count: a stroked line, and a rectangle drawn so thin it
    reads as one. A rule is what a caption's blank is made of, so finding
    them is the first half of closing the space in front of one."""
    out = []
    for d in read_once(page, "draw", page.get_drawings):
        colour = d.get("color") or d.get("fill") or (0, 0, 0)
        width = d.get("width") or 0.6
        # the outline of a filled area is no rule: Ln Conversion's grey panels
        # are drawn as filled shapes with straight sides, and their tops and
        # bottoms were read as lines to sign on
        filled_area = d["type"] == "f" and fitz.Rect(d["rect"]).height > 1.5
        for item in d["items"]:
            if item[0] == "l" and filled_area:
                continue
            if item[0] == "l":
                p, q = item[1], item[2]
                if abs(p.y - q.y) <= 0.6 and abs(p.x - q.x) >= 12:
                    out.append(((p.y + q.y) / 2, min(p.x, q.x), max(p.x, q.x),
                                max(width, 0.4), tuple(colour)))
            elif item[0] == "re":
                r = fitz.Rect(item[1])
                if 0 < r.height <= 1.5 and r.width >= 12:
                    out.append(((r.y0 + r.y1) / 2, r.x0, r.x1,
                                max(r.height, 0.4), tuple(colour)))
    return sorted(set(out))


def upright_edges(page, visible_only=False):
    """The upright lines a page draws: cell sides, box edges, dividers.

    Wanted for what they say about a horizontal rule that ends at one. A
    fill-in blank has a free left end - it starts where the caption stops. A
    rule that begins exactly at an upright is the top or bottom of a ruled
    cell, and the text to its left is in the cell before it, not a caption
    for it."""
    out = []
    for d in read_once(page, "draw", page.get_drawings):
        # A white box on a white page draws no edge at all. Hatching Egg lays
        # forty of them over its page to mask things off, and read as cell
        # sides they put every blank "inside a cell" and the rebuild drew
        # those blanks with no line to write on.
        # The restyle's own uses (the signing pass, gap closing) were tuned
        # with them in - Aviso's signing area read differently without - so
        # only the rebuild asks for this.
        ink = d.get("color") if d["type"] in ("s", "fs") else None
        if (visible_only and (ink is None or min(ink) > 0.95)
                and (d["type"] == "s" or not d.get("fill") or min(d["fill"]) > 0.95)):
            continue
        for item in d["items"]:
            if item[0] == "l":
                p, q = item[1], item[2]
                if abs(p.x - q.x) <= 0.6 and abs(p.y - q.y) >= 4:
                    out.append(((p.x + q.x) / 2, min(p.y, q.y), max(p.y, q.y)))
            elif item[0] == "re":
                r = fitz.Rect(item[1])
                if r.height < 4:
                    continue
                if r.width <= 1.5:            # a side drawn as a thin box
                    out.append(((r.x0 + r.x1) / 2, r.y0, r.y1))
                else:                          # a box gives both of its sides
                    out.append((r.x0, r.y0, r.y1))
                    out.append((r.x1, r.y0, r.y1))
    return out


def label_gaps(page, min_gap=6.0, max_gap=60.0, col_tol=1.5, col_span=72.0):
    """Captions that stand away from the rule they label.

    A form sets "DFO Licence No." and then starts its rule half an inch
    further along, and once the type is re-set at a size of its own the space
    between the two is wider still. Under `min_gap` the two already read as
    joined - a word space is two or three points - and closing it would move
    the page for nothing, so it is not a gap at all. Each gap found is returned as the empty
    strip between the caption and the rule, with what the caption says and
    which way it could be closed.

    Each gap is marked `in_column` when its rule's left end lines up with
    another rule just above or below it. That decides which way it closes.
    A rule standing on its own is run back to its caption. A rule that is one
    of a column keeps its edge - running one back alone would leave the
    column ragged - and the caption is moved across to it instead. That is
    the split the client asked for: some lines extended, some labels
    repositioned."""
    words = printed(page).get_text("words")
    rs = flat_rules(printed(page))
    verts = upright_edges(printed(page))
    close = []
    starts_of = []              # (x where a rule's label starts, the rule's y)
    for y, x0, x1, width, colour in rs:
        # A rule that starts at a cell side is that cell's edge, not a blank.
        if any(abs(vx - x0) <= 2.0 and vy0 - 2 <= y <= vy1 + 2
               for vx, vy0, vy1 in verts):
            continue
        line = sorted((w for w in words if w[3] - 2.5 <= y <= w[3] + 4.5),
                      key=lambda w: w[0])
        # words standing on the rule make it their underline, not a blank:
        # Wire Instructions' "1.  Beneficiary Bank:" had the heading's
        # underline run back to meet the "1."
        if any(w[0] < x1 - 1 and w[2] > x0 + 1 for w in line):
            continue
        ahead = [w for w in line if w[2] <= x0 + 0.5]
        if not ahead:
            continue
        lead = ahead[-1]
        # where this rule's label starts, kept for every labelled rule on the
        # page - near or far from its line - so a label can be seen to be one
        # of a column of labels set flush left on the same edge
        group = [lead]
        for w in reversed(ahead[:-1]):
            if group[0][0] - w[2] >= 6.0:
                break
            group.insert(0, w)
        starts_of.append((group[0][0], y))
        gap = x0 - lead[2]
        if not (min_gap <= gap <= max_gap):
            continue
        if any(lead[2] < w[0] < x0 - 0.5 for w in line):
            continue                       # something already stands in the gap
        # the caption is the run since the last rule on this line
        stop = max([r[2] for r in rs if abs(r[0] - y) <= 0.6 and r[2] <= lead[0] + 0.5]
                   or [-1])
        run = [w for w in line if w[0] >= stop - 0.5 and w[2] <= x0 + 0.5]
        # A caption is contiguous words. A row can put several things between
        # one rule and the next - three tax bands and then the label for the
        # cell beside them - and the caption for THIS rule is only the last
        # group. The break is a gap of 6pt or more, the same width this tool
        # reads elsewhere as a typed tab stop rather than a word space.
        for i in range(len(run) - 1, 0, -1):
            if run[i][0] - run[i - 1][2] >= 6.0:
                run = run[i:]
                break
        said = " ".join(w[4] for w in run).strip()
        # a caption says something. A tick box drawn as a glyph, or a stray
        # mark the form uses as furniture, is alphanumeric to Python without
        # being a word, so this asks for letters a reader would recognise.
        if not any(("a" <= c <= "z") or ("A" <= c <= "Z") or c.isdigit()
                   for c in said):
            continue
        # and a caption is not the end of a sentence with its own blanks in
        # it: "... day of ______, A.D. 20___." on Est125 had the signature
        # line beside it run back 60pt to meet it
        if "___" in said and said.rstrip().endswith("."):
            continue
        box = fitz.Rect(min(w[0] for w in run), min(w[1] for w in run),
                        lead[2], max(w[3] for w in run))
        # Where the caption's own room begins: after the rule before it on
        # this line, or at the page edge if it is the first thing on the row.
        # Its spans are matched against that room rather than against the ink,
        # because a caption's span carries the spaces after its last letter
        # and so measures wider than the words do.
        starts = max(stop, 0.0)
        found = {"rect": fitz.Rect(lead[2], y - 1.5, x0, y + 1.5),
                 "y": y, "from": lead[2], "to": x0, "width": width,
                 "colour": colour, "gap": gap, "said": said[-60:],
                 "run": box,
                 "room": fitz.Rect(starts, box.y0, x0, box.y1)}
        found["in_column"] = any(
            abs(o[1] - x0) <= col_tol and abs(o[0] - y) > 0.6
            and abs(o[0] - y) <= col_span for o in rs)
        close.append(found)
    # A caption can pair with more than one rule - a form draws its blank as a
    # line and again as a thin box, or rules a cell either side of it - and
    # closing the same gap twice sets the caption down twice. Only the nearest
    # rule to each caption counts.
    best = {}
    for g in close:
        key = (round(g["run"].y1, 1), round(g["run"].x0, 1))
        if key not in best or g["gap"] < best[key]["gap"]:
            best[key] = g
    # A label that starts on the same edge as the labels of the rules just
    # above or below it is one of a column the form set flush left. Moved
    # across to its rule, it leaves the column: the ABM trace's "PAN number
    # used for the transaction (if available):" was slid 41pt right, out of
    # line with "ATM unit identification number:" and "Tracer number" over
    # and under it. Where the labels AND the rules are both columns, the gap
    # between them is the form's own layout.
    for g in best.values():
        g["in_label_column"] = any(abs(x - g["run"].x0) <= 1.0 and abs(y - g["y"]) > 0.6
                                   and abs(y - g["y"]) <= 100 for x, y in starts_of)
    return sorted(best.values(), key=lambda g: (g["y"], g["from"]))


def rule_labels(page):
    """Every rule that has a label set before it on its line, however far
    away: where the rule starts and sits, and the label's left and right
    edges. Read on the form as it came, it says how the form lined its
    labels up - on their left edges, or on their right edges against the
    lines they label."""
    shown = printed(page)
    words = shown.get_text("words")
    out = []
    for y, x0, x1, width, colour in flat_rules(shown):
        line = sorted((w for w in words if w[3] - 2.5 <= y <= w[3] + 4.5),
                      key=lambda w: w[0])
        ahead = [w for w in line if w[2] <= x0 + 0.5]
        if not ahead:
            continue
        group = [ahead[-1]]
        for w in reversed(ahead[:-1]):
            if group[0][0] - w[2] >= 6.0:
                break
            group.insert(0, w)
        out.append({"rule_x0": x0, "y": y, "x0": group[0][0], "x1": group[-1][2]})
    return out


def ends_against_rules(g, labels):
    """Did the form set this label to end against its rule?

    Only with evidence: on the form as it came, another label on the same
    column of rules ends where this one does, and none starts where this one
    does. Milk's "Name of Lender", "Address", "Telephone No." and "E-mail"
    were set that way, and moving them across restores it. A label that
    shares its left edge with its neighbours - ABM's "PAN number used for the
    transaction", Collateral's "AND TO:" under "TO:" - was set flush left,
    and moving it takes it out of line."""
    mine = [o for o in labels if abs(o["y"] - g["y"]) <= 1.5 and abs(o["rule_x0"] - g["to"]) <= 2]
    if not mine:
        return False
    me = mine[0]
    near = [o for o in labels if o is not me and 0.6 < abs(o["y"] - me["y"]) <= 100]
    if any(abs(o["x0"] - me["x0"]) <= 1.0 for o in near):
        return False
    column = [o for o in near if abs(o["rule_x0"] - me["rule_x0"]) <= 2]
    return any(abs(o["x1"] - me["x1"]) <= 2.0 for o in column)


def move_labels_to_rules(page, found, fonts, pad=3.0):
    """Slide a caption across to the rule it labels, leaving the rule alone.

    This is the other way of closing the gap, for the rules that belong to a
    column: the column keeps its edge and the label comes to it. The strip
    between the two is empty - that is how the gap was found - so the caption
    has somewhere to go, and only its own spans are lifted and set again.

    A caption holding a glyph Arial cannot draw is left where it is: those
    spans are still in the form's own font, and moving one means carrying
    that font's encoding with it.

    Returns the gaps it closed. A caption it could not take is simply not in
    the list, so the caller can report it rather than assume it was done."""
    want = [g for g in found if g["gap"] > pad + 0.5]
    if not want:
        return []
    # The page is read once. Every span is either one being moved or one
    # staying put, and telling them apart by identity only works if they came
    # from the same reading - two calls to get_text build two sets of dicts.
    rows = []
    for block in printed(page).get_text("dict")["blocks"]:
        for ln in block.get("lines", ()):
            spans = [sp for sp in ln["spans"] if sp["text"].strip()]
            if not spans:
                continue
            said = "".join(sp["text"] for sp in ln["spans"]).strip()
            rows.append({"box": fitz.Rect(ln["bbox"]),
                         "base": ln["spans"][0]["origin"][1],
                         "spans": spans,
                         # the same test verify_overlap uses to decide which
                         # lines are prose worth watching for a clash
                         "wordy": sum(c.isalpha() for c in said) >= 3
                                  and "_" not in said})
    every = [sp for r in rows for sp in r["spans"]]
    jobs, going = [], set()
    for g in want:
        room = g["room"] + (-1, -1.5, 0.5, 1.5)
        spans = [sp for sp in every
                 if id(sp) not in going
                 and room.x0 <= sp["bbox"][0] and sp["bbox"][2] <= room.x1
                 and room.y0 <= sp["bbox"][1] and sp["bbox"][3] <= room.y1]
        if not spans:
            continue
        if not all(drawable(sp["text"], style_of(sp["flags"], fonts)[2])
                   for sp in spans):
            continue
        # Does it actually have room? The gap was found by looking along the
        # caption's own baseline, and a form can set another column's text a
        # point or two above or below that line, close enough to be run into.
        # So the caption's box is tried in its new place against every other
        # piece of text on the page before anything is erased. This is what
        # stops a row of a rate table - which reads as a long caption - from
        # being slid into its neighbour.
        dx = g["gap"] - pad
        ink = fitz.Rect(min(sp["bbox"][0] for sp in spans),
                        min(sp["bbox"][1] for sp in spans),
                        max(sp["bbox"][2] for sp in spans),
                        max(sp["bbox"][3] for sp in spans)) + (dx, 0, dx, 0)
        mine = {id(sp) for sp in spans}
        if any(ink.x0 < sp["bbox"][2] - 0.5 and sp["bbox"][0] + 0.5 < ink.x1
               and ink.y0 < sp["bbox"][3] - 0.5 and sp["bbox"][1] + 0.5 < ink.y1
               for sp in every if id(sp) not in mine):
            continue
        # And the same question at the granularity verify_overlap asks it:
        # whole lines sharing a row. Ink can clear ink while the line the
        # caption belongs to grows across its neighbour's box, which is how a
        # row of a withholding-tax table came to be slid into the column
        # beside it - no letter touching another, and plainly wrong.
        home = next((r for r in rows
                     if any(id(sp) in mine for sp in r["spans"])), None)
        if home is not None and home["wordy"]:
            stays = [fitz.Rect(sp["bbox"]) for sp in home["spans"]
                     if id(sp) not in mine]
            grown = fitz.Rect(ink)
            for r in stays:
                grown |= r
            if any(other is not home and other["wordy"]
                   and abs(home["base"] - other["base"]) <= 2
                   and grown.x1 > other["box"].x0 + 0.5
                   and other["box"].x1 > grown.x0 + 0.5
                   for other in rows):
                continue
        going.update(mine)
        jobs.append((g, spans, dx))
    # Labels that end against one column of rules move together or not at
    # all. Moving "Subtotal" while "Total" over it cannot go leaves the two
    # ending at different places - worse than both staying where they were.
    placed_ = {id(g) for g, _, _ in jobs}
    stuck = {round(g["to"]) for g in want if id(g) not in placed_}
    jobs = [j for j in jobs if round(j[0]["to"]) not in stuck]
    if not jobs:
        return []
    # Redaction takes every glyph whose box touches the rect, so a caption's
    # box is cut around every span that is staying where it is. Without that,
    # a tight line under the caption loses its letters to a rect drawn a
    # fraction of a point too tall, and they are never put back.
    staying = [fitz.Rect(sp["bbox"]) for sp in every if id(sp) not in going]
    forget(page)                        # the page is about to change
    for _, spans, _ in jobs:
        for sp in spans:
            for piece in clip_out(fitz.Rect(sp["bbox"]), staying):
                page.add_redact_annot(piece, fill=None, cross_out=False)
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                          graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                          text=fitz.PDF_REDACT_TEXT_REMOVE)
    done = []
    for g, spans, dx in jobs:
        for sp in spans:
            alias, path, _ = style_of(sp["flags"], fonts)
            page.insert_text(fitz.Point(sp["origin"][0] + dx, sp["origin"][1]),
                             sp["text"], fontname=alias, fontfile=path,
                             fontsize=sp["size"], color=span_colour(sp),
                             overlay=True)
        ink = fitz.Rect(min(sp["bbox"][0] for sp in spans),
                        min(sp["bbox"][1] for sp in spans),
                        max(sp["bbox"][2] for sp in spans),
                        max(sp["bbox"][3] for sp in spans))
        done.append(dict(g, drawn=ink + (dx, 0, dx, 0)))
    forget(page)
    return done


def underlined_lines(page):
    """The lines a form underlines with a drawn rule: a heading like "Owner
    Signatures" in Word, the rule a hair under the baseline and as long as
    the words. [(baseline, rule (y, x0, x1, width, colour))], read from the
    page as the form printed it."""
    p = printed(page)
    lines, _ = collect_lines(p, False, "Arial", 10.0, 0.6)
    # the rules, and how each is drawn - a stroked line, or a sliver of fill,
    # which is how Word underlines - so a piece added to one matches it
    rules = []
    for d in p.get_drawings():
        colour = d.get("color") or d.get("fill") or (0, 0, 0)
        for item in d["items"]:
            if item[0] == "l" and d["type"] in ("s", "fs"):
                a, b = item[1], item[2]
                if abs(a.y - b.y) <= 0.6 and abs(a.x - b.x) >= 12:
                    rules.append(((a.y + b.y) / 2, min(a.x, b.x), max(a.x, b.x),
                                  max(d.get("width") or 0.6, 0.4), tuple(colour), "s"))
            elif item[0] == "re" and d["type"] == "f":
                r = fitz.Rect(item[1])
                if 0 < r.height <= 1.5 and r.width >= 4:
                    rules.append(((r.y0 + r.y1) / 2, r.x0, r.x1, r.height,
                                  tuple(d.get("fill") or colour), "f"))
    trace = [c for run in p.get_texttrace() if run.get("type") in (0, 1)
             for c in run.get("chars", []) if c[0] > 32]
    walls = uprights(p)
    out = []
    for ln in lines:
        base = ln["spans"][0]["origin"][1]
        size = max(sp["size"] for sp in ln["spans"])
        ink = [fitz.Rect(c[3]) for c in trace if glyph_in(c[3], ln["bbox"])]
        if not ink:
            continue
        # the whole line, and each run of it a tab stop sets apart: "2.<tab>
        # Beneficiary Bank:" underlines the heading but not its number, and
        # follow_underlines splits a line into runs the same way
        ink.sort(key=lambda r: r.x0)
        spans_ = [(ink[0].x0, max(r.x1 for r in ink))]
        start = 0
        for k in range(1, len(ink) + 1):
            if k == len(ink) or ink[k].x0 - max(r.x1 for r in ink[start:k]) > 6.0:
                if start or k < len(ink):
                    spans_.append((ink[start].x0, max(r.x1 for r in ink[start:k])))
                start = k
        for rule in rules:
            y, rx0, rx1 = rule[0], rule[1], rule[2]
            if (rx1 - rx0 >= 12 and base + 0.2 <= y <= base + 0.45 * size
                    and any(abs(rx0 - x0) <= 3 and abs(rx1 - x1) <= 3 for x0, x1 in spans_)):
                out.append((base, rule))
                break
        else:
            # Words underlined inside a sentence - "I/We hereby authorize",
            # "Cheque Payable To", a web address. Re-set in Arial the words
            # move along the line and the rule stays where they were. What
            # the rule covers is recorded as text, with which occurrence of it
            # on the line, so it can be found again on the restyled page.
            chars = dedupe(sorted((c for c in trace if glyph_in(c[3], ln["bbox"])),
                                  key=lambda c: c[3][0]))
            text = "".join(chr(c[0]).replace("­", "-") for c in chars)
            for rule in rules:
                y, rx0, rx1 = rule[0], rule[1], rule[2]
                if not base + 0.2 <= y <= base + 0.45 * size:
                    continue
                # a cell's top or bottom edge meets the cell's sides; an
                # underline meets nothing (Lost Item's "Cheque Payable To"
                # sits on the top edge of the cell below it)
                if any(w.y0 - 1.5 <= y <= w.y1 + 1.5
                       and (abs(w.x0 - rx0) <= 1.5 or abs(w.x1 - rx0) <= 1.5
                            or abs(w.x0 - rx1) <= 1.5 or abs(w.x1 - rx1) <= 1.5)
                       for w in walls):
                    continue
                under = [k for k, c in enumerate(chars)
                         if rx0 - 1 <= (c[3][0] + c[3][2]) / 2 <= rx1 + 1]
                if not under or under != list(range(under[0], under[-1] + 1)):
                    continue
                if len(under) == len(chars):
                    continue                 # the whole line: handled above, or not ours
                # an underline is as long as its words; a cell's border that
                # runs on under a label ("Branch:") is not one
                if (abs(rx0 - chars[under[0]][3][0]) > 3
                        or abs(rx1 - chars[under[-1]][3][2]) > 3):
                    continue
                words = text[under[0]:under[-1] + 1]
                nth = text[:under[0]].count(words)
                out.append((base, rule, words, nth))
    return out


def dedupe(chars):
    """Glyphs drawn twice in the same place count once: a form that fakes bold
    by printing each letter twice reads "oorr" in its trace. Spaces of every
    kind go too - the restyled page writes its spaces as no-break spaces, and
    compared with the form's own text "MBX - Automated" never matched."""
    out = []
    for c in chars:
        if chr(c[0]).isspace():
            continue
        if out and out[-1][0] == c[0] and abs(out[-1][2][0] - c[2][0]) < 0.8                 and abs(out[-1][2][1] - c[2][1]) < 0.8:
            continue
        out.append(c)
    return out


def inline_field_lines(src):
    """Lines to write on, for text boxes the form set inside a row of words
    with nothing under them: "In consideration of advances made to [box]
    and [box]" on the Hypothecation Agreement. The rebuild draws a line under
    every field; restyled, these were gaps nobody could see were blanks.

    Only a one-line text box with words beside it on its row, nothing ruled
    or typed under it, no words inside it, no value already shown, and not
    a cell of a table. Returns [(x0, x1, y)] in the page's coordinates."""
    p = printed(src)
    words = p.get_text("words")
    rules = [r for r in rule_segments(p) if r.height < 2.5]
    walls = uprights(p)
    unders = [c for t in p.get_texttrace() for c in t["chars"] if c[0] == 95]
    out = []
    for w in src.widgets():
        if w.field_type_string != "Text" or (w.field_value or "").strip():
            continue
        b = fitz.Rect(w.rect)
        # (a box up to two lines deep: Wire Instructions' name boxes are 27
        # to 44pt; what sits beside it is read against its first line)
        if b.height > 48 or b.width < 30:
            continue
        mid = (b.y0 + min(b.y1, b.y0 + 24)) / 2
        row = [x for x in words if x[1] - 2 <= mid <= x[3] + 2]
        beside = [x for x in row if (x[2] <= b.x0 + 2 and b.x0 - x[2] <= 120)
                  or (x[0] >= b.x1 - 2 and x[0] - b.x1 <= 120)]
        if not beside:
            continue
        if any(x[0] < b.x1 - 2 and x[2] > b.x0 + 2 for x in row):
            continue                                  # words inside the box
        if any(b.y1 - 6 <= (r.y0 + r.y1) / 2 <= b.y1 + 6
               and min(r.x1, b.x1) - max(r.x0, b.x0) >= 0.5 * b.width for r in rules):
            continue                                  # already ruled
        if any(b.x0 - 2 <= c[3][0] <= b.x1 + 2 and b.y0 - 2 <= c[2][1] <= b.y1 + 6
               for c in unders):
            continue                                  # a typed blank there
        if any(abs(v.x0 - b.x0) <= 4 or abs(v.x1 - b.x1) <= 4
               for v in walls if v.y0 <= mid <= v.y1):
            continue                                  # a cell of a table
        base = max(x[3] for x in beside) - 2.5        # about the words' baseline
        out.append((b.x0 + 1, b.x1 - 1, min(b.y1 - 1, base + 3)))
    # boxes side by side on a row (Wire Instructions' transit and account
    # number after "828-") are one line, not two with a notch between
    out.sort(key=lambda t: (round(t[2]), t[0]))
    joined = []
    for x0, x1, y in out:
        if joined and abs(joined[-1][2] - y) <= 1.5 and 0 <= x0 - joined[-1][1] <= 8:
            joined[-1] = (joined[-1][0], x1, joined[-1][2])
        else:
            joined.append((x0, x1, y))
    return joined


def follow_underlines(page, marked, keep_out=(), gap=6.0):
    """Make each underline reach the ends of its words again.

    Set in Arial, or relabelled, an underlined heading comes out wider than
    the rule under it: "Owner Signatures" became "Authorized Signature" and
    its rule stopped under the "u" at each end. As with a rule run back to
    its caption, the form's own rule is left as it is and a matching piece
    is laid at each end it now falls short of. `keep_out` holds lines being
    re-wrapped, whose words no longer sit on the rule's line. Returns what
    it drew, for the report and the line-art check."""
    done = []
    if not marked:
        return done
    trace = [c for run in page.get_texttrace() if run.get("type") in (0, 1)
             for c in run.get("chars", []) if c[0] > 32]
    trims = []
    shape = page.new_shape()
    # the rules still on the page: one a signing area took away with its
    # heading is not there to follow, and run out to letters near its line it
    # drew pieces under the standard block's captions
    here = rule_segments(printed(page))
    for entry in marked:
        base, (y, rx0, rx1, width, colour, kind) = entry[0], entry[1]
        if any(abs(base - b) < 0.6 for b in keep_out):
            continue
        if not any(abs((s.y0 + s.y1) / 2 - y) <= 1.5 and s.x0 < rx1 and rx0 < s.x1 for s in here):
            continue
        if len(entry) == 4:
            words, nth = entry[2], entry[3]
            row = dedupe(sorted((c for c in trace if abs(c[2][1] - base) <= 1.0),
                                key=lambda c: c[3][0]))
            line = "".join(chr(c[0]).replace("­", "-") for c in row)
            at, k = -1, -1
            while k < nth:
                at = line.find(words, at + 1)
                if at < 0:
                    break
                k += 1
            if at < 0:
                continue                     # reworded or re-wrapped: left as it was
            nx0, nx1 = row[at][3][0], row[at + len(words) - 1][3][2]
            if abs(nx0 - rx0) > 1.0 or abs(nx1 - rx1) > 1.0:
                trims.append((fitz.Rect(rx0, y - width / 2, rx1, y + width / 2),
                              nx0, nx1, y, width, colour, kind))
            continue
        near = sorted((fitz.Rect(c[3]) for c in trace
                       if abs(c[2][1] - base) <= 1.0
                       and c[3][2] >= rx0 - 80 and c[3][0] <= rx1 + 80),
                      key=lambda r: r.x0)
        if not near:
            continue
        # the run of letters the rule is under: neighbours on the same
        # baseline further along the row are not part of it
        runs, cur = [], [near[0]]
        for r in near[1:]:
            if r.x0 - cur[-1].x1 > gap:
                runs.append(cur)
                cur = [r]
            else:
                cur.append(r)
        runs.append(cur)
        best = max(runs, key=lambda run: min(run[-1].x1, rx1) - max(run[0].x0, rx0))
        nx0, nx1 = best[0].x0, max(r.x1 for r in best)
        if min(nx1, rx1) - max(nx0, rx0) <= 0:
            continue
        # Set smaller - a 12pt heading at 10 - the words end well inside
        # their rule, and "Beneficiary Bank:" trailed 12pt of underline past
        # the colon. Then the rule itself is taken up and laid again under
        # the words, the same kind and weight; only that one path is removed.
        if nx0 > rx0 + 1.5 or nx1 < rx1 - 1.5:
            old = fitz.Rect(rx0, y - width / 2, rx1, y + width / 2)
            trims.append((old, max(nx0, rx0) if nx0 > rx0 + 1.5 else min(nx0, rx0),
                          min(nx1, rx1) if nx1 < rx1 - 1.5 else max(nx1, rx1),
                          y, width, colour, kind))
            continue
        pieces = []
        if nx0 < rx0 - 1.0:
            pieces.append((nx0, rx0))
        if nx1 > rx1 + 1.0:
            pieces.append((rx1, nx1))
        for a, b in pieces:
            if kind == "f":
                shape.draw_rect(fitz.Rect(a, y - width / 2, b, y + width / 2))
                shape.finish(color=None, fill=colour, width=0)
            else:
                shape.draw_line(fitz.Point(a, y), fitz.Point(b, y))
                shape.finish(color=colour, width=width)
            done.append({"y": y, "from": a, "to": b, "colour": colour, "width": width,
                         "kind": kind, "drawn": fitz.Rect(a, y - width, b, y + width)})
    shape.commit()
    for old, a, b, y, width, colour, kind in trims:
        # one redaction per rule, as the signing pass does: taken together,
        # strips that touch can leave a piece standing
        page.add_redact_annot(old + (-0.5, -0.5, 0.5, 0.5), fill=None, cross_out=False)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                              graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                              text=fitz.PDF_REDACT_TEXT_NONE)
    if trims:
        forget(page)
        shape = page.new_shape()
        for old, a, b, y, width, colour, kind in trims:
            if kind == "f":
                shape.draw_rect(fitz.Rect(a, y - width / 2, b, y + width / 2))
                shape.finish(color=None, fill=colour, width=0)
            else:
                shape.draw_line(fitz.Point(a, y), fitz.Point(b, y))
                shape.finish(color=colour, width=width)
            done.append({"y": y, "from": a, "to": b, "colour": colour, "width": width,
                         "kind": kind, "drawn": fitz.Rect(a, y - width, b, y + width),
                         "replaced": old, "was": old.x1 - old.x0})
        shape.commit()
    return done


def close_label_gaps(page, found, pad=3.0):
    """Run each rule back to its caption, and say where that was done.

    The rule is not redrawn - the form's own line is left exactly as it is
    and a matching piece is laid in front of it, same height, same colour,
    starting a hair after the caption so it never runs under the letters.
    Nothing moves, which is the point: these forms are ones whose layout is
    already right."""
    shape = page.new_shape()
    done = []
    for g in found:
        x0 = g["from"] + pad
        if x0 >= g["to"] - 0.5:
            continue
        shape.draw_line(fitz.Point(x0, g["y"]), fitz.Point(g["to"], g["y"]))
        done.append(dict(g, drawn=fitz.Rect(x0, g["y"] - g["width"],
                                            g["to"], g["y"] + g["width"])))
        shape.finish(color=g["colour"], width=g["width"])
    shape.commit()
    return done


# A mail-merge tag the form was built with: «Customer_Number», <<Branch_Phone>>.
# Word's own prompts in an empty content control, which export to PDF as
# grey print - "Click or tap here to enter text." - are the same thing: a
# blank for someone to fill in.
MERGE_TAG = re.compile(r"«[^»\n]{1,60}»|<<[^>\n]{1,60}>>"
                       # any space between the words: re-set in Arial they
                       # read back as no-break spaces
                       r"|Click\s+or\s+tap\s+here\s+to\s+enter\s+text\."
                       r"|Click\s+or\s+tap\s+to\s+enter\s+a\s+date\.")


def merge_tag_name(tag, before=""):
    """The name for the box a merge tag becomes: the tag's own name, or for
    one of Word's prompts, the last few words before it ("...Transfer of
    Funds to RRIF#" gives Funds_to_RRIF)."""
    if tag.startswith(("«", "<<")):
        return re.sub(r"\W+", "_", tag.strip("«»<> ")).strip("_") or "field"
    words = re.sub(r"[^\w\s]", " ", before).split()[-3:]
    stem = "_".join(words) or ("date" if "date" in tag else "text")
    return ("Date_" + stem) if "date" in tag and "date" not in stem.lower() else stem


def merge_tags_to_fields(page, taken, min_width=90.0, margin=36.0):
    """Take each mail-merge tag off the page and put a text box where it was.

    "«Customer_Number»" is an instruction to a mail merge that never ran, and
    printed as it stands it is gibberish to a member. The client's word was
    to remove the wording between the brackets and put a text box in its
    place. Only the tag's own glyphs are erased - the words either side of
    it, even in the middle of a sentence, stay exactly where they are - and
    the box is at least `min_width` wide where the line has room for it, so
    a short tag still gets a box someone can type an account number into.

    Returns what it did, so the caller can tell the glyph census which
    characters went on purpose and report every box by name. `taken` is the
    set of field names already in the document; a new box never reuses one."""
    raw = page.get_text("rawdict")
    words = page.get_text("words")
    right = page.rect.width - margin
    found = []
    for block in raw["blocks"]:
        for ln in block.get("lines", ()):
            chars = [c for sp in ln["spans"] for c in sp["chars"]]
            text = "".join(c["c"] for c in chars)
            for m in MERGE_TAG.finditer(text):
                glyphs = chars[m.start():m.end()]
                box = fitz.Rect(glyphs[0]["bbox"])
                for c in glyphs[1:]:
                    box |= fitz.Rect(c["bbox"])
                base = glyphs[0]["origin"][1]
                # room to the right: up to the next word on this line, else
                # the margin - never over anything already printed
                after = [w[0] for w in words
                         if w[0] >= box.x1 - 0.5 and w[1] < box.y1 and w[3] > box.y0]
                limit = min(after) - 2 if after else right
                # and never over a character that followed the tag in the same
                # word - the full stop in "branch at «Branch_Phone»." is part
                # of the tag's word, so the word boxes alone would miss it
                rest = [c for c in chars[m.end():] if not c["c"].isspace()]
                if rest:
                    limit = min(limit, rest[0]["bbox"][0] - 1)
                wide = max(box.width, min(min_width, limit - box.x0))
                name = merge_tag_name(m.group(0), text[:m.start()])
                stem, n = name, 2
                while name in taken:
                    name, n = f"{stem}_{n}", n + 1
                taken.add(name)
                found.append({"tag": m.group(0), "name": name, "ink": box,
                              "rect": fitz.Rect(box.x0, box.y0 - 1,
                                                box.x0 + wide, box.y1 + 1),
                              "glyphs": [(base, c["c"]) for c in glyphs]})
    if not found:
        return []
    forget(page)
    for f in found:
        # a hair inside the tag's own box, so the letters either side - which
        # touch it - are not taken with it
        page.add_redact_annot(f["ink"] + (0.4, 0.3, -0.4, -0.3), fill=None,
                              cross_out=False)
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                          graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                          text=fitz.PDF_REDACT_TEXT_REMOVE)
    for f in found:
        w = fitz.Widget()
        w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
        w.field_name = f["name"]
        w.rect = f["rect"]
        w.text_font = "Helv"
        w.text_fontsize = 0          # auto: fits whatever is typed
        w.border_width = 0
        page.add_widget(w)
    forget(page)
    return found


# A note the form's author left for whoever builds it, set in pure red and
# never meant for a member: "import todays date", "manually input from
# insurance policy doc", "Can we remove Essex address, do we want to add LK
# head office?". Red on its own is not enough - Wire Outgoing prints its
# "$50.00" fee in red, and a warning can be red - so a note also has to read
# as one: a data-source instruction, or words addressed to the form's author.
NOTE_ASKS = re.compile(r"^\s*(import|manually|populate|auto-?populate|pull from)\b", re.I)
NOTE_TALK = re.compile(r"\?|^\s*(?:(?:can|could|do|does|should|shall|would|will) we"
                       r"|there (?:is|are)|we need|need to|to ?do\b|note to|fyi)\b", re.I)


def author_red(colour):
    r, g, b = (colour >> 16) & 255, (colour >> 8) & 255, colour & 255
    return r >= 200 and g <= 60 and b <= 60


def author_notes(page):
    """The author's notes on a page, each with its lines, where each letter
    sits, and what it asks for: `asks` when it names where a value comes from
    (it wants a box to type into), and `under` the field already covering it,
    if one does."""
    raw = page.get_text("rawdict")
    lines = []
    for block in raw["blocks"]:
        for ln in block.get("lines", ()):
            glyphs = [(c, sp["size"]) for sp in ln["spans"] if author_red(sp["color"])
                      for c in sp["chars"]]
            ink = [c for c, _ in glyphs if not c["c"].isspace()]
            if not ink:
                continue
            box = fitz.Rect(ink[0]["bbox"])
            for c in ink[1:]:
                box |= fitz.Rect(c["bbox"])
            lines.append({"box": box, "size": glyphs[0][1],
                          # Word joins a note's words with no-break spaces
                          "text": " ".join("".join(c["c"] for c, _ in glyphs).split()),
                          "glyphs": [(c["origin"][1], c["c"], fitz.Rect(c["bbox"]))
                                     for c, _ in glyphs]})
    lines.sort(key=lambda l: (l["box"].y0, l["box"].x0))
    notes = []
    for l in lines:
        # a note set over several lines is one note: the same left edge, and
        # no more than a line's leading between one line and the next. A line
        # that asks for a value of its own is a note of its own, though - a
        # stack of "import todays date", "import member #" is one box each.
        home = None if NOTE_ASKS.match(l["text"]) else next((n for n in notes
                     if abs(l["box"].x0 - n["lines"][-1]["box"].x0) <= 4
                     and -0.4 * l["size"] <= l["box"].y0 - n["lines"][-1]["box"].y1
                     <= 0.8 * l["size"]), None)
        if home:
            home["lines"].append(l)
        else:
            notes.append({"lines": [l]})
    out = []
    fields = [w for w in page.widgets()]
    for n in notes:
        text = " ".join(l["text"] for l in n["lines"])
        asks = bool(NOTE_ASKS.match(text))
        if len(re.findall(r"[A-Za-z]{2,}", text)) < (2 if asks else 3):
            continue
        if not (asks or NOTE_TALK.search(text)):
            continue
        box = fitz.Rect(n["lines"][0]["box"])
        for l in n["lines"][1:]:
            box |= l["box"]
        first = n["lines"][0]["box"]
        under = next((w.field_name for w in fields
                      if w.field_type_string == "Text"
                      and (fitz.Rect(w.rect) & first).get_area() >= 0.5 * first.get_area()), None)
        out.append({"text": text, "lines": n["lines"], "box": box, "asks": asks,
                    "under": under})
    return out


def note_field_name(words, note, taken):
    """The name for the box a note asks for: the caption it answers - "Your
    Policy:" gives Your_Policy - or its own first words if it stands alone.
    Never one already in `taken`, which it joins."""
    first = note["lines"][0]["box"]
    line = sorted((w for w in words if w[1] < first.y1 and w[3] > first.y0
                   and w[2] <= first.x0 + 0.5), key=lambda w: w[0])
    group = line[-1:]
    for w in reversed(line[:-1]):
        if group[0][0] - w[2] >= 6.0:
            break
        group.insert(0, w)
    said = " ".join(w[4] for w in group) or " ".join(note["text"].split()[:3])
    name = re.sub(r"\W+", "_", said).strip("_") or "field"
    stem, k = name, 2
    while name in taken:
        name, k = f"{stem}_{k}", k + 1
    taken.add(name)
    return name


def author_notes_off(page, taken, min_width=90.0, margin=36.0):
    """Take the author's notes off the page.

    The client's hand-cleaned forms show what they want done with them: a
    note naming where a value comes from becomes the box that value goes in,
    unless a box is already there - "import member #" sits under the form's
    own member-number field - and a remark to the author simply goes. Only
    the note's own letters are erased. Returns what went, so the caller can
    tell the glyph census and name every note in the report; `taken` is the
    set of field names in use, and a new box never reuses one."""
    notes = author_notes(page)
    if not notes:
        return []
    words = page.get_text("words")
    right = page.rect.width - margin
    for n in notes:
        n["made"] = None
        if not n["asks"] or n["under"]:
            continue
        first = n["lines"][0]["box"]
        name = note_field_name(words, n, taken)
        after = [w[0] for w in words if w[0] >= first.x1 - 0.5
                 and w[1] < first.y1 and w[3] > first.y0]
        limit = min(after) - 2 if after else right
        wide = max(first.width, min(min_width, limit - first.x0))
        n["made"] = {"name": name, "rect": fitz.Rect(first.x0, first.y0 - 1,
                                                     first.x0 + wide, first.y1 + 1)}
    forget(page)
    for n in notes:
        for l in n["lines"]:
            for _, ch, box in l["glyphs"]:
                if not ch.isspace():
                    # a hair inside each letter, so a neighbour that touches
                    # it is not taken with it
                    page.add_redact_annot(box + (0.4, 0.3, -0.4, -0.3), fill=None,
                                          cross_out=False)
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                          graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                          text=fitz.PDF_REDACT_TEXT_REMOVE)
    for n in notes:
        if n["made"]:
            w = fitz.Widget()
            w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
            w.field_name = n["made"]["name"]
            w.rect = n["made"]["rect"]
            w.text_font = "Helv"
            w.text_fontsize = 0
            w.border_width = 0
            page.add_widget(w)
    forget(page)
    return notes


# Words a signing area is made of. A line of text counts as part of one only
# if every word in it is here - "Dated at ___, Ontario" or "Quota No." is not,
# and stops the area where it stands.
SIGN_WORDS = set("""
signature signatures witness witnesses name names date owner owners joint
borrower borrowers producer producers mortgagor lessee lessees member members
authorized authorizing authorised staff of the please print or type signing
officer import from above per and applicant applicants co holder holders
annuitant grantor spouse trustee trustees printed here sign s
signed credit union libro representative
witnessed by commissioner commissioners oaths notary public for request agent as office a
if two more are required limited
authorization employee officers attorney attorneys
""".split())
# ("(if two or more signatures are required)" under PAD's joint owner lines
#  cut its signing grid into pieces too small for the block)
# ("Date Signed" between a staff line and a signature line on TFSA's row was
#  not signing language, so the block was refused for drawing over it; nor
#  was "Credit Union Witness")
# (and the marketing board's acknowledgement under a quota direction: "The
#  Chicken Farmers of Ontario hereby acknowledge receipt ..." over one
#  "Authorized Signature" line is the Board signing, not the member)
SIGN_HEAD = re.compile(r"(?:farmers|producers|board|commission)\b.{0,60}hereby acknowledge|"
                       r"office use|use only|for\s+\w+\s+use|dfo use|for use by|"
                       r"institution only", re.I)
SIGN_JOINS = {"and", "or", "of", "the", "per", "s", "a", "by", "for", "as"}
# who is signing: an area has to name someone, or say sign. "Name: ___ and
# ___" is in signing words from end to end and is a block of names.
SIGN_ROLE = re.compile(r"sign|witness|borrower|owner|member|joint|producer|mortgagor|attorney|"
                       r"lessee|applicant|holder|annuitant|grantor|spouse|trustee|"
                       r"officer|staff|authori[sz]ed", re.I)


def signing_words(text):
    """The words of a line, as the signing vocabulary sees them."""
    return [w for w in re.split(r"[^a-z]+", text.lower()) if w]


# a caption for something other than signing, and one that still has to
# do with it: "Phone", "Fax Number", "Name and address of Financial
# Institution" against "Date of request", "Witnessed by ... Notary Public"
DETAILS = re.compile(r"address|phone|fax|telephone|contact|institution|account|amount|e-?mail",
                     re.I)
SIGNISH = re.compile(r"witness|date|agent|representative|sign|officer|staff|name|notary|"
                     r"commissioner", re.I)
ACCEPTS = re.compile(r"\bI(?:/we)?\b.{0,90}\b(?:accept|agree|acknowledge|consent|authori[sz]e)|"
                     r"\bby signing\b|\bsigned below\b"
                     # an agreement's own words for signing: Milk's "IN WITNESS
                     # WHEREOF the Borrower has hereunto set his hand and seal"
                     r"|\bin witness whereof\b|\bhereunto set\b|signed,? sealed and delivered",
                     re.I)
CORPORATE = re.compile(r"authority to bind|bind the (?:corporation|company)|"
                       r"\bcorporat|\bc/s\b", re.I)


def signing_areas(page, min_rules=2):
    """Where a page asks its members to sign, and how many of them.

    A signing area is a run of lines to write on - drawn rules or typed
    underscores - whose surrounding text is only the language of signing:
    "Witness", "Owner", "Signature(s) of Producer(s)", "Date", "(please print
    or type)". Each line is given the role its caption says - a witness's
    line, a date, a name, or a signature - and every signature line is one
    member signing.

    An area under an ACKNOWLEDGEMENT or OFFICE USE heading is someone else
    signing - the Board, a Libro officer - and is not a member's area. An
    area of a single line is ambiguous and left alone. Both come back in
    `declined` so they can be reported, never passed over in silence."""
    p = printed(page)
    words = p.get_text("words")
    verts = upright_edges(p)
    rules = []
    for y, x0, x1, width, colour in flat_rules(p):
        if x1 - x0 > page.rect.width * 0.7:
            continue                       # a divider across the page
        if any(abs(vx - x0) <= 2.0 and vy0 - 2 <= y <= vy1 + 2
               for vx, vy0, vy1 in verts):
            continue                       # the edge of a ruled cell
        rules.append({"y": y, "x0": x0, "x1": x1, "typed": False})
    for w in words:
        if re.fullmatch(r"_{6,}[.:,]?", w[4]):
            rules.append({"y": w[3] - 1, "x0": w[0], "x1": w[2], "typed": True,
                          "box": fitz.Rect(w[:4])})
    if not rules:
        return [], []
    sig_fields = [fitz.Rect(w.rect) for w in page.widgets()
                  if w.field_type == fitz.PDF_WIDGET_TYPE_SIGNATURE]
    lines, frags = [], []
    for b in p.get_text("dict")["blocks"]:
        for ln in b.get("lines", ()):
            spans = [sp for sp in ln["spans"] if sp["text"].strip()
                     and not re.fullmatch(r"[_\s.:,]*", sp["text"])]
            if not spans:
                # a lone comma or full stop where a blank used to end is a
                # fragment too (see below), but never underscores: those are
                # lines to write on
                bits = [sp for sp in ln["spans"] if re.fullmatch(r"\s*[.:,;]+\s*", sp["text"])]
                frags.extend((fitz.Rect(sp["bbox"]), sp["text"].strip()) for sp in bits)
                continue
            box = fitz.Rect(spans[0]["bbox"])
            for sp in spans[1:]:
                box |= fitz.Rect(sp["bbox"])
            text = " ".join("".join(sp["text"] for sp in spans).split())
            if len(re.sub(r"\W", "", text)) <= 2:
                # a stray glyph or two - RRIF's "h", "d", "ff", a comma left
                # where a blank was - says nothing; it goes with an area it
                # sits in, or the block is refused for drawing over it
                frags.append((box, text))
                continue
            said = signing_words(text)
            # "and" on its own - the "and ___ and ___" of a parties block -
            # is a joining word, not a caption: signing language has to say
            # something besides
            lines.append({"box": box, "text": text,
                          "signing": bool(said) and all(w in SIGN_WORDS for w in said)
                                     and any(w not in SIGN_JOINS for w in said)})

    # A line the width of the words just over it is their underline, not
    # somewhere to sign: Safe Deposit's "Owner Signatures" heading had its
    # underline read as a signature line, and the block was refused for
    # being drawn over the heading.
    # (centred under the words and about as wide: a heading the relabel
    #  rewrote keeps its old underline - "Owner Signatures" became
    #  "Authorized Signature", 9pt off at each end)
    under_of = lambda r, l: (-3 <= r["y"] - l["box"].y1 <= 4
                             and abs((r["x0"] + r["x1"]) / 2 - (l["box"].x0 + l["box"].x1) / 2) <= 6
                             and 0.5 * l["box"].width <= r["x1"] - r["x0"] <= l["box"].width * 1.3 + 6)
    underlines_ = [(r, l) for r in rules if not r["typed"] for l in lines if under_of(r, l)]
    rules = [r for r in rules if r["typed"] or not any(under_of(r, l) for l in lines)]

    # which line each piece of text sits under, if any: the nearest one
    # above it that it overlaps. A caption belongs to that line alone.
    # A label set just before a line on its baseline - "Trustee: ___" - is
    # that line's, beyond doubt, and is never read as the caption under some
    # other line above it.
    # (a label's span can carry trailing spaces a few points into its line)
    before = lambda l, r: abs(l["box"].y1 - 2 - r["y"]) <= 6 and -6 <= r["x0"] - l["box"].x1 <= 70
    labels = {id(l) for l in lines for r in rules if before(l, r)}
    owner_of = {}
    for l in lines:
        if id(l) in labels:
            continue
        # (a caption can start a point or two above its line: FHSA
        # Designation's "Checked By / Entered By / Entry Date" did, went
        # unowned, and their lines borrowed "Applicant Signature" from above)
        above = [r for r in rules if -3 <= l["box"].y0 - r["y"] <= 24
                 and l["box"].x0 < r["x1"] and r["x0"] < l["box"].x1]
        if above:
            owner_of[id(l)] = id(min(above, key=lambda r: abs(l["box"].y0 - r["y"])))

    def caption_of(r):
        """The text that says what a line is for: under it, before it on the
        same line, or just over it."""
        under = [l for l in lines if owner_of.get(id(l)) == id(r)]
        left = [l for l in lines if before(l, r)]
        # a heading over a line is short - "Signature(s)" - and a sentence
        # that ends a little way above is not one: closed up, a page brought
        # "by the hand(s) of its proper officer(s)..." within reach of the
        # first signing row and that row stopped counting as one
        over = [l for l in lines if 0 <= r["y"] - l["box"].y1 <= 30
                and l["box"].x0 < r["x1"] and r["x0"] < l["box"].x1
                and owner_of.get(id(l)) in (None, id(r)) and len(l["text"]) <= 40
                # (a label typed before another line is that line's: FHSA
                #  Designation's ", in the Province of Ontario, on" captioned
                #  the witness line under it and made the row "details")
                and id(l) not in labels]
        for group in (left, under, over):
            if group:
                return min(group, key=lambda l: abs(l["box"].y0 - r["y"]))
        return None

    def role(cap):
        t = (cap["text"] if cap else "").lower()
        if "witness" in t:
            return "witness"
        # "Date Signed" is a date, not a second signature line
        if re.match(r"\W*date\b", t) and "signature" not in t:
            return "date"
        if "sign" in t:
            return "signature"
        if re.search(r"staff|employee|agent|\boffice\b|representative|credit union|libro", t):
            return "staff"            # "Authorized Office, as Agent" signs for Libro
        if re.fullmatch(r"\W*date\W*", t):
            return "date"
        if "name" in t:
            return "name"
        return "signature"                 # a bare line is somewhere to sign

    for r in rules:
        r["cap"] = caption_of(r)
        # A typed line's label can be typed onto it: "Dated: ________" is one
        # run of text, so nothing sat before the line and it read as a bare
        # line to sign on - PAD's signing grid counted a fifth signer.
        if r["cap"] is None and r["typed"]:
            left_ = [w for w in words if abs(w[3] - 1 - r["y"]) <= 2
                     and 0 <= r["x0"] - w[2] <= 12 and not re.fullmatch(r"[_.:,]*", w[4])]
            if left_:
                w = max(left_, key=lambda w: w[2])
                said_ = signing_words(w[4])
                r["cap"] = {"box": fitz.Rect(w[:4]), "text": w[4],
                            "signing": bool(said_) and all(x in SIGN_WORDS for x in said_)}
        r["role"] = role(r["cap"])
    # Whose a line is can depend on the one above it. "Trustee: ___ Witness:
    # ___" over "Signature: ___ Signature: ___" is two people, each with a
    # name line and a signature line under it: a caption that only says
    # "Signature" belongs to whoever the line above it in the same column
    # was for, and where a column does say "Signature" under a role, the
    # role's own line is that person's name.
    plain_sig = re.compile(r"^\W*signature\W*$", re.I)
    for r in sorted(rules, key=lambda r: r["y"]):
        if r["cap"] is None or not plain_sig.match(r["cap"]["text"]):
            continue
        above = [o for o in rules if 0 < r["y"] - o["y"] <= 40
                 and o["x0"] < r["x1"] and r["x0"] < o["x1"] and o["cap"] is not None]
        if not above:
            continue
        over = max(above, key=lambda o: o["y"])
        if over["role"] in ("witness", "staff"):
            r["role"] = over["role"]
        elif over["role"] == "signature":
            over["role"] = "name"          # the role's line is its name line
    # Keep only the lines whose captions are all signing language, then cut
    # them into areas wherever something else is printed in between.
    def blank_of_something_else(r):
        """A bare line with other words just under it and close beside - the
        tail of the "Address" blank, set in underscores a point to the right
        of its caption - is that caption's blank, not somewhere to sign."""
        return any(not l["signing"] and 0 <= l["box"].y0 - r["y"] <= 16
                   and l["box"].x0 - 40 < r["x1"] and r["x0"] < l["box"].x1 + 40
                   for l in lines)

    # A corporation signing - Hypothecation's "I/We have the authority to bind
    # the corporation" under its officer's line, with the corporation's name
    # over it - is not a member's area, and the client keeps it as printed.
    # Its line, and whatever shares that row, stays out of every area.
    def corporate(r):
        return any(CORPORATE.search(l["text"]) and -4 <= l["box"].y0 - r["y"] <= 30
                   and l["box"].x0 < r["x1"] and r["x0"] < l["box"].x1 for l in lines)
    corp_rows = [r["y"] for r in rules if corporate(r)]
    # Uncaptioned pieces drawn end to end across most of the page are one
    # line to write on - RRIF Payment's last "special instructions" line, in
    # three pieces - not three places to sign. (A signing row drawn the same
    # way has its captions, and stays.)
    def chained(r):
        row_ = sorted((o for o in rules if abs(o["y"] - r["y"]) <= 1), key=lambda o: o["x0"])
        chain = [row_[0]]
        for o in row_[1:]:
            if o["x0"] - chain[-1]["x1"] <= 2:
                chain.append(o)
            elif r in chain:
                break
            else:
                chain = [o]
        return (r in chain and len(chain) > 1
                and chain[-1]["x1"] - chain[0]["x0"] > page.rect.width * 0.7
                and all(o["cap"] is None for o in chain))
    long_lines = [r for r in rules if r["cap"] is None and chained(r)]
    usable = sorted((r for r in rules
                     if r not in long_lines
                     and not any(abs(r["y"] - y) <= 6 for y in corp_rows)
                     and ((r["cap"] is None and not blank_of_something_else(r))
                          or (r["cap"] is not None and r["cap"]["signing"]))),
                    key=lambda r: (r["y"], r["x0"]))
    areas, run = [], []
    for r in usable:
        if run:
            gap_top, gap_bot = run[-1]["y"], r["y"]
            between = [l for l in lines if gap_top + 2 < l["box"].y0 < gap_bot - 2
                       and not l["signing"]]
            # (a witness's or staff line can sit a little further down: PAD's
            # "___ for Libro Credit Union / Witness" is 101pt under its grid)
            reach_ = 130 if r["role"] in ("witness", "staff") else 100
            if r["y"] - run[-1]["y"] > reach_ or between:
                areas.append(run)
                run = []
        run.append(r)
    if run:
        areas.append(run)

    found, declined = [], []
    for run in areas:
        caps = [r["cap"] for r in run if r["cap"]]
        # the lines of text that belong to the area: captions, and signing
        # language set just above the first line - "Signature(s)",
        # "SIGNATURES OF:" - that heads it
        top_y = min(r["y"] for r in run)
        foot_y = max(r["y"] for r in run)
        across = (min(r["x0"] for r in run) - 90, max(r["x1"] for r in run) + 90)
        # signing language at the area's edges belongs to it: a heading over
        # it - "Signature(s)", "Name(s): (please print or type)" - and the
        # caption of its last line, which a form can set well below the line
        # (a caption that belongs to some other line is that line's: FHSA
        # Withdrawal's "Date Signed" captions the line beside the bank's
        # address, and taken as a heading it was erased with the block)
        mine = {id(r) for r in run} | {None}
        # (to 56pt: Appointment of Agent's "Signature(s)" stood 51pt over its
        #  lines once rebuilt)
        heads = [l for l in lines if l["signing"] and 0 <= top_y - l["box"].y1 <= 56
                 and l["box"].x0 < across[1] and across[0] < l["box"].x1
                 and owner_of.get(id(l)) in mine]
        # (to 32pt: RRIF's "Authorized Officer, as Agent" is the second line
        #  of a caption, 27pt under its line, and left out it was "printing"
        #  the block could not be drawn over)
        tails = [l for l in lines if l["signing"] and 0 <= l["box"].y0 - foot_y <= 32
                 and l["box"].x0 < across[1] and across[0] < l["box"].x1
                 and owner_of.get(id(l)) in mine]
        # and anything in pure signing language between its first line and
        # its last is its own too, however far it sits from a line
        inner = [l for l in lines if l["signing"] and top_y - 2 <= l["box"].y0 <= foot_y + 2
                 and l["box"].x0 < across[1] and across[0] < l["box"].x1]
        # A line that ends a sentence is the tail of a paragraph, not a caption:
        # Appointment of Agent's "... by any one of the undersigned, to" /
        # "Libro Credit Union Limited." is all signing words, and was erased
        # with the block as the area's heading.
        # (ending a sentence and set right under a line of prose - PAC's
        #  "___ for Libro Credit Union." ends with a stop too, beside its line)
        prose = lambda l: (l["text"].rstrip().endswith(".") and any(
            not o["signing"] and 0 <= l["box"].y0 - o["box"].y1 <= 6
            and o["box"].x0 < l["box"].x1 and l["box"].x0 < o["box"].x1 for o in lines))
        heads = [l for l in heads if not prose(l)]
        tails = [l for l in tails if not prose(l)]
        inner = [l for l in inner if not prose(l)]
        own = {id(c): c for c in caps + heads + tails + inner}.values()
        box = fitz.Rect(min(r["x0"] for r in run), top_y - 2,
                        max(r["x1"] for r in run), max(r["y"] for r in run) + 2)
        for l in own:
            box |= l["box"]
        # a heading over the area that says who signs here
        over = [l for l in lines if 0 <= box.y0 - l["box"].y1 <= 80
                and SIGN_HEAD.search(l["text"])]
        # One member per row that has a line to sign on - a rebuilt row can
        # draw a signer's name as three short lines, one per field, and that is
        # still one signer - or one per signature caption where two sign side
        # by side on the same row.
        rows_of = []
        for r0 in sorted(run, key=lambda r: r["y"]):
            if rows_of and abs(rows_of[-1][0]["y"] - r0["y"]) <= 6:
                rows_of[-1].append(r0)
            else:
                rows_of.append([r0])
        members = 0
        for row in rows_of:
            if any(o["role"] == "signature" for o in row):
                named = {id(o["cap"]) for o in row
                         if o["role"] == "signature" and o["cap"] is not None
                         and "sign" in o["cap"]["text"].lower()}
                members += max(1, len(named))
        what = f"{len(run)} line(s) at y={box.y0:.0f}"
        # Lines under a member's own words of agreement are where they sign,
        # captioned or not: the Letters of Engagement put "I have read,
        # understood and accept the terms ..." over two bare lines and a Date.
        # (up to 120pt: Authorization to Release has its officers' "I/We, the
        #  undersigned, ... hereby authorize" two paragraphs over their lines)
        accepts = any(ACCEPTS.search(l["text"]) and 0 <= box.y0 - l["box"].y1 <= 120
                      for l in lines)
        # and a line a signature field sits on is somewhere to sign, whatever
        # is printed round it: Authorization to Release's officers' lines had
        # only author's notes for captions, and those come off the page
        accepts = accepts or any(
            0 <= r["y"] - f.y1 + 6 <= 14 and f.x0 < r["x1"] and r["x0"] < f.x1
            for r in run for f in sig_fields)
        if not accepts and not any(SIGN_ROLE.search(t) for t in [l["text"] for l in own]):
            declined.append(f"{what}: no caption names anyone signing - a block of names")
            continue
        # Bare lines are somewhere to sign only if the area says so somewhere:
        # an agent's name and address lines have no caption at all.
        said = (accepts or any(r["cap"] is not None for r in run)
                or any(SIGN_ROLE.search(l["text"]) for l in heads))
        # A line with a label before it that is not signing language - "Current
        # Date", "Farm/Business Name" - makes the run a block of details to
        # fill in, whatever the lines under it say.
        filled = [l["text"] for r in run for l in lines
                  if abs(l["box"].y1 - 2 - r["y"]) <= 6
                  and 0 <= r["x0"] - l["box"].x1 <= 70 and not l["signing"]]
        if not said:
            declined.append(f"{what}: no caption says anyone signs here")
            continue
        # Lines that start where labelled detail lines just above them start
        # are the next rows of that column - "Name 1", "Name 2" under
        # "Current Date", "Farm/Business Name" - not a place to sign.
        labelled = lambda o: (o not in run and o["cap"] is not None
                              and not o["cap"]["signing"] and before(o["cap"], o))
        column = {id(o): o for r in run for o in rules
                  if labelled(o) and abs(o["x0"] - r["x0"]) <= 3
                  and 0 < top_y - o["y"] <= 80}
        if len(column) >= 2:
            first = min(column.values(), key=lambda o: o["y"])
            declined.append(f"{what}: continues a column of details under "
                            f"{first['cap']['text'][:30]!r}")
            continue
        # A line on a row that also holds a labelled detail - the second
        # blank of "Dated at ___, Ontario, ___" - belongs to that row, not to
        # the signing under it. It leaves the area; the rest stays.
        # and so does a row with a line captioned for something other than
        # signing: "Contact Name | Date" over "Authorized Signature | Phone |
        # Fax Number" is the relinquishing institution's section, and "Name
        # and address of Financial Institution | Date Signed" is where the
        # cheque goes - neither is the member signing
        detail = lambda o: (o not in run and o["cap"] is not None
                            and not o["cap"]["signing"] and not before(o["cap"], o)
                            and (DETAILS.search(o["cap"]["text"])
                                 or not SIGNISH.search(o["cap"]["text"])))
        mates = [r for r in run if any((labelled(o) or detail(o)) and abs(o["y"] - r["y"]) <= 6
                                       for o in rules)]
        if mates:
            run = [r for r in run if r not in mates]
            if not run or not any(r["cap"] is not None for r in run):
                declined.append(f"{what}: its lines belong to rows of details")
                continue
            box = fitz.Rect(min(r["x0"] for r in run), min(r["y"] for r in run) - 2,
                            max(r["x1"] for r in run), max(r["y"] for r in run) + 2)
            # and the captions of the rows that left go with them: kept as
            # the area's own, FHSA Withdrawal's "Date Signed" was erased with
            # the block though its line stayed
            own = [l for l in own if any(-14 <= l["box"].y0 - r["y"] <= 30 for r in run)]
            for l in own:
                box |= l["box"]
            members = 0
            rows_of = []
            for r0 in sorted(run, key=lambda r: r["y"]):
                if rows_of and abs(rows_of[-1][0]["y"] - r0["y"]) <= 6:
                    rows_of[-1].append(r0)
                else:
                    rows_of.append([r0])
            for row in rows_of:
                if any(o["role"] == "signature" for o in row):
                    named = {id(o["cap"]) for o in row
                             if o["role"] == "signature" and o["cap"] is not None
                             and "sign" in o["cap"]["text"].lower()}
                    members += max(1, len(named))
        if filled:
            declined.append(f"{what}: lines labelled {filled[0][:30]!r} - details, not signing")
            continue
        if over:
            declined.append(f"{what}: under {over[-1]['text'][:40]!r} - not a member's area")
            continue
        # One line is enough where its caption says it is for signing - the
        # standard block it becomes has two member rows whatever the form
        # had. A bare line on its own is still too little to go on.
        captioned = any(r["cap"] is not None and "sign" in r["cap"]["text"].lower()
                        for r in run)
        if members == 0 or (len(run) < min_rules and not captioned):
            declined.append(f"{what}: {members} signature line(s) - too little to go on")
            continue
        pieces = [fitz.Rect(l["box"]) for l in own]
        # a heading the area takes goes with its underline: left behind, the
        # underline sat under nothing and was run out to the block's captions
        own_ids = {id(l) for l in own}
        pieces += [fitz.Rect(r["x0"] - 1, r["y"] - 1.5, r["x1"] + 1, r["y"] + 1.5)
                   for r, l in underlines_ if id(l) in own_ids]
        bits = [(f, t) for f, t in frags
                if box.x0 - 4 <= f.x0 and f.x1 <= box.x1 + 4
                and box.y0 - 14 <= f.y0 and f.y1 <= box.y1 + 4]
        pieces += [fitz.Rect(f) for f, _ in bits]
        for r in run:
            pieces.append(fitz.Rect(r["box"]) if r["typed"]
                          else fitz.Rect(r["x0"] - 1, r["y"] - 1.5, r["x1"] + 1, r["y"] + 1.5))
        found.append({"box": box, "members": members, "rules": run,
                      "pieces": pieces, "text": [l["text"] for l in own],
                      "frag_text": [t for _, t in bits]})
    return found, declined


# The client's standard signing block has two member rows (2026-10-07: "two
# member lines and a staff line is what they want") - more where the form had
# more people signing, so joint owners 2 and 3 keep a row each.
STANDARD_MEMBERS = 2


def member_rows(area):
    return max(STANDARD_MEMBERS, area["members"])


def standard_signing(page, area, fonts, size, margin=36.0):
    """Replace one signing area with the client's standard, in place.

        Name                  Authorized Signature       Date        x members
                              Authorizing Staff Member   Date

    Everything inside the area goes - its captions, its lines, the orange
    SIGN flags, its fields - and the standard block is drawn from the top
    of where the area was. It takes the room the area had and any free
    paper below it; if even at its tightest it will not fit, nothing is
    touched and the reason comes back instead.

    Fields are carried over only where the area had them: a form that came
    with fillable signature lines gets fillable ones back, reusing the old
    names in order, and a flat form stays flat.

    Returns a record of every glyph, line and field removed and added, for
    the census, the line-art check and the report."""
    box = fitz.Rect(area["box"])
    p = printed(page)
    words = p.get_text("words")
    # free paper below the area, down to the next thing printed or the margin
    below = [w[1] for w in words if w[1] > box.y1 + 1
             and w[0] < page.rect.width - margin and w[2] > margin]
    draws = [fitz.Rect(d["rect"]).y0 for d in p.get_drawings()
             if fitz.Rect(d["rect"]).y0 > box.y1 + 1
             and not fitz.Rect(d["rect"]).intersects(box)]
    # a line's worth of air above whatever comes next: 4pt left the staff
    # row's caption sitting on the "Quota No. / Address" rule under it
    ground = min(below + draws + [page.rect.height - margin])
    floor = ground - max(4.0, 2 * size)
    # a staff row for each staff member who signs there: the estate forms
    # have two Libro witnesses, each with a signature field of their own
    staff_sigs = [w for w in page.widgets() if fitz.Rect(w.rect).intersects(box)
                  and STAFFISH.search(w.field_name or "")
                  and w.field_type == fitz.PDF_WIDGET_TYPE_SIGNATURE]
    staff_rows = max(1, len(staff_sigs))
    rows = member_rows(area) + staff_rows
    lead = size * 1.2                       # a caption line
    # a row has to hold the tallest field that will be moved onto it, with
    # the caption of the row above still clear of the field's top
    tallest = max([fitz.Rect(w.rect).height for w in page.widgets()
                   if fitz.Rect(w.rect).intersects(box)] + [0.0])
    least = max(size * 2.6, tallest + size * 1.5)
    pitch = max(least, min(size * 4.2, (floor - box.y0 - lead) / rows))
    need = pitch * rows + lead
    if box.y0 + need > floor and box.y0 + need <= ground - 4:
        floor = ground - 4                  # the air is a nicety; the block is not
    if box.y0 + need > floor:
        return None, (f"needs {need:.0f}pt for {member_rows(area)} member row(s) and "
                      f"the staff row, and has {floor - box.y0:.0f}pt")
    text_right = max([w[2] for w in words] + [box.x1])
    left = box.x0
    right = min(page.rect.width - margin, max(box.x1, text_right))
    # An area of one line on the right of the page - "Signature ____" set
    # beside a date - is too narrow for three columns: "Authorized Name"
    # ran into "Authorized Signature", and the staff caption off the page.
    # It takes the text column's width, leftwards as far as anything printed
    # beside it on the rows the block will cover.
    text_left = max(margin * 0.5, min([w[0] for w in words] + [box.x0]))
    if right - left < 330:
        band = (box.y0, box.y0 + need)
        beside = [w[2] for w in words if w[3] > band[0] and w[1] < band[1]
                  and w[2] <= box.x0 - 1]
        left = min(left, max([text_left] + [x + 10 for x in beside]))
    # The block is one shape on every form: the width of the text column,
    # from its left margin to the right one (the client, 2026-10-07: "should
    # be centred and consistent across forms"). Set from the area's own
    # bounds it came out shifted and squeezed wherever the form's signing
    # lines were. Only where that would cover something printed beside the
    # area does it keep to the area.
    def columns(left, right):
        wide = right - left
        gutter = wide * 0.04
        return [(left, left + wide * 0.36),
                (left + wide * 0.36 + gutter, left + wide * 0.74 + gutter),
                (left + wide * 0.74 + 2 * gutter, right)]
    narrow_cols = columns(left, right)
    full_left = max(margin, text_left)
    full_right = page.rect.width - full_left      # centred: the same margin each side
    cols = columns(full_left, full_right)
    left, right = full_left, full_right

    # --- what goes -----------------------------------------------------
    # Read the page as the census will: fresh from its bytes. A field's own
    # appearance - the orange "SIGN" tab on a signature line - is counted by
    # the census, but a page that has been read through `printed` stops
    # showing it. The fresh copy is read twice, with and without its fields,
    # and the difference is what the fields themselves draw.
    fresh = fitz.open("pdf", page.parent.tobytes())
    fp = fresh[page.number]

    def glyphs(pg):
        return [(chr(c[0]), c[2][1], fitz.Rect(c[3]))
                for run in pg.get_texttrace() if run.get("type") == 0
                for c in run.get("chars", []) if c[0] > 0 and not chr(c[0]).isspace()]

    every = glyphs(fp)
    fresh.xref_set_key(fp.xref, "Annots", "null")
    plain = glyphs(fresh.reload_page(fp))
    # Only the area's own pieces go: its captions, its typed underscores, a
    # thin strip round each of its drawn lines. Anything else that happens to
    # sit inside its bounds - a footer the last caption shares a band with -
    # is protected, and is never declared gone. Declaring the whole box once
    # erased a footer and told the census it was meant.
    own = [p + (-0.5, -0.5, 0.5, 0.5) for p in area["pieces"]]
    mine_ = lambda r: any(p.contains(fitz.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2))
                          for p in own)
    area_box = box + (-1, -1, 1, 1)
    gone_glyphs = [(base, ch) for ch, base, r in plain if mine_(r)]
    staying = [r for ch, base, r in plain if not mine_(r)
               and any(r.intersects(p) for p in own)]
    # the standard block is drawn from the top of the area down; it may not
    # land on anything that is staying
    block_top = box.y0
    block = fitz.Rect(left, block_top, right, block_top + need)
    blocked = [r for ch, base, r in plain if not mine_(r) and r.intersects(block)]
    # Something printed beside the area's first line - FHSA Withdrawal's
    # "Cheque Number ___" on the row its signing lines start - is cleared by
    # starting the block under it, at full width, where there is the room;
    # kept to the area instead, the block came out shifted right and narrow.
    if blocked and max(r.y1 for r in blocked) <= block_top + size * 2.2:
        block_top_ = max(r.y1 for r in blocked) + size * 0.4
        if block_top_ + need <= ground - 4:
            block_ = fitz.Rect(left, block_top_, right, block_top_ + need)
            if not [r for ch, base, r in plain if not mine_(r) and r.intersects(block_)]:
                block_top, block, blocked = block_top_, block_, []
    if blocked:
        cols = narrow_cols
        left, right = cols[0][0], cols[2][1]
        block = fitz.Rect(left, block_top, right, block_top + need)
        blocked = [r for ch, base, r in plain if not mine_(r) and r.intersects(block)]
    if blocked:
        near = min(blocked, key=lambda r: r.y0)
        return None, (f"would be drawn over printing that is not part of it, "
                      f"near y={near.y0:.0f}")
    printed_here = Counter((round(base, 1), ch) for ch, base, r in plain)

    # the fields in the area stay the same fields - their names are how the
    # client's system fills them - and are moved, not made again
    held = sorted((w for w in page.widgets() if fitz.Rect(w.rect).intersects(box)),
                  key=lambda w: (round(w.rect.y0), w.rect.x0))
    # Fields travel as a signer's group: a signer's name can be three fields
    # on one line - first name, initial, surname - and they belong in one
    # cell together. A group forms round each signature field; where the
    # area has none, each row of fields is one signer.
    SIG = fitz.PDF_WIDGET_TYPE_SIGNATURE
    # A witness's fields, or a staff member's, are the staff row's - never a
    # member's. Their names say so: "[Libro_witness1.fullname1]".
    staffish = STAFFISH
    staff_held = [w for w in held if staffish.search(w.field_name or "")]
    held = [w for w in held if w not in staff_held]
    anchors = [w for w in held if w.field_type == SIG]
    groups = [[w] for w in anchors]
    if anchors:
        centre = lambda w: fitz.Point((w.rect.x0 + w.rect.x1) / 2, (w.rect.y0 + w.rect.y1) / 2)
        for w in held:
            if w.field_type == SIG:
                continue
            near = min(range(len(anchors)),
                       key=lambda k: abs(centre(w) - centre(anchors[k])))
            groups[near].append(w)
    else:
        # One signer's fields sit close together - a date a line above the
        # name, the name's three parts side by side - and the next signer's
        # start well below. A gap of more than 24pt between one field and
        # the next, top to bottom, is the start of the next signer.
        mid = lambda w: (w.rect.y0 + w.rect.y1) / 2
        for w in sorted(held, key=mid):
            if groups and mid(w) - max(mid(v) for v in groups[-1]) <= 24:
                groups[-1].append(w)
            else:
                groups.append([w])
    # signers in reading order: top to bottom, and left to right for two
    # signing side by side, whose fields can sit a point apart vertically
    top = lambda g: min(w.rect.y0 for w in g)
    left = lambda g: min(w.rect.x0 for w in g)
    groups.sort(key=top)
    for i in range(1, len(groups)):
        k = i
        while k > 0 and abs(top(groups[k]) - top(groups[k - 1])) <= 10                 and left(groups[k]) < left(groups[k - 1]):
            groups[k - 1], groups[k] = groups[k], groups[k - 1]
            k -= 1

    def drawn_by(w):
        """What a field's own appearance puts on the page, glyph by glyph.

        Fields can overlap - a joint signature field starting inside the
        primary's - so a glyph inside both is credited to one only: the
        field whose left edge is nearest to its left, since a SIGN tab sits
        at the start of its own field. Counted for both, the joint's tab
        read as lost from the primary once the two were apart."""
        fields = [v for v in page.widgets()]
        r = fitz.Rect(w.rect) + (-1, -1, 1, 1)

        def home(g):
            mid = fitz.Point((g.x0 + g.x1) / 2, (g.y0 + g.y1) / 2)
            holding = [v for v in fields
                       if (fitz.Rect(v.rect) + (-1, -1, 1, 1)).contains(mid)]
            if not holding:
                return None
            return min(holding, key=lambda v: (abs(g.x0 - v.rect.x0), v.rect.width)).xref

        mine = Counter((round(base, 1), ch) for ch, base, g in every
                       if r.contains(fitz.Point((g.x0 + g.x1) / 2, (g.y0 + g.y1) / 2))
                       and home(g) == w.xref)
        mine.subtract(printed_here)
        return [(base, ch) for (base, ch), n in mine.items() for _ in range(max(n, 0))]

    # The small marks a form draws on a signature line - the orange SIGN tab
    # a few points over it - belong to that line and go with it.
    lines_ = [p for p in area["pieces"] if p.height <= 4]
    marks = [fitz.Rect(d["rect"]) for d in printed(page).get_drawings()
             if fitz.Rect(d["rect"]).width <= 14 and fitz.Rect(d["rect"]).height <= 4
             and any(l.x0 - 2 <= d["rect"].x0 <= l.x1
                     and 0 <= l.y0 - d["rect"].y1 <= 14 for l in lines_)]
    for m in marks:
        box |= m
    forget(page)
    # text first, in one pass
    for p in own:
        for piece in clip_out(p, staying):
            page.add_redact_annot(piece, fill=None, cross_out=False)
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                          graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                          text=fitz.PDF_REDACT_TEXT_REMOVE)
    # then each line and mark by a redaction of its own. Taken together, two
    # strips that overlap - a signature line the rebuild drew in two pieces -
    # left the second piece standing.
    for r in lines_ + marks:
        page.add_redact_annot(r + (-0.5, -0.5, 0.5, 0.5), fill=None, cross_out=False)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                              graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                              text=fitz.PDF_REDACT_TEXT_NONE)
    forget(page)

    # --- what comes ----------------------------------------------------
    alias, path, font = style_of(0, fonts)
    made_glyphs, made_rules, moved_glyphs, placed, unplaced = [], [], [], [], []
    shifted = []                # (field, what it drew before it moved)
    spare_sigs = []             # a second signature field with no cell of its own
    lost_marks = []             # a field's own glyph that did not survive the move
    shape = page.new_shape()

    def rule(x0, x1, y):
        shape.draw_line(fitz.Point(x0, y), fitz.Point(x1, y))
        shape.finish(color=(0, 0, 0), width=0.6)
        made_rules.append(art_shape("s", (0, 0, 0), x1 - x0, 0.0))

    made_text = []

    def caption(x, y, text):
        page.insert_text(fitz.Point(x, y), text, fontname=alias, fontfile=path,
                         fontsize=size, color=(0, 0, 0), overlay=True)
        made_glyphs.extend((y, ch) for ch in text if not ch.isspace())
        made_text.append(text)

    def seat(group, x0, x1, y):
        """Move these fields onto the line just drawn, side by side in the
        order and proportions they had, so a name in three fields stays a
        name in three fields."""
        if not group:
            return False
        group = sorted(group, key=lambda w: w.rect.x0)
        # A signature field is never resized - MuPDF draws its appearance
        # again without the SIGN tab if it is - so a cell takes one, at its
        # own size, and a second has no cell to go to.
        sigs_here = [w for w in group if w.field_type == SIG]
        if sigs_here:
            spare_sigs.extend(sigs_here[1:])
            group = [w for w in group if w.field_type != SIG or w is sigs_here[0]]
        total = sum(max(w.rect.width, 1.0) for w in group)
        at = x0
        for w in group:
            was = fitz.Rect(w.rect)
            share = (was.width if w.field_type == SIG
                     else (x1 - x0) * max(was.width, 1.0) / total)
            # A field keeps its own height. Made shorter, a signature field
            # has its appearance drawn again without its SIGN tab; the rows
            # are spaced for the tallest field instead (see `pitch`).
            high = max(was.height, size * 1.4)
            # keep the field where it stood relative to its own line: a field
            # that reached a few points under its line - and drew its SIGN tab
            # there - does the same on the new one, rather than dropping its
            # tab onto the caption underneath
            under = [r["y"] for r in area["rules"] if abs(r["y"] - was.y1) <= 12
                     and r["x0"] < was.x1 and was.x0 < r["x1"]]
            drop = min(max(was.y1 - min(under, key=lambda v: abs(v - was.y1)), -6), 6)                 if under else 0.0
            now = (fitz.Rect(at, y + drop - was.height, at + was.width, y + drop)
                   if w.field_type == SIG else
                   fitz.Rect(at, y + drop - high, at + share - (2 if len(group) > 1 else 0),
                             y + drop))
            at += share
            shifted.append((w, drawn_by(w)))
            if w.field_type == SIG:
                # moved as it stands: set_rect carries the existing appearance
                # along, where update() would draw it again without its tab
                w._annot.set_rect(now)
                w.rect = now
            else:
                w.rect = now
                w.update()
            placed.append(w.field_name)
        return True

    def sat_on(w):
        """The role of the line a field was sitting on, if it sat on one."""
        for r in area["rules"]:
            if (abs(r["y"] - w.rect.y1) <= 6 and w.rect.x0 < r["x1"]
                    and r["x0"] < w.rect.x1):
                return r["role"]
        return None

    def split(group):
        """A signer's fields, by the cell each belongs in: a signature field
        or a text field sitting on a signature line to Signature, a field
        named for a date to Date, a field named for a name - or anything
        else - to Name."""
        cells = [[], [], []]
        for w in group:
            called = (w.field_name or "").lower()
            if w.field_type == SIG:
                cells[1].append(w)
            elif "date" in called:
                cells[2].append(w)
            elif "name" in called:
                cells[0].append(w)
            elif sat_on(w) == "signature":
                cells[1].append(w)
            elif sat_on(w) == "date":
                cells[2].append(w)
            else:
                cells[0].append(w)
        return cells

    bare = 0
    y = block_top + pitch - size * 0.6
    # the form's own signers, in order, and whoever else signed there - a
    # witness's group, past the signers the area counted
    others = groups[area["members"]:]
    groups = groups[:area["members"]]
    for n in range(member_rows(area)):
        cells = split(groups.pop(0)) if groups else [[], [], []]
        # (the first column is "Name": the client's word, 2026-10-07)
        for (x0, x1), label, group in zip(cols, ("Name",
                                                "Authorized Signature", "Date"), cells):
            rule(x0, x1, y)
            caption(x0, y + size + 1, label)
            if (held or staff_held) and not seat(group, x0, x1, y):
                bare += 1
        y += pitch
    # a group left over belonged to someone who is not a member signing - a
    # witness's line - and the staff row is where that signing now happens
    # The staff rows: one per staff member signing, each with that member's
    # fields - grouped round their signature fields - and the first taking a
    # group left over from someone who was not a member signing. A staff
    # member's name field goes in with their signature: the row is
    # "Authorizing Staff Member | Date", and Est170's
    # "[Libro_witness1.fullname1.label1.name]" otherwise had no cell at all.
    staff_sigs = [w for w in staff_held if w.field_type == SIG]
    staff_groups = [[w] for w in staff_sigs] or [[]]
    for w in staff_held:
        if w.field_type == SIG and staff_sigs:
            continue
        k = (min(range(len(staff_sigs)),
                 key=lambda k: abs(fitz.Point((w.rect.x0 + w.rect.x1) / 2, (w.rect.y0 + w.rect.y1) / 2)
                                   - fitz.Point((staff_sigs[k].rect.x0 + staff_sigs[k].rect.x1) / 2,
                                                (staff_sigs[k].rect.y0 + staff_sigs[k].rect.y1) / 2)))
             if staff_sigs else 0)
        staff_groups[k].append(w)
    if others:
        staff_groups[0] += others.pop(0)
    staff = [[], [], []]
    for n, group_ in enumerate(staff_groups):
        if n:
            y += pitch
        cells_ = split(group_)
        # The staff member signs in the first column, under the members'
        # names, then a gap, then the date - the client's layout
        # (2026-10-07); their name field goes in with their signature.
        cells_[0], cells_[1] = cells_[1] + cells_[0], []
        for (x0, x1), label, group in zip((cols[0], cols[2]),
                                          ("Authorizing Staff Member", "Date"),
                                          (cells_[0], cells_[2])):
            rule(x0, x1, y)
            caption(x0, y + size + 1, label)
            if (held or staff_held) and not seat(group, x0, x1, y):
                bare += 1
    shape.commit()
    dropped = []        # (name, kind) of fields taken out: none, by the client's word
    unplaced = ([w.field_name for w in staff[0]] + [w.field_name for g in groups + others for w in g]
                + [w.field_name for w in spare_sigs])
    forget(page)
    # Where each moved field's own glyphs landed is measured, not predicted:
    # a field made shorter redraws its SIGN tab somewhere a straight shift
    # would not put it. The page is read fresh once more, and each field's
    # glyphs before and after are paired, character by character.
    if shifted:
        again = fitz.open("pdf", page.parent.tobytes())
        ap = again[page.number]
        every = glyphs(ap)
        again.xref_set_key(ap.xref, "Annots", "null")
        printed_here = Counter((round(base, 1), ch)
                               for ch, base, r in glyphs(again.reload_page(ap)))
        for w, was in shifted:
            now = drawn_by(w)
            for ch in {c for _, c in was} | {c for _, c in now}:
                old_b = sorted(b for b, c in was if c == ch)
                new_b = sorted(b for b, c in now if c == ch)
                for k in range(max(len(old_b), len(new_b))):
                    moved_glyphs.append((old_b[k] if k < len(old_b) else None,
                                         new_b[k] if k < len(new_b) else None, ch))
                    if k >= len(new_b):
                        lost_marks.append((w.field_name, ch))
    return {"box": box, "bottom": y + size + 2, "gone_glyphs": gone_glyphs,
            "made_glyphs": made_glyphs, "made_rules": made_rules,
            "moved_glyphs": moved_glyphs, "placed": placed, "unplaced": unplaced,
            "bare": bare, "members": area["members"],
            "was": area["text"] + area.get("frag_text", []),
            "made_text": made_text, "lost_marks": lost_marks, "dropped": dropped,
            # the art the area actually lost, for the line-art check: its own
            # lines and marks, not everything inside its bounds (a shaded
            # panel the block is drawn over stays, and is no shape gained)
            "gone_art": [r + (-1, -1, 1, 1) for r in lines_ + marks]}, None


DECLARATION = re.compile(r"declaration|affidavit|statutory", re.I)


def title_declares(page, first=False):
    """Does this page's title make it a declaration or an affidavit?

    Read from the title - the largest type at the top of the page - never
    from anywhere near the top: the TFSA application and both locking-in
    agreements mention a "declaration of trust" in their opening wording,
    and are not declarations. Where nothing at the top stands out by size,
    page 1 takes its bold or capitalised line as the title. A later page has
    a title of its own only under a letterhead; otherwise it says None, and
    belongs to the document before it."""
    p = printed(page)
    top = p.rect.height * 0.3
    spans = [(sp["size"], sp["text"], bool(sp["flags"] & 16 or "bold" in sp["font"].lower()),
              sp["bbox"][1] < top)
             for b in p.get_text("dict")["blocks"]
             for ln in b.get("lines", ()) for sp in ln["spans"] if sp["text"].strip()]
    heads = [x for x in spans if x[3]]
    if not heads:
        return None if not first else False
    big = max(size for size, _, _, _ in heads)
    weight = Counter()
    for size, t, _, _ in spans:
        weight[round(size, 1)] += len(t.strip())
    distinct = big - weight.most_common(1)[0][0] >= 1.0
    if not first and not (distinct and any(i["bbox"][3] < p.rect.height * 0.15
                                           for i in p.get_image_info())):
        # A later page starts a document of its own only under a letterhead:
        # a line of 12pt halfway down Est115's second page is emphasis, not
        # a title
        return None
    return any(DECLARATION.search(t) for size, t, bold, _ in heads
               if size >= big - 0.5 and (distinct or bold or t.upper() == t))


def is_declaration(doc):
    """Is this document a declaration or an affidavit? Its signing is witnessed
    by whoever the law says - a commissioner, any adult - and the client's own
    hand-cleaned Mortgagor's Declaration kept "Witness" exactly as it was.
    Decided by the first page's title (see title_declares)."""
    # or by what it has its signer swear: Est135's "Renunciation of Designated
    # Beneficiary" is a solemn declaration under the Evidence Act, witnessed
    if not doc.page_count:
        return False
    return bool(title_declares(doc[0], first=True)) or bool(
        re.search(r"solemn(?:ly)? declar", " ".join(p.get_text() for p in doc), re.I))


def declared_pages(doc):
    """Which pages belong to a declaration. A file can carry more than one
    document - Est107 is a statutory declaration and then a receipt for the
    box's contents, under its own letterhead - so each page follows the
    last title before it."""
    out, now = [], False
    for n, page in enumerate(doc):
        said = title_declares(page, first=(n == 0))
        if said is not None:
            now = said
        out.append(now)
    return out


# The relabel table's signing words: whoever signs becomes "Authorized
# Signature" / "Name", a witness becomes "Authorized Name" or "Authorizing
# Staff Member". On a declaration or affidavit none of that is Libro's to
# change - who declares and who may witness is set by law - and the client's
# own hand-cleaned Mortgagor's Declaration kept "Witness:" and "Mortgagor:"
# exactly as they were. A witness the form names as Libro's own is staff
# whatever the document, so that swap still stands.
SIGNING_SWAPS = re.compile(r"^(?:authori[sz]ed (?:name|signature)|authorizing staff member"
                           r"|name(?: \d)?)$", re.I)
LIBRO_WITNESS = re.compile(r"libro|credit union|representative", re.I)


def kept_on_declaration(old, new):
    """Does this relabel stay off a declaration? (the old label, its swap)"""
    return bool(SIGNING_SWAPS.match(new.strip().rstrip(":"))) and not LIBRO_WITNESS.search(old)


NO_ROOM_AREAS = []     # (page number, box, members) of areas the block did not fit
STAFFISH = re.compile(r"witness|staff", re.I)   # a staff member's field, by its name


def signing_excluded(name, path=None):
    """Is this form one that never has a signing block added? Listed in
    signing_forms.txt with a leading "-": the client's call (2026-10-07) was
    that information sheets and staff worksheets - the Wire Instructions, the
    T4 estate calculations - ask for no signature and get none."""
    path = path or SIGNING_FORMS
    stem = " ".join(os.path.splitext(os.path.basename(name))[0].split()).casefold()
    if not os.path.exists(path):
        return False
    with open(path, encoding="utf-8") as fh:
        for l in fh:
            l = l.split("#", 1)[0].strip()
            if l.startswith("-") and " ".join(
                    os.path.splitext(l[1:].strip())[0].split()).casefold() == stem:
                return True
    return False


# The heading of a form's terms: an added block goes before it, at the end of
# the part of the form that is filled in.
TERMS_HEAD = re.compile(r"^\s*terms(?:\s+(?:and|&)\s+conditions)?\b"
                        # and the staff's part: ABM POS Trace's "ADMIN OFFICE USE
                        # ONLY" box ends what the member fills in, and the block
                        # went after it, onto a page of its own
                        r"|\buse\s+only\b|\bfor\s+(?:office|credit union|branch|admin\w*)\s+use\b",
                        re.I)


def terms_heading(page):
    """The top of a "Terms and Conditions" heading on this page, or None. A
    heading is a short line of its own, below the top of the page - not the
    page's title."""
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            t = "".join(s["text"] for s in l["spans"]).strip()
            if TERMS_HEAD.search(t) and len(t) < 60 and l["bbox"][1] > page.rect.height * 0.2:
                return l["bbox"][1]
    return None


def asks_for_signature(doc):
    """Does the form ask anyone to sign, anywhere - a signature field, or the
    words? Only one that does not gets the block added. Read by its signing
    areas instead, forms whose signatures sit in a table - Est190's trustees,
    the Safe Deposit entry log - had a second block put on them."""
    if any(w.field_type == fitz.PDF_WIDGET_TYPE_SIGNATURE for p in doc for w in p.widgets()):
        return True
    return any(SIGN_ANYWHERE.search(p.get_text()) for p in doc)


# (not "signed": the Assignment letter mentions a policy "signed by the
#  above stated insured", which asks no one here to sign)
# ("by signing this Agreement" asks for a signature too: Ln Conversion
#  had its own area replaced and a second block added at its end)
SIGN_ANYWHERE = re.compile(r"signature|witness|(?<![a-z])sign(?:ing)?(?![a-z])", re.I)


def block_spot(doc):
    """Where a form that asks for no signature gets the standard block: on the
    page its terms start, before them, or else on its last page. Returns
    (page index, whether it goes before a terms heading)."""
    for i, p in enumerate(doc):
        if terms_heading(p) is not None:
            return i, True
    return doc.page_count - 1, False


def add_signing_block(page, fonts, size, terms=False, margin=36.0):
    """The client's standard block, added where a form that asks for no
    signature ends its fill-in part: after the last thing printed above its
    terms heading, or above its footer. The same drawing as a replaced area,
    from an empty area at that height; refused, with the reason, where it
    would not fit."""
    p = printed(page)
    words = p.get_text("words")
    stop = page.rect.height * 0.9                # the footer's band
    if terms:
        head = terms_heading(p)
        if head is not None:
            stop = min(stop, head - 1)
    body = [w for w in words if w[3] <= stop]
    draws = [fitz.Rect(d["rect"]) for d in p.get_drawings()
             if d["rect"].y1 <= stop and d["rect"].height < page.rect.height * 0.5]
    bottom = max([w[3] for w in body] + [r.y1 for r in draws] + [margin])
    left = max(margin * 0.5, min([w[0] for w in words] + [margin]))
    y = bottom + 2 * size
    area = {"box": fitz.Rect(left, y, page.rect.width - margin, y + 1), "members": 0,
            "rules": [], "pieces": [], "text": [], "frag_text": []}
    return standard_signing(page, area, fonts, size)


def standardize_signing(page, fonts, size, skip=False):
    """Every member signing area on a page, brought to the client's standard.

    Returns (done, notes): the records of what was replaced, and a line for
    the report about every area found, replaced or not."""
    if skip:
        return [], []
    areas, declined = signing_areas(page)
    done, notes = [], [("signing_area_left", d) for d in declined]
    # bottom first: replacing one never moves the paper under another
    for area in sorted(areas, key=lambda a: -a["box"].y0):
        rec, why = standard_signing(page, area, fonts, size)
        if rec is None:
            # for the rebuild, which can lay the page out again with the room
            NO_ROOM_AREAS.append((page.number, fitz.Rect(area["box"]), area["members"]))
            notes.append(("SIGNING_AREA_NO_ROOM",
                          f"area at y={area['box'].y0:.0f} ({area['members']} "
                          f"member(s)) was left as it is: it {why}"))
            continue
        done.append(rec)
        for field, what in sorted(set((f, "".join(sorted(c for g, c in rec["lost_marks"]
                                                           if g == f)))
                                       for f, _ in rec["lost_marks"])):
            notes.append(("SIGNING_FIELD_APPEARANCE",
                          f"field {field!r} lost {what!r} from its own appearance "
                          f"when it was moved"))
        for name in rec["unplaced"]:
            notes.append(("SIGNING_FIELD_UNPLACED",
                          f"field {name!r} was in the signing area at "
                          f"y={rec['box'].y0:.0f} and has no cell of its kind in "
                          f"the standard block; it was left where it was"))
        if rec["placed"] or rec["bare"]:
            notes.append(("signing_fields",
                          f"{len(rec['placed'])} field(s) moved to their cells "
                          f"({', '.join(rec['placed'][:4])}); {rec['bare']} cell(s) "
                          f"have no field"))
        # The text an area takes with it is its captions. A sentence going
        # with it - the end of a paragraph read as a caption - is a loss the
        # word check is told to expect, so it is said here instead.
        for t in rec["was"]:
            if t.rstrip().endswith(".") and len(t.split()) > 6:
                notes.append(("SIGNING_TOOK_SENTENCE",
                              f"area at y={rec['box'].y0:.0f} took {t[:60]!r} with it"))
        for name, kind in rec["dropped"]:
            notes.append(("signing_field_dropped",
                          f"field {name!r} ({kind}) has no cell in the standard "
                          f"block - a signer past the second, or a second field of "
                          f"one kind for one signer; taken out"))
        notes.append(("signing_area_standard",
                      f"area at y={rec['box'].y0:.0f} rebuilt as {max(STANDARD_MEMBERS, rec['members'])} "
                      f"member row(s) - Name | Authorized Signature | "
                      f"Date - and a staff row (the form had {rec['members']} "
                      f"signer(s)); it read "
                      + "; ".join(repr(t[:30]) for t in rec["was"][:4])))
    return done, notes


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


def in_drop(key, rects):
    """Whether a line, named by where its first glyph sits, is one of the
    lines being taken off the page."""
    point = fitz.Point(*key)
    return any(point in r for r in rects)


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


def centred_shape(bbox, centre, text_left, text_right, tol, min_indent):
    """Whether a line's geometry reads as centred: near the page centre, well
    in from the text margin, and short enough that the centring can be meant.

    The width test is what keeps ordinary body text out. A line that fills its
    column straddles the centre whatever its alignment, and on a form with a
    hanging indent its two margins can match to within a point by chance -
    re-centring such a line shifts it left into the checkbox beside it."""
    column = text_right - text_left
    return (abs((bbox.x0 + bbox.x1) / 2 - centre) < tol
            and bbox.x0 - text_left > min_indent
            and (column <= 0 or bbox.width <= CENTRE_MAX_WIDTH * column))


def in_left_block(line, lines, others=()):
    """Whether a line starts where a line just above or below it starts - the
    last item of a tick-box list, a row of a left-aligned column. Such a line
    is aligned on its left edge, whatever its centre happens to be.

    ABM POS Trace's "Other, please specify." is short and sits near the middle
    of the page, so it read as centred and was re-centred on the page, 12pt
    right of the list it closes. `others` are lines that do not count (a
    paragraph's own lines)."""
    b = line["bbox"]
    size = max(sp["size"] for sp in line["spans"])
    for other in lines:
        if other is line or any(other is o for o in others):
            continue
        o = other["bbox"]
        # two centred lines of about the same length start together too
        # (the LIF's "Agreement Under" / "for Transfers to a"), so the left
        # edges must agree and the centres must not
        if (abs(o.x0 - b.x0) <= 1.5
                and abs((o.x0 + o.x1) - (b.x0 + b.x1)) / 2 > 3.0
                and o.y0 - b.y1 <= 1.6 * size and b.y0 - o.y1 <= 1.6 * size):
            return True
    return False


def alone_on_row(line, lines):
    """Whether nothing else shares this line's baseline. A line in one column
    of a two-column block can straddle the page centre by accident, and
    re-centring it would drag it over its neighbour."""
    base = line["spans"][0]["origin"][1]
    return not any(other is not line and abs(other["spans"][0]["origin"][1] - base) <= 2
                   for other in lines)


def looks_right_aligned(bbox, text_left, text_right, starts=()):
    """A short line set flush against the right edge of the text - a page
    number, a date, 'rev. 05/12'. It keeps its right edge and grows to the
    left; growing right, it would run past the edge on its own.

    It must also start where no other line does: text in the right-hand
    column of a two-column page also ends at the right edge and begins well
    in from the left, but shares its left edge with the lines around it."""
    column = text_right - text_left
    if not (abs(bbox.x1 - text_right) <= 4 and bbox.width < 0.5 * column
            and bbox.x0 - text_left > 0.4 * column):
        return False
    return sum(1 for x in starts if abs(x - bbox.x0) <= 2) <= 1


def right_aligned_flags(lines, text_left, text_right, starts):
    """Which lines are set flush right, judged over the page rather than one
    line at a time.

    A title set over two lines is right-aligned as a block, and its long first
    line is far too wide to read as flush right on its own - so it was redrawn
    from its left edge while the short line under it kept the right edge they
    used to share, and the block came apart. A line joins the block when it
    ends where a flush-right line beside it ends, within a couple of points,
    and starts where no other line does, which is what keeps the right-hand
    column of a two-column page out."""
    flags = [looks_right_aligned(ln["bbox"], text_left, text_right, starts) for ln in lines]
    while True:
        added = False
        for i, ln in enumerate(lines):
            if flags[i] or sum(1 for x in starts if abs(x - ln["bbox"].x0) <= 2) > 1:
                continue
            for j, other in enumerate(lines):
                if not flags[j] or i == j or abs(ln["bbox"].x1 - other["bbox"].x1) > 2:
                    continue
                gap = 1.6 * max(sp["size"] for sp in ln["spans"] + other["spans"])
                if (ln["bbox"].y0 - other["bbox"].y1 <= gap
                        and other["bbox"].y0 - ln["bbox"].y1 <= gap):
                    flags[i] = added = True
                    break
        if not added:
            return flags


def ink_ends(page, lines):
    """Where each line's glyphs actually end. The line box can reach further,
    over trailing spaces, so a flush-right line is anchored on this.

    A glyph counts as this line's only if it sits on this line's baseline.
    Line boxes overlap where a form sets two labels side by side and the first
    one's box runs past where the second starts - and taking the second's
    glyphs for the first's put its ink end 19pt beyond its last letter, which
    in turn cancelled the stop that should have held it back."""
    chars = [c for run in read_once(page, "trace", page.get_texttrace) if run.get("type") in (0, 1)
             for c in run.get("chars", []) if c[0] > 0 and not chr(c[0]).isspace()]
    bases = [ln["spans"][0]["origin"][1] for ln in lines]
    ends = [None] * len(lines)
    for c in chars:
        # each glyph belongs to the nearest baseline of the lines whose box
        # holds it - side-by-side labels can sit less than a point apart
        owner = min((i for i, ln in enumerate(lines) if glyph_in(c[3], ln["bbox"])),
                    key=lambda i: abs(bases[i] - c[2][1]), default=None)
        if owner is not None:
            ends[owner] = max(ends[owner] or 0.0, c[3][2])
    return [e if e is not None else ln["bbox"].x1 for e, ln in zip(ends, lines)]


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


def span_overrun(spans, fonts, size, gap_tol):
    """How far the text before a tab-like gap grows into the span after it.

    A line can hold two things set well apart - "Payment is made to incorrect
    account number." and, a tab away, "Correct account:". The span after the
    gap keeps its own place, so when the text before it is set wider the two
    run together. The room a line has is measured to whatever follows the
    whole line, which says nothing about this, so it's measured here."""
    worst = 0.0
    prev_old = prev_new = None
    line_max = max((sp["size"] for sp in spans), default=size)
    for sp in spans:
        font = style_of(sp["flags"], fonts)[2]
        at = span_size(sp, line_max, size)
        x = sp["origin"][0]
        if prev_old is not None and sp["bbox"][0] - prev_old < gap_tol:
            x = prev_new                                  # carried along
        elif prev_new is not None and sp["text"].strip():
            worst = max(worst, prev_new - (sp["bbox"][0] - 1.0))
        prev_old, prev_new = sp["bbox"][2], x + font.text_length(sp["text"], at)
    return worst


# A centred line covers at most this much of the text column.
CENTRE_MAX_WIDTH = 0.6


def clip_out(rect, blockers, gap=0.5):
    """What's left of rect once the blockers are cut out of it, as up to four
    pieces each. The erase works on span boxes, and those overlap their
    neighbours (trailing spaces run past the next span's first glyph) and the
    lines above and below. Cutting only the blocker's own area keeps a glyph
    that must stay while still erasing everything around it - cutting its
    whole column would spare the line above's glyphs too, and they'd be
    redrawn on top of themselves."""
    parts = [rect]
    for b in blockers:
        b = fitz.Rect(b.x0 - gap, b.y0 - gap, b.x1 + gap, b.y1 + gap)
        out = []
        for r in parts:
            if not r.intersects(b):
                out.append(r)
                continue
            if r.y0 < b.y0:
                out.append(fitz.Rect(r.x0, r.y0, r.x1, b.y0))
            if r.y1 > b.y1:
                out.append(fitz.Rect(r.x0, b.y1, r.x1, r.y1))
            top, bottom = max(r.y0, b.y0), min(r.y1, b.y1)
            if r.x0 < b.x0:
                out.append(fitz.Rect(r.x0, top, b.x0, bottom))
            if r.x1 > b.x1:
                out.append(fitz.Rect(b.x1, top, r.x1, bottom))
        parts = [p for p in out if p.width > 0.2 and p.height > 0.2]
    return parts


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
        # Only the last run of them: a line that is nothing but two typed
        # rules set apart by a tab - Mortgagor's witness and signature lines -
        # lost the first one, and the census was told to expect it gone
        if n and out and out[-1]["text"][-1:].isspace():
            break
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
    for w in read_once(page, "words", lambda: page.get_text("words")):
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
    text_right = max(ln["bbox"].x1 for ln in lines)
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
            "centred": all(centred_shape(ln["bbox"], centre, text_left, text_right,
                                         args.centre_tol, args.min_indent)
                            and not in_left_block(ln, lines, pl) for ln in pl),
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
    for d in read_once(page, "draw", page.get_drawings):
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
    obs += [(fitz.Rect(i["bbox"]), "an image", True) for i in read_once(page, "images", page.get_image_info)]
    # A tick box is a field, so it is not in the page's drawings - but it
    # prints, and a line grown into it is hidden under it: "...is attached"
    # on the POA transfer came out as "...is attac" with the box on top.
    obs += [(r, "a tick box", False) for _, r in tick_boxes(page)]
    known = [ln["bbox"] for ln in lines]
    for run in read_once(page, "trace", page.get_texttrace):
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


def dominant_size(lines):
    """The size most of a page's text is set in, weighted by how much text is
    at each size. Captions and footers sit below it; headings above."""
    weight = Counter()
    for ln in lines:
        for sp in ln["spans"]:
            weight[round(sp["size"], 1)] += len(sp["text"].strip())
    return weight.most_common(1)[0][0] if weight else 0.0


def line_room(page, lines, static, right, args):
    """How far each line may run at any size. A line stops before the next
    thing on its row - text in another column, a box edge, a checkbox - or
    at `right` if nothing follows it. A blank after a label (an underscore
    rule) isn't a stop: the label may push into it, shortening it from the
    left, so each line also gets that blank. Form fields are ignored, apart
    from tick boxes and radio buttons, which print as boxes."""
    blanks = fill_ins(page, lines)
    trace = [(c[0], c[2], c[3]) for run in read_once(page, "trace", page.get_texttrace) if run.get("type") in (0, 1)
             for c in run.get("chars", [])]
    text_left = min(ln["bbox"].x0 for ln in lines)
    text_right = max(ln["bbox"].x1 for ln in lines)
    starts = [ln["bbox"].x0 for ln in lines]
    flush = right_aligned_flags(lines, text_left, text_right, starts)
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
        # A drawing the line's own glyphs already run through isn't beside the
        # line at all: a rule drawn a couple of points under the baseline
        # falls inside the row's band, and taking it as a stop would cut the
        # line off well short of where the form itself prints it. Text is a
        # different matter - two labels can sit a point apart and still be
        # what stops each other, so those stops always count.
        def follows(r):
            """On this row, and past where this line's glyphs stop. Measured
            against the ink, not the line box: the box runs on over trailing
            spaces, and a box edge standing in that space read as being behind
            the line rather than in front of it - so nothing held the line
            back and it was set straight through the edge."""
            return (r.y1 > base - 0.7 * size and r.y0 < base + 0.3 * size
                    and r.x0 > b.x0 + 0.5 * b.width and r.x1 > ends[i] + 1)

        stops = [o for o, _, _ in static if follows(o)
                 and not any(o.intersects(r) for r in in_blank)
                 and o.x0 >= ends[i] - 0.5]
        beside = [other["bbox"] for j, other in enumerate(lines)
                  if j != i and follows(other["bbox"])]
        stops += beside
        stop = min([right] + [o.x0 for o in stops])
        # A shaded bar or box the line sits inside is its background, and the
        # line has to stay in it: a heading set in a grey bar that grows past
        # the end of the bar reads as a mistake. Only with --shrink-stubborn,
        # where the heading can take a smaller size of its own - held against
        # the whole document, this bought a tidier grey bar on one form at the
        # cost of a size step on four others.
        around = [o for o, _, contains in static
                  if getattr(args, "shrink_stubborn", False)
                  and contains and o.x0 <= b.x0 + 1 and o.x1 > b.x0
                  and o.y0 - 2 <= base <= o.y1 + 2]
        if around:
            stop = min(stop, min(o.x1 for o in around))
        # Clearance: as much as the line already had, up to 3pt. Where the
        # next thing along is text, keep a little even if the original left
        # none, or the two run together as one word.
        least = 1.5 if any(abs(o.x0 - stop) < 0.01 for o in beside) else 0.0
        hard = stop - max(least, min(3.0, stop - b.x1)) if stop < right else right
        # Whatever the stops say, a line may keep the room it already used:
        # where a form overlaps its own labels, that's the form's doing and no
        # reason to drive the whole document smaller.
        hard = max(hard, ends[i])
        blank = None
        if mine:
            bx0, bx1 = min(r.x0 for r in in_blank), max(r.x1 for r in in_blank)
            gap = bx0 - label_end               # as the form had it, unless it overlapped
            blank = {"x0": bx0, "x1": bx1, "reach": max(bx1, hard), "parts": mine,
                     "gap": min(4.0, gap) if gap >= 0 else 1.5}
        centred = (centred_shape(b, centre, text_left, text_right,
                                 args.centre_tol, args.min_indent)
                   and alone_on_row(ln, lines)
                   and not in_left_block(ln, lines))
        out.append({"hard": hard, "blank": blank, "left_stop": left_stop, "ink_end": ends[i],
                    "label": label if inline else ln["spans"], "centred": centred,
                    "right": not centred and not inline and flush[i]})
    return out


def plan_reflow(doc, fonts, args, fine=None, dys=None):
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
    for pi, page in enumerate(doc):
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
                      "body": dominant_size(lines), "page": page, "pushes": [],
                      "floor": ext.y1 if ext else page.rect.height,
                      "paras": find_paragraphs(page, lines, right, args),
                      "room": line_room(page, lines, static, right, args),
                      # Normally a re-wrapped paragraph may not reach lower
                      # than the page's content already does, so --header's
                      # room check still holds. --restructure lets it use the
                      # foot of the page as well, less whatever the header
                      # pushes the page down by: a form that ends 30pt above
                      # its own bottom margin has a line's worth of room
                      # there, and that is often the whole difference.
                      "cap": (page.rect.height - args.min_bottom_margin
                              - (dys or {}).get(pi, 0.0) if getattr(args, "restructure", False) else
                              min(ext.y1 if ext else page.rect.height,
                                  page.rect.height - args.min_bottom_margin)),
                      "centre": page.rect.width / 2})

    def preview(line):
        return "".join(s["text"] for s in line["spans"]).strip()[:50]

    def attempt(size):
        plans = [{"reflow": {}, "rules": [], "cuts": [], "squeeze": {}, "pushes": []}
                 for _ in pages]
        for pg in pages:
            if pg is not None:
                pg["pushes"] = []
        blockers = []
        for pi, pg in enumerate(pages):
            if pg is None:
                continue
            lines, limit = pg["lines"], pg["limit"]
            small = fine[pi]
            widths = [line_width(room["label"], fonts, small.get(line_key(ln), size))
                      for ln, room in zip(lines, pg["room"])]
            over, why = set(), {}
            multi = {k for p in pg["paras"] if len(p["lines"]) > 1 for k in p["lines"]}

            def stubborn(k):
                """A line nothing can be done for by setting the document
                smaller as a whole: it stands on its own, in a slot of its
                own, and --shrink-stubborn lets it take a smaller size while
                the body keeps its own. A form's table-cell labels and its
                shaded heading bars are what this is for."""
                return getattr(args, "shrink_stubborn", False) and k not in multi

            def caption(k):
                """A label set smaller than this page's body text, sitting in
                a slot of its own. Setting the whole document smaller does
                nothing for it - it keeps whatever size fits that slot
                (fit_fine_print settles that) - so once re-wrapping has been
                tried and failed, it mustn't hold the rest of the document
                down. Measured against the page's own body size, never
                against the size being tried: that would call every body line
                a caption as soon as a larger size was tried, and each one
                would then be shrunk on its own."""
                r = pg["room"][k]
                return (max(sp["size"] for sp in lines[k]["spans"]) < pg["body"] - 0.05
                        and r["blank"] is None and not r["right"] and not r["centred"])
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
                # text before a tab-like gap growing into what sits after it
                spill = span_overrun(room["label"], fonts, size, args.gap_tolerance)
                blank = room["blank"]
                if blank is None:
                    if spill > 0.01 and end <= room["hard"] + 0.01:
                        start = ln["bbox"].x0
                        room_for = (end - start) - spill
                        need = room_for / (end - start) if end > start else 0.0
                        if getattr(args, "condense", 1.0) < 1.0 and need >= getattr(args, "condense", 1.0):
                            plans[pi]["squeeze"][line_key(ln)] = round(need, 4)
                        else:
                            over.add(i)
                        continue
                    if end > room["hard"] + 0.01:
                        # Set the line a little narrower rather than let it
                        # hold the document's size down. Squeezing the glyphs
                        # horizontally leaves their height alone, and height
                        # is what the eye reads a size change by: at 96% this
                        # is invisible where dropping half a point is not.
                        start = ln["bbox"].x0
                        # a point in hand: the measured end and the ink the
                        # drawing actually lays down agree to about that, and
                        # squeezing to the very limit put a bracket 1.3pt into
                        # the rule beside it
                        need = ((room["hard"] - 1.0 - start) / (end - start)
                                if end > start else 0.0)
                        if (getattr(args, "condense", 1.0) < 1.0 and need >= getattr(args, "condense", 1.0)
                                and not room["centred"] and not room["right"]):
                            plans[pi]["squeeze"][line_key(ln)] = round(need, 4)
                        else:
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
                if (i in why or i not in owner) and not (caption(i) or stubborn(i)):
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
                        not sp.get("unnamed") and drawable(sp["text"], style_of(sp["flags"], fonts)[2])
                        for sp in para["spans"]):
                    para["bad"] = "it holds characters Arial can't draw, which have to stay put"
                if para["bad"]:
                    if not all(caption(k) or stubborn(k) for k in para["lines"]):
                        blockers.append((pi + 1, para["preview"], para["bad"]))
                    continue
                # other words keep a space's clearance: met edge to edge,
                # "required" and the "No" a tab stop after it read as one word
                obst = pg["static"] + [(r + (-0.3 * size, 0, 0, 0), f"the line {t!r}", False)
                                       for j, r, t in text_obs if j not in para["lines"]]
                layout, why = layout_paragraph(para, size, fonts, limit, obst, pg["cap"],
                                               args.min_leading, pg["centre"])
                if layout is None and getattr(args, "condense", 1.0) < 1.0 and not para["centred"]:
                    # It needs a line more than it has room for. Setting the
                    # paragraph narrower lets each line hold more, so the text
                    # keeps the line count it had - which is the only thing
                    # the space below it will take. The wrap is worked out at
                    # full width against room stretched by the same amount,
                    # then drawn squeezed back into the real room.
                    left = para["bbox"].x0
                    for scale in (0.98, 0.96, getattr(args, "condense", 1.0)):
                        if scale < getattr(args, "condense", 1.0) - 1e-9:
                            break
                        wide = [(fitz.Rect(left + (o.x0 - left) / scale, o.y0,
                                           left + (o.x1 - left) / scale, o.y1), what, keep)
                                for o, what, keep in obst]
                        # the same point in hand the single-line squeeze
                        # keeps: measured and drawn ends agree to about that
                        got, _ = layout_paragraph(para, size, fonts,
                                                  left + (limit - 1.0 - left) / scale, wide,
                                                  pg["cap"], args.min_leading, pg["centre"])
                        if got is not None:
                            got["squeeze"], got["pivot"] = scale, left
                            layout, why = got, None
                            break
                if (layout is None and getattr(args, "rebuild", False)
                        and "runs into" in (why or "")):
                    # Nothing about this paragraph fits because there's no
                    # room under it. Ask for the room: everything below the
                    # paragraph goes down by a line, if the page still has
                    # that much space at its foot and its furniture can be
                    # moved at all. A page can be cut in more than one place -
                    # each cut moves what's below it, so they add up.
                    pitch = round(para["pitch"] or 1.2 * size, 1)
                    line = round(para["bbox"].y1 + 1, 1)
                    taken = sum(d for _, d in pg["pushes"])
                    room = pg["cap"] - pg["floor"] - taken
                    # A paragraph may want more than one line's worth: ask for
                    # as many as the foot of the page can still give.
                    for lines_wanted in (1, 2, 3):
                        step = round(pitch * lines_wanted, 1)
                        if step > room + 0.01 or not can_push(pg["page"], line):
                            break
                        cuts = pg["pushes"] + [(line, step)]
                        shifted = [(r + (0, drop_at(cuts, r.y0), 0, drop_at(cuts, r.y0)),
                                    what, keep) for r, what, keep in obst]
                        got, why2 = layout_paragraph(para, size, fonts, limit, shifted,
                                                     pg["cap"], args.min_leading,
                                                     pg["centre"])
                        if got is not None:
                            pg["pushes"] = cuts
                            plans[pi]["pushes"] = list(cuts)
                            layout, why = got, None
                            break
                if layout is None:
                    if not all(caption(k) or stubborn(k) for k in para["lines"]):
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
        for layout in {id(v): v for v in plan.get("reflow", {}).values()}.values():
            if layout.get("squeeze"):
                rows.append((pi + 1, "paragraph_condensed",
                             f"{layout['para']['preview']!r}: set at "
                             f"{layout['squeeze'] * 100:.0f}% width so it keeps "
                             f"{len(layout['para']['lines'])} line(s)"))
    for pi, plan in enumerate(plans):
        for key, at in sorted(plan.get("squeeze", {}).items()):
            line = next((ln for ln in pages[pi]["lines"] if line_key(ln) == key), None)
            text = "".join(sp["text"] for sp in line["spans"]).strip() if line else ""
            rows.append((pi + 1, "line_condensed",
                         f"{text[:50]!r}: set at {at * 100:.0f}% width to fit its room"))
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
# Relabelling: whole labels swapped for the house wording
# --------------------------------------------------------------------------

RELABEL_TABLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "relabel.txt")
ISSUERS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "third_parties.txt")
SIGNING_FORMS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signing_forms.txt")


TITLES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "titles.txt")


def house_title(name, path=TITLES):
    """The title a form's header shows: its file name, or what titles.txt
    says instead. "Authorization to Release Financial Info Edited" printed
    "Edited" in the header, and a typo in a file name ("Successr") printed
    the typo."""
    stem = os.path.splitext(os.path.basename(name))[0]
    key = " ".join(stem.split()).casefold()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("#") or "=>" not in line:
                    continue
                old, new = (part.strip() for part in line.split("=>", 1))
                if " ".join(old.split()).casefold() == key and new:
                    return new.upper()
    # in capitals: the client's title style (2026-10-08) is bold, upper
    # case, 14pt, set flush right
    return " ".join(stem.split()).upper()


def signing_listed(name, path=SIGNING_FORMS):
    """Is this form one whose signing area gets the client's standard block?

    The forms listed in signing_forms.txt, or every form where it has a line
    "*" - the client's word on 2026-10-07 was that the standard goes on all
    of them. (It was first only the forms they had named.)"""
    stem = " ".join(os.path.splitext(os.path.basename(name))[0].split()).casefold()
    if not os.path.exists(path):
        return False
    with open(path, encoding="utf-8") as fh:
        wanted = {" ".join(os.path.splitext(l.split("#", 1)[0].strip())[0].split()).casefold()
                  for l in fh}
    return "*" in wanted or stem in wanted - {""}
HOUSE_MARKS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "assets", "libro_marks.json")
# Lines worth a person's eye when the table leaves them alone: a witness or
# staff reference, or "Owner" - the client's word is now "Member", and where it
# is a defined term in an agreement's wording only a person should change it.
REVIEW_WORDS = re.compile(r"\bwitness|\bstaff\b|\bowners?\b", re.I)


def label_key(text):
    """A label as the table matches it: case, curly apostrophes, spacing and a
    trailing colon don't count."""
    t = " ".join(text.replace("’", "'").split()).casefold()
    return t[:-1].rstrip() if t.endswith(":") else t


def load_issuers(path):
    """Issuer names whose forms aren't ours to re-brand, one per line."""
    out = []
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if line:
                    out.append(line)
    return out


def load_relabels(path):
    """The relabel table: {label_key: replacement}.

    A comment is a line that starts with "#". A "#" anywhere else is part of
    a label - "Owner #", "Branch/Owner#" - and cutting the line there, as this
    once did, dropped those entries without a word."""
    table = {}
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("#"):
                    continue
                if "#" in line and "=>" not in line:
                    print(f"relabel table: {line!r} has no '=>' and is not a comment - "
                          f"skipped", file=sys.stderr)
                if "=>" in line:
                    old, new = (part.strip() for part in line.split("=>", 1))
                    if old and new:
                        table[label_key(old)] = new
    return table


def fit_fine_print(doc, fonts, args, size, fine, plans):
    """Footers and fine print keep their own size, so the body size can't make
    room for them - but Arial is wider than what they were set in, and they
    can still run past the edge or into a rule. Each one that doesn't fit its
    room is stepped down (never below --fine-floor) until it does. Changes
    `fine` in place; returns report rows."""
    rows = []
    for pi, page in enumerate(doc):
        small = fine[pi]
        p = printed(page)
        lines, _ = collect_lines(p, False, args.target_font, args.size, args.size_tolerance)
        if not lines:
            continue
        right = args.right_margin or ink_right(p) or max(ln["bbox"].x1 for ln in lines)
        if args.page_margin is not None and not args.right_margin:
            right = max(right, page.rect.width - args.page_margin)
        room = line_room(p, lines, reflow_obstacles(p, lines), right, args)
        body = dominant_size(lines)
        for ln, rm in zip(lines, room):
            key = line_key(ln)
            own, fine_print = max(sp["size"] for sp in ln["spans"]), key in small
            text_of = "".join(sp["text"] for sp in ln["spans"]).strip()
            if text_of.count("_") > 0.6 * len(text_of):
                continue        # a fill-in rule: rebuilt to length, not shrunk
            if fine_print:
                was = small[key]
            elif (key not in plans[pi].get("reflow", {})
                  and (own < body - 0.05 or getattr(args, "shrink_stubborn", False))):
                # Set smaller than the body. Growing text is the dangerous
                # direction: on one form a 7.2pt caption taken to 8pt ran
                # straight into the label beside it. But a caption can grow
                # without its size changing, because Arial is wider than what
                # most forms were set in - and plan_reflow leaves every caption
                # to this pass (see its caption()), so all of them are measured
                # here: "...is attached" at 9.96pt, set at 10, ran under the
                # tick box after it and neither pass looked.
                was = size
            else:
                continue
            at = was
            while at > args.fine_floor:
                if rm["right"]:
                    # anchored on its right edge, so it grows into what's to
                    # its left: that's the end that has to clear
                    start = rm["ink_end"] - line_width(rm["label"], fonts, at)
                    if start >= rm["left_stop"] + 2:
                        break
                    at = round(at - 0.5, 2)
                    continue
                end = (page.rect.width / 2 + line_width(rm["label"], fonts, at) / 2
                       if rm["centred"] else
                       redrawn_end(rm["label"], fonts, at, args.gap_tolerance))
                stop = rm["hard"]
                if rm["blank"] is not None:
                    stop = min(stop, rm["blank"]["x0"] - rm["blank"]["gap"])
                if end is None or end <= stop + 0.01:
                    break
                at = round(at - 0.5, 2)
            if at < was:
                small[key] = at
                text = "".join(sp["text"] for sp in ln["spans"]).strip()
                what = "fine_print_shrunk" if fine_print else "line_kept_smaller"
                rows.append((pi + 1, what if at > args.fine_floor else "FINE_PRINT_NO_ROOM",
                             f"{text[:50]!r}: {was:g} -> {at:g}pt to fit its room"))

        # A row reads as one thing, so it's set at one size. Where a label in
        # a row had to come down, its neighbours on that row come down with
        # it - three tax bands beside each other, two of them shrunk and one
        # not, is the sort of thing a reviewer stops at.
        for ln, rm in zip(lines, room):
            key = line_key(ln)
            if key not in small:
                continue
            base = ln["spans"][0]["origin"][1]
            own = max(sp["size"] for sp in ln["spans"])
            if size - small[key] > 1.5:
                # Too big a drop to be a matter of matching: a line that had
                # to come down this far is its own small thing - a "Sub#"
                # tucked beside a checkbox - and taking its whole row with it
                # shrinks text that was perfectly happy.
                continue
            for other in lines:
                mate = line_key(other)
                if (mate != key and abs(other["spans"][0]["origin"][1] - base) <= 2
                        and abs(max(sp["size"] for sp in other["spans"]) - own) <= 0.1
                        and small.get(mate, size) > small[key]):
                    small[mate] = small[key]
                    rows.append((pi + 1, "line_kept_smaller",
                                 f"{''.join(sp['text'] for sp in other['spans']).strip()[:50]!r}: "
                                 f"{size:g} -> {small[key]:g}pt, to match its row"))
    return rows


def plan_relabels(doc, fonts, args, table, size, fine, plans):
    """Which labels to swap: per page {line_key: new text}, plus report rows.

    Deliberately narrow, because rewording is a human job wherever it can't
    be done cleanly. A line is relabelled only when its WHOLE text is a label
    in the table, it stands on its own rather than being part of a paragraph,
    it isn't being re-wrapped, and the new wording fits the room the line
    already has at the document's size - relabelling never shrinks the
    document or moves anything else. A word inside a sentence is never
    touched. Everything that didn't qualify is reported for a human, as is
    any line mentioning a witness or staff that the table doesn't cover.
    Reads the document, changes nothing. Each entry is {"text", "centre"},
    centre being the x to keep when the label was centred under its line."""
    out, rows = [], []
    declared_at = declared_pages(doc)
    for pi, page in enumerate(doc):
        declared = declared_at[pi]
        found = {}
        out.append(found)
        p = printed(page)
        lines, _ = collect_lines(p, False, args.target_font, args.size, args.size_tolerance)
        if not lines:
            continue
        right = args.right_margin or ink_right(p) or max(ln["bbox"].x1 for ln in lines)
        if args.page_margin is not None and not args.right_margin:
            right = max(right, page.rect.width - args.page_margin)
        room = line_room(p, lines, reflow_obstacles(p, lines), right, args)
        in_para = {k for para in find_paragraphs(p, lines, right, args)
                   if len(para["lines"]) > 1 for k in para["lines"]}
        reflowed = (plans[pi] or {}).get("reflow", {})
        rules = [r for r in rule_segments(p) if r.width > 3 * max(r.height, 1)] + [
            f["rect"] for f in fill_ins(p, lines)]
        for i, ln in enumerate(lines):
            text = " ".join("".join(s["text"] for s in ln["spans"]).split())
            # A label often has its own blank typed on the same line -
            # "Signature of Annuitant _______". The underscores are the form
            # drawing a rule, not part of the label, so match without them
            # and put them back after.
            bare = text.rstrip("_ ").rstrip()
            tail = text[len(bare):]
            new = table.get(label_key(bare))
            # a word on its own in lower case is a sentence wrapped onto a new
            # line, not a label: Q-Trade's "... the beneficial" / "owner:"
            # came out "the beneficial Member:"
            if new is not None and len(bare.split()) == 1 and bare[:1].islower():
                new = None
            if new is None:
                if REVIEW_WORDS.search(text):
                    where = "in running text" if len(text) > 42 else "a label the table doesn't cover"
                    rows.append((pi + 1, "relabel_review", f"{text[:70]!r} ({where})"))
                continue
            if declared and kept_on_declaration(bare, new):
                rows.append((pi + 1, "relabel_left_declaration",
                             f"{bare!r} left as the form has it - on a declaration, who "
                             f"signs and who witnesses is the law's to say"))
                continue
            if bare.endswith(":") and not new.endswith(":"):
                new += ":"
            # set in capitals if most of the label's words were
            # ("DIRECTOR'S (Producer) NAME" sits in a row of capitals)
            words = [w for w in bare.split() if any(c.isalpha() for c in w)]
            if words and sum(w.upper() == w for w in words) * 2 > len(words):
                new = new.upper()
            new += tail
            if new == text:
                continue
            # keep any leading space the label was set with, so it starts
            # where the original's first letter did
            first = ln["spans"][0]["text"]
            new = first[:len(first) - len(first.lstrip())] + new
            key, rm = line_key(ln), room[i]
            anchor = None
            if any(sp.get("unnamed") or not drawable(sp["text"], style_of(sp["flags"], fonts)[2])
                   for sp in ln["spans"]):
                why = "it holds glyphs that have to stay in their own font"
            elif i in in_para:
                why = "it's part of a paragraph"
            elif key in reflowed:
                why = "its line is being re-wrapped"
            else:
                b = ln["bbox"]
                mid = (b.x0 + b.x1) / 2
                # A caption centred under the signature line just above it
                # keeps that centre, or the new wording drifts off its line.
                under = [r for r in rules if b.y0 - 16 <= r.y1 <= b.y0 + 2
                         and r.x0 <= b.x0 + 1 and r.x1 >= b.x1 - 1
                         and abs((r.x0 + r.x1) / 2 - mid) <= 6]
                anchor = mid if under else None
                # A caption set to finish with the rule above it keeps
                # that right edge. The house wording is longer than what
                # it replaces, and grown rightwards it runs off the end
                # of the line it belongs to. A caption that STARTS with
                # its rule is left alone - it is left-aligned and meant
                # to grow to the right.
                ends_with = [r for r in rules
                             if b.y0 - 45 <= r.y1 <= b.y0 + 3
                             and abs(b.x1 - r.x1) <= 15
                             and b.x0 - r.x0 > 20]
                right_at = b.x1 if anchor is None and ends_with else None

                def room_for(try_text):
                    wide = line_width([dict(ln["spans"][0], text=try_text)],
                                      fonts, fine[pi].get(key, size))
                    if anchor is not None:
                        return (anchor - wide / 2 >= rm["left_stop"] + 2
                                and anchor + wide / 2 <= rm["hard"])
                    if rm["right"]:
                        return rm["ink_end"] - wide >= rm["left_stop"] + 2
                    if rm["centred"]:
                        centre = page.rect.width / 2
                        return (centre - wide / 2 >= rm["left_stop"]
                                and centre + wide / 2 <= rm["hard"])
                    stop = rm["hard"]
                    if rm["blank"] is not None:
                        stop = min(stop, rm["blank"]["x0"] - rm["blank"]["gap"])
                    return ln["spans"][0]["origin"][0] + wide <= stop + 0.01

                # Measure the label, not the blank typed after it. The rule
                # is already handled as a blank in its own right, and its
                # room is measured from where it starts - so including the
                # underscores here compares the label against a stop that
                # sits in the middle of the label itself, and every label
                # with its own rule is refused however short the new wording.
                measure = new[:len(new) - len(tail)] if tail else new
                if right_at is not None:
                    wide = line_width([dict(ln["spans"][0], text=measure)],
                                      fonts, fine[pi].get(key, size))
                    fits = right_at - wide >= rm["left_stop"] + 2
                else:
                    fits = room_for(measure)
                pushed = None
                blank = rm["blank"]
                if (not fits and right_at is None and anchor is None and blank is not None
                        and not rm["right"] and not rm["centred"]):
                    # A label with its blank typed after it - "Folio# of Estate
                    # Trustee: ______" - can push the blank along, the way the
                    # restyle already does when Arial sets the label wider:
                    # the blank starts later and keeps at least the width any
                    # blank may shrink to. Only for a blank the restyle redraws
                    # apart from its label, so there is a planned rule to move.
                    planned = [r for r in (plans[pi] or {}).get("rules", [])
                               if r.get("inline") == key]
                    if planned:
                        wide = line_width([dict(ln["spans"][0], text=measure)],
                                          fonts, fine[pi].get(key, size))
                        start = ln["spans"][0]["origin"][0] + wide + blank["gap"]
                        width = blank["x1"] - blank["x0"]
                        # it starts later but never ends later: grown into
                        # the free room it ran up against "Telephone:"
                        stop = planned[0]["x1"]
                        if stop - start >= max(36.0, args.min_fill * width):
                            fits, pushed = True, (planned, start, stop)
                why = None if fits else f"{new!r} doesn't fit where the label sits"
                if pushed is not None:
                    planned, start, stop = pushed
                    for r in planned:
                        if r["x0"] < start:
                            r["x0"] = start
            if why:
                rows.append((pi + 1, "relabel_for_human", f"{text!r} -> {new.strip()!r}: {why}"))
            else:
                # the line's own box, so the proof can point at what changed
                found[key] = {"text": new, "centre": anchor, "was": text,
                              "right_at": right_at, "pushed": pushed is not None,
                              "box": fitz.Rect(ln["bbox"])}
                rows.append((pi + 1, "relabelled", f"{text!r} -> {new.strip()!r}"
                             + (" (kept centred under its line)" if anchor is not None else "")))
    return out, rows


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
    for run in read_once(page, "trace", page.get_texttrace):
        if run.get("type") not in (0, 1):                     # filled or stroked
            continue
        # Glyphs with ink only: a stray space has a bbox but draws nothing, and
        # Word leaves them at the very top of the page.
        ink = [fitz.Rect(c[3]) for c in run.get("chars", [])
               if c[0] > 0 and not chr(c[0]).isspace()]
        if ink:
            rects.append(fitz.Rect(min(r.x0 for r in ink), min(r.y0 for r in ink),
                                   max(r.x1 for r in ink), max(r.y1 for r in ink)))
    for d in read_once(page, "draw", page.get_drawings):
        if d["type"] == "f" and d.get("fill") and min(d["fill"]) >= 0.99:
            continue
        rects.append(fitz.Rect(d["rect"]))
    rects += [fitz.Rect(i["bbox"]) for i in read_once(page, "images", page.get_image_info)]
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
    top `zone` points and set at `min_title_size` or larger, plus any line the
    title runs onto below it. A logo is an image
    that *starts* within that band, no wider than half the page and no taller
    than a fifth of it - a letterhead logo often hangs below the band, and
    requiring it to fit entirely left the old logo on the page under the new
    one. This is a guess, so every removal is reported."""
    all_lines = []
    for block in read_once(page, "dict", lambda: page.get_text("dict"))["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            spans = [s for s in line["spans"] if s["text"].strip()]
            if spans:
                all_lines.append({"bbox": fitz.Rect(line["bbox"]), "spans": spans,
                                  "size": max(s["size"] for s in spans)})
    band = [ln for ln in all_lines
            if ln["bbox"].y0 <= zone and ln["size"] >= min_title_size]
    if not band:
        return [], [fitz.Rect(i["bbox"]) for i in read_once(page, "images", page.get_image_info)
                    if i["bbox"][1] <= zone
                    and fitz.Rect(i["bbox"]).width <= page.rect.width / 2
                    and fitz.Rect(i["bbox"]).height <= page.rect.height / 5]
    # Only the largest text in the band is the title. A form can put its own
    # labels up there - "Collection Item Number:", "Date:" - and on one form
    # they were taken for the title and removed with it, which the census
    # can't catch, because it's told the title is meant to go.
    biggest = max(ln["size"] for ln in band)
    band = [ln for ln in band if ln["size"] >= biggest - 0.6]
    head = min(band, key=lambda ln: (ln["bbox"].y0, ln["bbox"].x0))

    def lines_up(ln, t):
        """Whether a line is set the way the title is - sharing its centre,
        its left edge or its right edge. Where a form sets its title at the
        body size, this is all that separates the two: on one declaration the
        title is centred and 'TO:', 'RE:' and the address beneath it are not,
        and without this test they went with it."""
        # 5pt, not 2: a line box runs past its last glyph over any trailing
        # space, and one title line missed its own block by 0.8pt that way.
        return (abs((ln["bbox"].x0 + ln["bbox"].x1) / 2
                    - (t["bbox"].x0 + t["bbox"].x1) / 2) <= 5
                or abs(ln["bbox"].x0 - t["bbox"].x0) <= 5
                or abs(ln["bbox"].x1 - t["bbox"].x1) <= 5)

    titles = [ln for ln in band if ln is head or lines_up(ln, head)]
    # A title set over several lines can hang below the band. Take all of it,
    # or the leftover lines stay on the page under the new header.
    taken = {id(ln) for ln in titles}
    while True:
        more = [ln for ln in all_lines if id(ln) not in taken
                and any(abs(ln["size"] - t["size"]) <= 0.6
                        and 0 <= ln["bbox"].y0 - t["bbox"].y1 <= 1.6 * t["size"]
                        and lines_up(ln, t)
                        for t in titles)]
        if not more:
            break
        titles += more
        taken |= {id(ln) for ln in more}
    logos = [fitz.Rect(i["bbox"]) for i in read_once(page, "images", page.get_image_info)
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


def logo_print(page, rect, n=32):
    """A small grey thumbnail of what's drawn in `rect`, trimmed to its ink
    and padded square, with the shape's own width-to-height ratio. Comparing
    the printed result rather than the stored image keeps this steady across
    scaling, colour and how the file happens to store the picture."""
    try:
        from PIL import Image
    except ImportError:
        return None
    pm = page.get_pixmap(dpi=150, clip=rect)
    im = Image.frombytes("RGB", (pm.width, pm.height), pm.samples).convert("L")
    box = im.point(lambda p: 255 if p < 200 else 0).getbbox()
    if not box:
        return None
    im = im.crop(box)
    side = max(im.size)
    pad = Image.new("L", (side, side), 255)
    pad.paste(im, ((side - im.size[0]) // 2, (side - im.size[1]) // 2))
    px = list(pad.resize((n, n), Image.LANCZOS).getdata())
    lo, hi = min(px), max(px)
    if hi - lo < 8:
        return None
    return [(p - lo) * 255 // (hi - lo) for p in px], im.size[0] / im.size[1]


def house_marks(path):
    """The house logo's fingerprints, as written by --learn-marks."""
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return []


def letterhead_marks(page, zone):
    """Every mark printed in the top band of the page."""
    out = []
    for info in read_once(page, "images", page.get_image_info):
        r = fitz.Rect(info["bbox"])
        if r.y0 <= zone and 20 < r.width <= page.rect.width * 0.6:
            got = logo_print(page, r)
            if got:
                out.append(got)
    return out


def third_party(page, zone, marks, issuers):
    """Whose form this is. Returns (not ours, why).

    A form put out by someone else - a marketing board, the CRA, a payments
    network - isn't Libro's to re-brand, and quietly stamping a Libro logo and
    title on one is worse than leaving it alone. Two things are read: the
    letterhead mark, which has to be a Libro one, and the issuer names in
    `issuers`, matched in the top band only, so a Libro form that mentions the
    Canada Revenue Agency in its terms isn't caught by it.

    A shape is only compared with a house mark of about the same proportions.
    Squeezed into a square, a wide logo looks much like any other wide logo -
    the Libro wordmark came out closer to a government wordmark than to its
    own square form - and the proportions are what tell them apart."""
    band = page.get_textbox(fitz.Rect(0, 0, page.rect.width, zone)).lower()
    for name in issuers:
        if name.lower() in band:
            return True, f"the letterhead names {name!r}, so this form isn't Libro's"
    found = letterhead_marks(page, zone)
    if not found or not marks:
        return False, None
    best = None
    for fp, aspect in found:
        for ref in marks:
            if abs(aspect - ref["aspect"]) / ref["aspect"] <= 0.15:
                d = sum(abs(a - b) for a, b in zip(fp, ref["fp"])) / len(fp)
                best = d if best is None else min(best, d)
    # a mark that matches none of the stored ones, beside a letterhead that
    # names Libro, is a Libro logo drawn another way: Ln Renewal's reads 44
    # from every stored mark and its letterhead says "Libro Credit Union
    # Limited ("Libro")". Another issuer named there was caught above.
    if (best is None or best > 25) and re.search(r"\blibro\b", band):
        return False, None
    if best is None or best > 25:
        return True, ("the letterhead mark isn't a Libro one"
                      + (f" (nearest match {best:.0f}, needs 25 or less)" if best else
                         " (no mark of the same proportions to compare it with)"))
    return False, None


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
    old_titles, old_logos = find_old_header(printed(page), args.old_header_zone,
                                            args.old_title_min_size)
    others = [fitz.Rect(i["bbox"]) for i in read_once(page, "images", page.get_image_info)
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
    # Free space is what the page has below its content. It can come out
    # negative on a page that already ran past the bottom margin before we
    # touched it; that's the page's own doing, and only matters here if the
    # header actually asks the content to move.
    free = max(0.0, page.rect.height - args.min_bottom_margin - ext.y1)
    if dy > free:
        return None, [("HEADER_NO_ROOM",
                       f"content must move down {dy:.2f}pt to clear the header but only "
                       f"{free:.2f}pt is free below it (bottom margin kept at "
                       f"{args.min_bottom_margin:g}pt)")]

    issues = [("header_old_title_removed",
               f"{''.join(s['text'] for s in t['spans']).strip()!r} "
               f"({t['spans'][0]['font']} {t['spans'][0]['size']:.1f}pt)") for t in old_titles]
    # Taking the old title out is the one place the census is told to expect
    # text to vanish, so a wrong guess here is a loss nothing else can see.
    # Anything that reads like a form label rather than a title is called out.
    label_like = [t for t in old_titles
                  if "".join(s["text"] for s in t["spans"]).strip().endswith(":")
                  or "_" * 3 in "".join(s["text"] for s in t["spans"])]
    if label_like:
        issues.append(("HEADER_TITLE_CHECK",
                       f"{len(label_like)} line(s) removed as part of the old title read "
                       "like form labels, not a title - check nothing was lost: "
                       + "; ".join(repr("".join(s["text"] for s in t["spans"]).strip()[:40])
                                   for t in label_like)))
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
    """Take the old logo off page 1, and any line art lying inside the old
    title (an underlined title is text plus a separate rule, and the rule
    alone would be left floating under the new header).

    The title's text is not redacted here. restyle_page erases it along with
    every other line and simply doesn't redraw it, so a neighbour the erase
    catches is still redrawn. Redacting the title on its own clipped the line
    beneath it, and that text was gone before the restyle ever saw it."""
    forget(page)                       # the page is about to change
    expected = Counter()
    if plan["old_titles"]:
        for t in plan["old_titles"]:
            b = t["bbox"]
            page.add_redact_annot(fitz.Rect(b.x0 - pad, b.y0 - pad, b.x1 + pad, b.y1 + pad),
                                  fill=None, cross_out=False)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                              graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                              text=fitz.PDF_REDACT_TEXT_NONE)
    if plan["old_logos"]:
        for r in plan["old_logos"]:
            page.add_redact_annot(r, fill=None, cross_out=False)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_REMOVE,
                              graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                              text=fitz.PDF_REDACT_TEXT_NONE)
    return expected


def art_key(d):
    """What makes one drawing the same drawing as another: where it is, what
    it's made of, and how it's painted."""
    r = fitz.Rect(d["rect"])
    return (d["type"], tuple(round(v, 1) for v in r), len(d["items"]),
            tuple(d.get("fill") or ()), tuple(d.get("color") or ()))


def drop_at(cuts, y):
    """How far down something at y ends up, given where the page was cut.
    Each cut moves what is below it, so the cuts above a point add up."""
    return sum(step for line, step in cuts if y >= line - 0.01)


def can_push(page, from_y):
    """Whether the furniture below a line can be moved at all. A path that
    lies across the line can't be, and nothing else is worth attempting on a
    page where that's true."""
    return not any(fitz.Rect(d["rect"]).y0 < from_y - 0.01 < fitz.Rect(d["rect"]).y1
                   for d in printed(page).get_drawings() if d["items"])


def redraw_art(shape, d, dy):
    """Put one drawing back, dy points lower."""
    move = fitz.Matrix(1, 0, 0, 1, 0, dy)
    for item in d["items"]:
        if item[0] == "l":
            shape.draw_line(item[1] * move, item[2] * move)
        elif item[0] == "re":
            shape.draw_rect(fitz.Rect(item[1]) * move)
        elif item[0] == "qu":
            shape.draw_quad(item[1] * move)
        elif item[0] == "c":
            shape.draw_bezier(item[1] * move, item[2] * move,
                              item[3] * move, item[4] * move)
    cap = d.get("lineCap") or 0
    shape.finish(color=d.get("color"), fill=d.get("fill"),
                 width=d.get("width") or 0, dashes=d.get("dashes"),
                 even_odd=bool(d.get("even_odd")),
                 closePath=bool(d.get("closePath")),
                 lineJoin=d.get("lineJoin") or 0,
                 lineCap=max(cap) if isinstance(cap, (list, tuple)) else cap,
                 stroke_opacity=1 if d.get("stroke_opacity") is None
                 else d["stroke_opacity"],
                 fill_opacity=1 if d.get("fill_opacity") is None else d["fill_opacity"])


def push_down(page, from_y, delta):
    """Move what the form prints below `from_y` down by `delta`: its ruled
    lines, boxes and shading, and any field that sits there. Text isn't
    touched - the restyle draws that itself, and it is told to draw the lines
    below this point lower.

    This is the one thing in the tool that takes the form's own furniture
    apart and puts it back, so verify_line_art watches it: every shape has to
    come back, the same shape, or the run says so."""
    # What the page itself prints, not what its widgets draw: a signing tab's
    # own artwork belongs to the widget and travels with its rectangle, and
    # capturing it here baked a copy of the tab into the page.
    all_art = [d for d in printed(page).get_drawings() if d["items"]]
    art = [d for d in all_art if fitz.Rect(d["rect"]).y0 >= from_y - 0.01]
    # A single path can hold shapes both sides of the line - one form draws a
    # box at the top of the page and another 300pt below it as one path.
    # Removal keeps the part it didn't touch and hands back the rest, and
    # there is no honest way to put half a path somewhere else, so the move
    # is refused rather than guessed at.
    astride = [d for d in all_art
               if fitz.Rect(d["rect"]).y0 < from_y - 0.01 < fitz.Rect(d["rect"]).y1]
    if astride:
        return -len(astride)
    forget(page)
    rects = [fitz.Rect(d["rect"]) + (-0.5, -0.5, 0.5, 0.5) for d in art]
    if art:
        for r in rects:
            page.add_redact_annot(r)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                              graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
                              text=fitz.PDF_REDACT_TEXT_NONE)
        forget(page)
        # Removal takes whatever the rectangles touch, which reaches a little
        # past what was asked for: a rule a fraction of a point above the
        # first shape being moved goes with it. Rather than guess which, the
        # page is read again and whatever has actually gone is put back -
        # shapes below the line lower down, the rest exactly where they were.
        left = {art_key(d) for d in printed(page).get_drawings()}
        shape = page.new_shape()
        for d in all_art:
            if art_key(d) in left:
                continue                      # still on the page
            redraw_art(shape, d, delta if fitz.Rect(d["rect"]).y0 >= from_y - 0.01 else 0)
        shape.commit(overlay=False)           # back under the text
    doc = page.parent
    for xref, _, _ in page.annot_xrefs():
        if field_name(doc, xref) in SIGNING_WIDGETS:
            continue
        kind, val = doc.xref_get_key(xref, "Rect")
        if kind != "array":
            continue
        nums = [float(v) for v in val.strip()[1:-1].split()]
        if min(nums[1], nums[3]) < page.rect.height - from_y - 0.01:
            continue                          # PDF space runs bottom-up
        nums = [v - delta if i % 2 else v for i, v in enumerate(nums)]
        doc.xref_set_key(xref, "Rect", "[" + " ".join(f"{v:.4f}" for v in nums) + "]")
    forget(page)
    return len(art)


def shift_down(page, dy):
    """Move everything on the page down by dy points: the content stream under a
    translation, each annotation through its coordinates. A field's appearance
    is placed by its /Rect, so fields move without being redrawn. The signing
    widgets stay where the requirements tool put them. Returns issues for
    anything it couldn't move."""
    doc = page.parent
    issues = []
    forget(page)                       # the page is about to change
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
    forget(page)                       # the page is about to change
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
    others = [fitz.Rect(i["bbox"]) for i in read_once(page, "images", page.get_image_info)
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


# Options added after the first callers were written, with the value the
# command line gives them when they are left out. A caller that builds its
# own options - the sabotage tests do - gets these rather than an error.
LATER_OPTIONS = {"merge_fields": False, "signing": False, "close_gaps": False,
                 "gap_pad": 3.0, "gap_max": 60.0, "proof_scroll": False}


def process_file(src, dst, fonts, args, speller=None, relabels=None):
    for option, default in LATER_OPTIONS.items():
        if not hasattr(args, option):
            setattr(args, option, default)
    doc = fitz.open(src)
    # read before anything is touched: the header takes the old title away
    declared_doc = is_declaration(doc)
    name = os.path.basename(src)
    title = os.path.splitext(name)[0]
    rows = []

    if speller:
        rows += [[name, where, "spelling", detail]
                 for where, detail in spelling_issues(doc, title, speller)]

    # The header is planned before anything is touched: it decides how far
    # page 1's content moves, which the census has to know up front.
    header, dys = None, [0.0] * doc.page_count
    theirs, why = (third_party(doc[0], args.old_header_zone, house_marks(args.house_marks),
                               load_issuers(args.issuers))
                   if args.header and doc.page_count else (False, None))
    if theirs:
        rows.append([name, 1, "THIRD_PARTY_FORM",
                     why + (" - left as it is" if args.skip_third_party
                            else " - a Libro header was added anyway; pass --skip-third-party "
                                 "to leave these alone")])
    if args.header and doc.page_count and not (theirs and args.skip_third_party):
        header, notes = plan_header(doc[0], house_title(name), fonts, args)
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
    size, plans = args.size, [{"squeeze": {}} for _ in range(doc.page_count)]
    if args.reflow:
        size, plans, notes = plan_reflow(doc, fonts, args, fine,
                                         {i: v for i, v in enumerate(dys)})
        rows += [[name, where, issue, detail] for where, issue, detail in notes]
    elif args.best_fit:
        size, culprit = best_fit_size(doc, fonts, args.size, args.min_size,
                                      args.right_margin, args.quantise, fine)
        if size < args.size:
            rows.append([name, "", "size_reduced",
                         f"{args.size}pt -> {size}pt, limited by: {culprit!r}"])
    # Fine print is never set larger than the body it sits under.
    fine = [{k: min(s, size) for k, s in f.items()} for f in fine]

    # Labels to reword, decided at the settled size so they never change it.
    rows += [[name, where, issue, detail]
             for where, issue, detail in fit_fine_print(doc, fonts, args, size, fine, plans)]

    relabel = [{} for _ in range(doc.page_count)]
    if relabels:
        relabel, notes = plan_relabels(doc, fonts, args, relabels, size, fine, plans)
        rows += [[name, where, issue, detail] for where, issue, detail in notes]

    drop = []
    if header and not args.dry_run:
        expected[0].update(remove_old_header(doc[0], header, args.pad))
        # The old title is erased with the rest of page 1 and not redrawn.
        # Matched on where the line sits, not on an exact key: a line can be
        # read as one span here and as several elsewhere - a leading space
        # splits off on its own - and the key then names a different point.
        drop = [fitz.Rect(t["bbox"]) + (-2, -2, 2, 2) for t in header["old_titles"]]
        keep = plans[0].get("reflow", {})
        gone = {id(keep[k]) for k in keep if in_drop(k, drop)}
        for k in [k for k, v in keep.items() if id(v) in gone]:
            del keep[k]              # a re-wrap can't keep a line we're dropping

    tagged = [[] for _ in range(doc.page_count)]     # merge tags made boxes
    signed = [[] for _ in range(doc.page_count)]     # signing areas replaced
    field_names = {w.field_name for p in doc for w in p.widgets()}
    joined = [[] for _ in range(doc.page_count)]     # rules run back
    moved = [[] for _ in range(doc.page_count)]      # labels moved across
    underlines = [[] for _ in range(doc.page_count)]  # underlines lengthened
    # A form with no signing area on any page - read from a copy, before the
    # restyle moves anything - gets the standard block added, unless it is a
    # declaration, someone else's form, or one listed never to get one.
    adding = None
    if (args.signing and not args.dry_run and signing_listed(name)
            and not signing_excluded(name) and not theirs and not declared_doc):
        copy_ = fitz.open("pdf", doc.tobytes())
        if not asks_for_signature(copy_) and not any(signing_areas(p)[0] for p in copy_):
            adding = block_spot(copy_)
    for pno, page in enumerate(doc, start=1):
        shove = list(plans[pno - 1].get("pushes") or ()) if not args.dry_run else []
        # bottom-most cut first: a cut higher up then carries what has already
        # been moved down further still, which is how the cuts add up
        for line, step in sorted(shove, reverse=True):
            moved = push_down(page, line, step)
            if moved < 0:                     # a path lies across the line
                rows.append([name, pno, "PUSH_REFUSED",
                             f"the page below y={line:.0f} couldn't be moved down "
                             f"{step:.0f}pt: {-moved} path(s) lie across that line"])
                shove = [c for c in shove if c != (line, step)]
            else:
                rows.append([name, pno, "page_pushed_down",
                             f"everything below y={line:.0f} moved down {step:.0f}pt to "
                             f"make room for a paragraph ({moved} shape(s) moved)"])
        issues, allowed = restyle_page(
                page, fonts, size, args.pad, args.centre_tol, args.min_indent,
                args.width_tolerance, args.skip_conforming, args.target_font,
                args.size_tolerance, args.dry_run, args.gap_tolerance,
                not args.no_recover, plans[pno - 1].get("reflow"), plans[pno - 1].get("rules"),
                fine[pno - 1], relabel[pno - 1], drop if pno == 1 else None,
                plans[pno - 1].get("squeeze"), shove)
        expected[pno - 1].update(allowed)      # update, not +=: allowances can be negative
        for issue, detail in issues:
            rows.append([name, pno, issue, detail])
        # Mail-merge tags come off and text boxes go in their place. The census
        # is told each character that went, at the baseline it went from.
        if args.merge_fields and not args.dry_run:
            boxed = merge_tags_to_fields(page, field_names)
            tagged[pno - 1] = boxed
            for f in boxed:
                for base, ch in f["glyphs"]:
                    if not ch.isspace():
                        expected[pno - 1][(base, ord(ch))] -= 1
                rows.append([name, pno, "merge_tag_to_field",
                             f"{f['tag']} taken off the page; text box "
                             f"{f['name']!r} put in its place"])
            # The author's red notes go the same way: an instruction becomes
            # the box it asks for, a remark is taken off - and said in full,
            # because a question left for the client still wants answering.
            gone = author_notes_off(page, field_names)
            for n in gone:
                for l in n["lines"]:
                    for base, ch, _ in l["glyphs"]:
                        if not ch.isspace():
                            expected[pno - 1][(base, ord(ch))] -= 1
                if n["made"]:
                    tagged[pno - 1].append({"tag": n["text"], "name": n["made"]["name"],
                                            "ink": n["lines"][0]["box"],
                                            "rect": n["made"]["rect"]})
                    rows.append([name, pno, "author_note_to_field",
                                 f"{n['text']!r} taken off the page; text box "
                                 f"{n['made']['name']!r} put in its place"])
                elif n["under"]:
                    rows.append([name, pno, "author_note_removed",
                                 f"{n['text']!r} taken off the page; the form's own "
                                 f"field {n['under']!r} already sits there"])
                else:
                    rows.append([name, pno, "AUTHOR_NOTE_REMOVED",
                                 f"the form's author wrote {n['text']!r} - taken off "
                                 f"the page; pass it on if it still needs an answer"])
        # The member signing area, brought to the client's standard. It runs
        # before the gap pass, which would otherwise run a rule back inside an
        # area that is about to be replaced.
        if args.signing and not args.dry_run and signing_listed(name):
            declared = declared_doc
            if declared and pno == 1:
                rows.append([name, 1, "signing_area_left",
                             "a declaration - its signing and witnessing are left "
                             "as the form has them"])
            done, said = standardize_signing(page, fonts, min(size, 10.0),
                                             skip=bool(theirs) or declared)
            signed[pno - 1] = done
            for rec in done:
                for base, ch in rec["gone_glyphs"]:
                    expected[pno - 1][(base, ord(ch))] -= 1
                for base, ch in rec["made_glyphs"]:
                    expected[pno - 1][(base, ord(ch))] += 1
                for was, now, ch in rec["moved_glyphs"]:
                    if was is not None:
                        expected[pno - 1][(was, ord(ch))] -= 1
                    if now is not None:
                        expected[pno - 1][(now, ord(ch))] += 1
            rows += [[name, pno, issue, detail] for issue, detail in said]
            # A form that asks for no signature anywhere gets the block added
            # at the end of its fill-in part (the client's word, 2026-10-07).
            if (adding is not None and pno - 1 == adding[0]
                    and not any(signed[:pno])):
                rec, why = add_signing_block(page, fonts, min(size, 10.0), terms=adding[1])
                if rec is None:
                    rows.append([name, pno, "SIGNING_AREA_NO_ROOM",
                                 f"a block to add, the form asking for no signature: it {why}"])
                else:
                    signed[pno - 1] = signed[pno - 1] + [rec]
                    for base, ch in rec["made_glyphs"]:
                        expected[pno - 1][(base, ord(ch))] += 1
                    rows.append([name, pno, "SIGNING_AREA_ADDED",
                                 f"the form asks for no signature; the standard block was added "
                                 f"at y={rec['box'].y0:.0f}"
                                 + (" before its terms" if adding[1] else " at its end")])
        # Once the type is set, a caption that stands away from its own rule
        # can have the rule run back to meet it. This happens last because it
        # measures where the words actually ended up, not where they began.
        if args.close_gaps and not args.dry_run:
            # underlines first: the gap pass reads rules, and should see the
            # ones under headings at their full length
            forget(page)
            rewrapped = [k[1] for k in (plans[pno - 1].get("reflow") or {})]
            lined = follow_underlines(page, underlined_lines(fitz.open(src)[pno - 1]),
                                      rewrapped)
            # and a line under each text box set in a row of words
            shape = page.new_shape()
            now_words = page.get_text("words")
            # (never inside a standard signing area: the signing pass has
            # moved its fields and drawn its own lines)
            areas = [signing_extent(rec, page.rect.width) for rec in signed[pno - 1]]
            spans_ = []
            for x0, x1, y in inline_field_lines(fitz.open(src)[pno - 1]):
                if any(a.y0 - 4 <= y <= a.y1 + 4 for a in areas):
                    continue
                # The line spans the open space the box sits in, word to word,
                # as the words are now set: cut to the box it read as a stub
                # on Hypothecation, and re-set in Arial the "to" before the
                # first box reached into it.
                row = [w for w in now_words if w[1] - 1 <= y <= w[3] + 3]
                if any(x0 + 4 < w[0] < x1 - 4 for w in row):
                    continue                          # a word set inside it now
                # a word that starts before the box only moves the start on:
                # "Safe Deposit Box #:" grew into its box in Arial
                before = [w[2] for w in row if w[0] <= x0 + 4]
                after = [w[0] for w in row if w[0] >= x1 - 4]
                # with no word before or after it on its row, it runs to the
                # edge of the text the rows either side make - Hypothecation's
                # "_____ and _____" between two full lines stopped short
                near = [w for w in now_words if 0 < abs((w[1] + w[3]) / 2 - y) <= 30
                        and not (w[1] - 1 <= y <= w[3] + 3)]
                col0 = min((w[0] for w in near), default=x0)
                col1 = max((w[2] for w in near), default=x1)
                a0 = max(before) + 3 if before else min(x0, max(col0, 36.0))
                a1 = min(after) - 3 if after else max(x1, min(col1, page.rect.width - 36.0))
                # and never out of the box it is drawn in: Collection
                # Request's "From:" address lines ran on into the box beside
                walls_ = [v for v in uprights(printed(fitz.open(src)[pno - 1]))
                          if v.y0 - 2 <= y <= v.y1 + 2]
                # (the nearest wall either side of where the line starts:
                # the form's boxes there are wider than the box drawn round
                # them)
                wl = [v.x1 for v in walls_ if v.x1 <= x0 + 10]
                wr = [v.x0 for v in walls_ if v.x0 >= x0 + 10]
                if wl:
                    a0 = max(a0, max(wl) + 3)
                if wr:
                    a1 = min(a1, min(wr) - 3)
                if a1 - a0 < 20:
                    continue
                spans_.append([a0, a1, y, bool(after)])
            # boxes in one open space become one line
            spans_.sort(key=lambda t: (round(t[2]), t[0]))
            merged_ = []
            for t in spans_:
                if merged_ and abs(merged_[-1][2] - t[2]) <= 1.5 and t[0] <= merged_[-1][1] + 8:
                    merged_[-1][1] = max(merged_[-1][1], t[1])
                    merged_[-1][3] = merged_[-1][3] or t[3]
                else:
                    merged_.append(list(t))
            # lines on rows stacked one under another, with nothing after
            # them, end together (the Safe Deposit lease's Name / Account # /
            # Box # / Key # blanks came out ragged)
            free = sorted((t for t in merged_ if not t[3]), key=lambda t: t[2])
            groups, cur = [], []
            for t in free:
                if cur and t[2] - cur[-1][2] <= 50:
                    cur.append(t)
                else:
                    if cur:
                        groups.append(cur)
                    cur = [t]
            if cur:
                groups.append(cur)
            for g_ in groups:
                if len(g_) >= 2:
                    # the middle of their ends: the longest, on Wire
                    # Instructions CAD a name box nearly the width of the
                    # page, would stretch every line to it
                    ends = sorted(t[1] for t in g_)
                    end = ends[(len(ends) - 1) // 2] if len(ends) % 2 == 0 else ends[len(ends) // 2]
                    for t in g_:
                        t[1] = max(t[0] + 20, end)
            for x0, x1, y, _ in merged_:
                shape.draw_line(fitz.Point(x0, y), fitz.Point(x1, y))
                shape.finish(color=(0, 0, 0), width=0.6)
                lined.append({"y": y, "from": x0, "to": x1, "colour": (0, 0, 0),
                              "width": 0.6, "kind": "s", "field_line": True,
                              "drawn": fitz.Rect(x0, y - 0.6, x1, y + 0.6)})
            shape.commit()
            forget(page)
            underlines[pno - 1] = lined
            for g in lined:
                if g.get("field_line"):
                    rows.append([name, pno, "field_line_added",
                                 f"a line drawn under the text box at y={g['y']:.0f}, "
                                 f"{g['from']:.0f}-{g['to']:.0f}: it sits in a row of words "
                                 f"with nothing under it"])
                    continue
                if g.get("replaced") is not None:
                    rows.append([name, pno, "underline_followed",
                                 f"the underline at y={g['y']:.0f} laid again under its "
                                 f"words: {g['was']:.0f}pt -> {g['to'] - g['from']:.0f}pt"])
                    continue
                rows.append([name, pno, "underline_followed",
                             f"the underline at y={g['y']:.0f} lengthened "
                             f"{g['to'] - g['from']:.0f}pt to reach the end of its words"])
            forget(page)
            found = label_gaps(page, max_gap=args.gap_max)
            # A rule of its own is run back to the caption. A rule that is one
            # of a column keeps its edge and the caption moves to it, so the
            # column stays straight. The labels move first: running a rule
            # back does not disturb text, but moving text reads the page, and
            # it should read it before new rules are on it.
            # a label moves across to its column's rule only where the form set
            # its labels to end against their rules - read from the form as it
            # came, where the right edges still line up
            as_set = rule_labels(fitz.open(src)[pno - 1])
            movable = [g for g in found if g["in_column"]
                       and not g["in_label_column"] and ends_against_rules(g, as_set)]
            kept_open = [g for g in found if g["in_column"] and g not in movable]
            shifted = move_labels_to_rules(page, movable, fonts, args.gap_pad)
            joined[pno - 1] = close_label_gaps(
                page, [g for g in found if not g["in_column"]], args.gap_pad)
            moved[pno - 1] = shifted
            forget(page)
            for g in joined[pno - 1]:
                rows.append([name, pno, "gap_closed",
                             f"the rule after {g['said']!r} was run back "
                             f"{g['gap']:.0f}pt to meet it"])
            for g in shifted:
                rows.append([name, pno, "label_moved_to_rule",
                             f"{g['said']!r} was moved {g['gap'] - args.gap_pad:.0f}pt "
                             f"across to its rule, which is one of a column and "
                             f"keeps its edge"])
            for g in kept_open:
                rows.append([name, pno, "gap_kept",
                             f"{g['said']!r} stands clear of a rule in a column of "
                             f"rules, and the form did not set its labels to end "
                             f"against them - its own layout, left as it is"])
            undone = [g for g in movable
                      if not any(o["said"] == g["said"] and abs(o["y"] - g["y"]) < 0.6
                                 for o in shifted)]
            for g in undone:
                rows.append([name, pno, "GAP_LEFT_OPEN",
                             f"{g['said']!r} stands {g['gap']:.0f}pt clear of its rule "
                             f"and could not be moved to it - its rule is one of a "
                             f"column, so the rule was left alone too"])

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
                for issue, detail in verify_rules(orig[pno - 1], page, dys[pno - 1]):
                    rows.append([name, pno, issue, detail])
                for issue, detail in verify_words(orig[pno - 1], page):
                    rows.append([name, pno, issue, detail])
                for issue, detail in verify_overlap(orig[pno - 1], page, dys[pno - 1]):
                    rows.append([name, pno, issue, detail])
                for issue, detail in verify_tick_boxes(orig[pno - 1], page):
                    rows.append([name, pno, issue, detail])
                # The header is the one place art is meant to come and go: the
                # old title's rules go with it, and the new header brings its
                # own. The two pages are looked at in their own coordinates -
                # the old title's boxes on the original, the header's band on
                # the restyled one - because everything else on page 1 has
                # moved down between them.
                # (not the old logo's box: taking the logo off removes the
                #  image only, never line art, and a table border touching the
                #  logo's foot - the Loan Payout statements' - was counted gone
                #  on the form and then "gained" once moved down)
                gone = ([fitz.Rect(t["bbox"]) + (-2, -2, 2, 2) for t in header["old_titles"]]
                        if header and pno == 1 else [])
                added = ([fitz.Rect(0, 0, page.rect.width, header["bottom"] + 4)]
                         if header and pno == 1 else [])
                # Each piece laid in front of a rule is declared by its
                # shape, so the check still watches the strip of page it
                # lands on. Every one is named in the report above.
                expect = Counter(art_shape("s", g["colour"],
                                           g["drawn"].x1 - g["drawn"].x0, 0.0)
                                 for g in joined[pno - 1])
                # an underline's piece is drawn the way its rule is
                expect.update(art_shape(g["kind"], g["colour"], g["to"] - g["from"],
                                        g["width"] if g["kind"] == "f" else 0.0)
                              for g in underlines[pno - 1])
                # a signing area replaced: its old lines are gone on purpose,
                # and its new ones are expected by shape
                # an underline laid again: the form's rule is gone on purpose
                gone = list(gone) + [g["replaced"] + (-1, -1, 1, 1)
                                     for g in underlines[pno - 1]
                                     if g.get("replaced") is not None]
                for rec in signed[pno - 1]:
                    gone = list(gone) + rec.get("gone_art", [rec["box"] + (-2, -2, 2, 2)])
                    expect.update(rec["made_rules"])
                for issue, detail in verify_line_art(orig[pno - 1], page,
                                                     gone, added, expect):
                    rows.append([name, pno, issue, detail])
            if header:
                rows += [[name, 1, issue, detail] for issue, detail in check_header(out[0], header)]
            rows += [[name, "", issue, detail]
                     for issue, detail in verify_field_behaviour(orig, out)]
            # Every field the form had is still in the file, by name and kind,
            # but for those the signing block took out on purpose. Until now
            # the restyle checked what fields did, never that they were there.
            had = Counter((w.field_name, w.field_type_string) for p in orig for w in p.widgets())
            for recs in signed:
                for rec in recs:
                    had.subtract(Counter(rec.get("dropped", [])))
            kept_ = Counter((w.field_name, w.field_type_string) for p in out for w in p.widgets())
            for (fname, kind), n in sorted((had - kept_).items()):
                rows.append([name, "", "FIELD_LOST", f"{fname} ({kind})"
                             + (f" x{n}" if n > 1 else "")])
            rows += [[name, page, issue, detail]
                     for page, issue, detail in verify_looks(orig, out, header, signed, dys)]
            # Proofs come from the saved file too: the in-memory page has lost
            # its form fields by now, so it isn't what ships.
            if args.png:
                scroll = []
                for pno in range(min(orig.page_count, out.page_count)):
                    # Where the words changed: relabelled lines, and the old
                    # title the header replaces. Both move down with the page
                    # on the restyled side.
                    dy = dys[pno]
                    swaps = [e["box"] for e in relabel[pno].values() if e.get("box")]
                    # on the restyled side, box the words as they were
                    # actually set: the new wording is a different length,
                    # and a caption kept centred sits somewhere else again
                    drawn = []
                    for e in relabel[pno].values():
                        if not e.get("box"):
                            continue
                        band = fitz.Rect(0, e["box"].y0 + dy - 2,
                                         out[pno].rect.width, e["box"].y1 + dy + 2)
                        hit = out[pno].search_for(e["text"].strip(), clip=band)
                        if hit:
                            # the new wording can read the same as words
                            # already on that row - 'Name' sits inside
                            # "WITNESS' NAME" too - so take the one nearest
                            # where the label being replaced actually stood
                            want = (e["centre"] if e["centre"] is not None
                                    else (e["box"].x0 + e["box"].x1) / 2)
                            drawn.append(min(hit, key=lambda r: abs((r.x0 + r.x1) / 2 - want)))
                        else:
                            drawn.append(e["box"] + (0, dy, 0, dy))
                    dropped = ([fitz.Rect(t["bbox"]) for t in header["old_titles"]]
                               if header and pno == 0 else [])
                    # the same number on both sides for the same change
                    entries = [e for e in relabel[pno].values() if e.get("box")]
                    marks = [(r, "swap", i + 1) for i, r in enumerate(swaps)]
                    after_swaps = [(r, "swap", i + 1) for i, r in enumerate(drawn)]
                    legend = ["%d.  %r  ->  %r" % (i + 1, e["was"], e["text"].strip())
                              for i, e in enumerate(entries)]
                    # the gaps closed on this page, numbered on from the swaps
                    shut = ([("rule run back %.0fpt to meet" % g["gap"], g)
                             for g in joined[pno]]
                            + [("label moved %.0fpt across to the rule of"
                                % (g["gap"] - args.gap_pad), g)
                               for g in moved[pno]])
                    for j, (how, g) in enumerate(shut, start=len(entries) + 1):
                        after_swaps.append((g["drawn"] + (0, -3, 0, 3), "join", j))
                        legend.append("%d.  %s %r" % (j, how, g["said"]))
                    for k, rec in enumerate(signed[pno],
                                            start=len(entries) + len(shut) + len(tagged[pno]) + 1):
                        marks.append((rec["box"], "sign", k))
                        after_swaps.append((fitz.Rect(rec["box"].x0, rec["box"].y0,
                                                      rec["box"].x1, rec["bottom"])
                                            + (0, dy, 0, dy), "sign", k))
                        legend.append("%d.  signing area: %d member row(s) + staff row"
                                      % (k, rec["members"]))
                    for j, f in enumerate(tagged[pno], start=len(entries) + len(shut) + 1):
                        # on the form's side, where the tag actually sat
                        # there - the restyle moved the words before it
                        was = orig[pno].search_for(f["tag"]) or [f["ink"]]
                        marks.append((min(was, key=lambda r: abs(r.y0 - f["ink"].y0)),
                                      "field", j))
                        after_swaps.append((f["rect"] + (0, dy, 0, dy), "field", j))
                        legend.append("%d.  %s  ->  text box %r" % (j, f["tag"], f["name"]))
                    if dropped:
                        legend.append("red  the form's own title, replaced by the header")
                    note = ("   yellow = wording replaced" if swaps else "")
                    if joined[pno] or moved[pno]:
                        note += "   blue = caption and rule brought together"
                    if tagged[pno]:
                        note += "   green = merge tag now a text box"
                    if signed[pno]:
                        note += "   purple = signing area to the standard"
                    if dropped:
                        note += "   red = old title, replaced by the header"
                    one = None if args.proof_scroll else os.path.join(
                        args.png, f"{title} [{size:g}pt] p{pno + 1}_proof.png")
                    scroll.append(save_proof(
                        orig[pno].get_pixmap(dpi=args.png_dpi),
                        out[pno].get_pixmap(dpi=args.png_dpi), one,
                        f"{name}   page {pno + 1} of {out.page_count}   "
                        f"set at {size:g}pt   (original left, restyled "
                        f"right)" + note,
                        before_marks=marks + [(r, "gone") for r in dropped],
                        after_marks=after_swaps,
                        dpi=args.png_dpi, legend=legend))
                if args.proof_scroll and scroll:
                    join_down(scroll, os.path.join(
                        args.png, f"{title} [{size:g}pt] all_proof.png"))
    return rows, size


def digest_lines(rows, failed, sizes, target, relabels=None):
    """The run in the order someone has to act on it: what needs a person,
    what needs a decision, what merely happened. Everything here is in the
    CSV already - this is the CSV grouped and sorted, so a reviewer opens one
    file and knows which forms to touch instead of pivoting a spreadsheet."""
    files = sorted(sizes)
    out = [f"{len(files) + len(failed)} file(s)"
           + (f", {len(failed)} failed" if failed else "")]

    checks = Counter(r[2] for r in rows if r[2].isupper())
    if checks:
        out += ["", "CHECKS THAT FIRED"]
        out += [f"  {n:4} {issue}" for issue, n in sorted(checks.items())]
    else:
        out += ["", "CHECKS  all clear: no text lost, gained, past an edge or on a rule."]

    if failed:
        out += ["", f"FAILED ({len(failed)})"]
        for f, why in failed:
            out += [f"  {f}", f"      {why}"]

    # Anything a check refused to do. Grouped by check, worst first.
    by_issue = {}
    for r in rows:
        if r[2].isupper():
            by_issue.setdefault(r[2], []).append(r)
    if by_issue:
        total = sum(len(v) for v in by_issue.values())
        out += ["", f"NEEDS A HUMAN ({total})"]
        for issue, hits in sorted(by_issue.items(), key=lambda kv: -len(kv[1])):
            out.append(f"  {issue}")
            for r in hits:
                where = f" p{r[1]}" if r[1] not in ("", None) else ""
                out.append(f"    {r[0]}{where}")
                out.append(f"        {r[3]}")

    # Rewording the table asked for and the page wouldn't take. The most
    # actionable thing in the run: the wording is decided, the room isn't.
    stuck = [r for r in rows if r[2] == "relabel_for_human"]
    if stuck:
        out += ["", f"RELABEL BY HAND ({len(stuck)}, no room or inside a sentence)"]
        for r in stuck:
            out += [f"  {r[0]} p{r[1]}", f"      {r[3]}"]

    # Wording the table wouldn't touch: a real decision, one line per label.
    # A label already in the house wording needs no decision, so it's dropped.
    house = {label_key(v) for v in (relabels or {}).values()}
    review = [r for r in rows if r[2] == "relabel_review"
              and label_key(r[3].split("' (")[0].lstrip("'")) not in house]
    labels = [r for r in review if "(in running text)" not in r[3]]
    running = [r for r in review if "(in running text)" in r[3]]
    if labels:
        by_label = {}
        for r in labels:
            by_label.setdefault(r[3].split("' (")[0].lstrip("'"), []).append(r)
        out += ["", f"DECIDE THE WORDING ({len(by_label)} label(s), "
                    f"{len({r[0] for r in labels})} file(s))"]
        for label, hits in sorted(by_label.items(), key=lambda kv: -len(kv[1])):
            where = ", ".join(sorted({f"{r[0]} p{r[1]}" for r in hits})[:3])
            more = len({f"{r[0]} p{r[1]}" for r in hits}) - 3
            out.append(f"  {label!r}")
            out.append(f"      {where}" + (f" and {more} more" if more > 0 else ""))
    if running:
        out += ["", f"MENTIONS A WITNESS, STAFF OR OWNER IN RUNNING TEXT "
                    f"({len({r[0] for r in running})} file(s), left alone by design)"]
        out += [f"  {f}" for f in sorted({r[0] for r in running})]

    # What the house wording actually replaced, per form. The reviewer's
    # question is "should 'Owner' have become 'Name' on this form?", and that
    # can only be answered form by form, with the old words in front of them.
    done = [r for r in rows if r[2] == "relabelled"]
    if done:
        out += ["", f"WORDING REPLACED ({len(done)} label(s), "
                    f"{len({r[0] for r in done})} file(s)) - check each one reads right"]
        for f in sorted({r[0] for r in done}):
            out.append(f"  {f}")
            seen = {}
            for r in done:
                if r[0] == f:
                    seen.setdefault(r[3].split(" (kept")[0], []).append(r[1])
            for swap, pages in seen.items():
                where = ", ".join(f"p{p}" for p in sorted(set(pages), key=str))
                out.append(f"      {swap}   [{where}]")

    # Taking out the old title is the only text removed on purpose, so it's
    # listed too: nothing else would show that a form label went with it.
    gone = [r for r in rows if r[2] == "header_old_title_removed"]
    if gone:
        out += ["", f"OLD TITLE REMOVED ({len(gone)} line(s), "
                    f"{len({r[0] for r in gone})} file(s)) - replaced by the new header"]
        for f in sorted({r[0] for r in gone}):
            out.append(f"  {f}")
            out += [f"      {r[3]}" for r in gone if r[0] == f]

    # Size is the client's uniformity question, so it gets its own list.
    small = sorted(((z, f) for f, z in sizes.items() if z < target))
    if small:
        out += ["", f"SET BELOW {target:g}pt ({len(small)} of {len(files)})"]
        for z in sorted({z for z, _ in small}):
            named = [f for zz, f in small if zz == z]
            out.append(f"  {z:g}pt  " + "; ".join(named[:4])
                       + (f"; and {len(named) - 4} more" if len(named) > 4 else ""))
    at = len(files) - len(small)
    out.append(f"  {at} file(s) took the full {target:g}pt.")

    spelled = [r for r in rows if r[2] == "spelling"]
    if spelled:
        words = Counter(r[3].split("'")[1] for r in spelled if "'" in r[3])
        out += ["", f"SPELLING ({len(spelled)} word(s) in "
                    f"{len({r[0] for r in spelled})} file(s), report only)"]
        out.append("  " + ", ".join(f"{w} x{n}" if n > 1 else w
                                    for w, n in words.most_common(12))
                   + (" ..." if len(words) > 12 else ""))

    changed = Counter(r[2] for r in rows
                      if r[2] in ("relabelled", "reflowed", "ligature_spelt_out",
                                  "blank_shortened", "blank_moved", "reflow_blocked",
                                  "header", "kept_original_glyphs"))
    if changed:
        out += ["", "WHAT THE RUN DID"]
        out += [f"  {n:4} {k}" for k, n in sorted(changed.items())]
    return out


_TOOLS = {}


def restyle_one(job):
    """One file, in whatever process picks it up. Fonts and the dictionary
    are built once per worker: neither survives being sent between
    processes, and both cost more to make than a file costs to restyle."""
    src, dst, args, relabels = job
    if not _TOOLS:
        _TOOLS["fonts"] = resolve_fonts({k: getattr(args, f"font_{k}") for k in
                                         ("regular", "bold", "italic", "bolditalic")
                                         if getattr(args, f"font_{k}")})
        _TOOLS["speller"] = load_speller(args.spell_allow) if args.spellcheck else None
    try:
        rows, used = process_file(src, dst, _TOOLS["fonts"], args, _TOOLS["speller"], relabels)
        return src, rows, used, None
    except Exception as exc:                      # a bad file mustn't stop the batch
        return src, [], None, f"{type(exc).__name__}: {exc}"


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
    ap.add_argument("--jobs", type=int, default=1, metavar="N",
                    help="restyle N files at once (0 for one per core). Files don't "
                         "depend on each other, and the report comes back in the same "
                         "order however many are used.")
    ap.add_argument("--report", help="write a CSV of flagged lines here")
    ap.add_argument("--digest", help="where to write the run's summary for reviewers "
                                     "(default: beside --report, as .digest.txt)")
    ap.add_argument("--png", help="folder for side-by-side before/after proof images")
    ap.add_argument("--png-dpi", type=int, default=110)
    ap.add_argument("--signing", action="store_true",
                    help="bring each member signing area to the client's "
                         "standard: Name | Authorized Signature | "
                         "Date per member, then Authorizing Staff Member | Date")
    ap.add_argument("--merge-fields", action="store_true",
                    help="take mail-merge tags (a name in guillemets or double "
                         "angle brackets) off the page and put a text box in "
                         "each one's place")
    ap.add_argument("--close-gaps", action="store_true",
                    help="where a caption stands clear of the rule it labels, "
                         "run the rule back to meet it. A rule that lines up "
                         "with others in a column is reported instead, never "
                         "moved")
    ap.add_argument("--gap-pad", type=float, default=3.0,
                    help="space left between a caption and the rule run back "
                         "to it (default 3pt)")
    ap.add_argument("--gap-max", type=float, default=60.0,
                    help="the widest gap that is treated as a caption standing "
                         "off its rule rather than as layout (default 60pt)")
    ap.add_argument("--proof-scroll", action="store_true",
                    help="one tall proof per form instead of one per page: "
                         "every page joined top to bottom, form on the left, "
                         "restyled on the right, read in a single scroll")
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
    rl = ap.add_argument_group("relabelling (--relabel)")
    rl.add_argument("--relabel", action="store_true",
                    help="swap whole labels for the house wording in the table (Witness -> "
                         "Authorizing Staff Member, ...). Only whole labels that stand alone and "
                         "fit are changed, never words inside sentences; the rest is listed "
                         "for a human")
    rl.add_argument("--relabel-table", default=RELABEL_TABLE,
                    help="the table of 'label => replacement' lines (default relabel.txt "
                         "beside this script)")
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
    hd.add_argument("--skip-third-party", action="store_true",
                    help="don't put the Libro header on a form someone else issued - a "
                         "marketing board, the CRA, a payments network. Detected either "
                         "way and reported as THIRD_PARTY_FORM.")
    hd.add_argument("--issuers", default=ISSUERS,
                    help="issuer names whose forms aren't ours to re-brand, one per line "
                         "(default third_parties.txt beside this script)")
    hd.add_argument("--house-marks", default=HOUSE_MARKS,
                    help="fingerprints of the house logo, as written by --learn-marks")
    ap.add_argument("--rebuild", action="store_true",
                    help="experimental, and more than --restructure: let a paragraph that "
                         "has no room under it push the rest of the page down, moving the "
                         "form's own rules and boxes with it. Every shape is accounted "
                         "for (LINE_ART_LOST), and every push is reported.")
    ap.add_argument("--restructure", action="store_true",
                    help="experimental: reshape a form to hold the target size rather than "
                         "set the whole document smaller. Turns on --shrink-stubborn and "
                         "--condense 0.94 together; every change it makes is reported.")
    ap.add_argument("--condense", type=float, default=1.0, metavar="FLOOR",
                    help="how narrow a line may be set to fit its room, as a fraction of "
                         "its natural width (e.g. 0.94). Squeezing the glyphs leaves their "
                         "height alone, so it reads as unchanged where a smaller size "
                         "doesn't. 1.0, the default, never squeezes.")
    ap.add_argument("--shrink-stubborn", action="store_true",
                    help="let a line that can't be re-wrapped take a smaller size of its "
                         "own rather than holding the whole document down - a table-cell "
                         "label or a heading in a shaded bar. Each one is reported.")
    ap.add_argument("--fine-floor", type=float, default=5.0,
                    help="footers and fine print may be stepped down to this size to fit "
                         "the room they have; below it they're reported instead (default 5)")
    sc.add_argument("--spellcheck", action="store_true",
                    help="report words the dictionary doesn't know; never changes text")
    sc.add_argument("--spell-allow", default=ALLOWLIST,
                    help="words never to flag, one per line (default spelling_allowlist.txt "
                         "beside this script)")
    args = ap.parse_args()
    if args.rebuild:
        args.restructure = True
    if args.restructure:
        args.shrink_stubborn = True
        if args.condense >= 1.0:
            args.condense = 0.94

    fonts = resolve_fonts({k: getattr(args, f"font_{k}") for k in
                           ("regular", "bold", "italic", "bolditalic")
                           if getattr(args, f"font_{k}")})
    print(f"Using: {fonts['regular'][1]}")
    speller = load_speller(args.spell_allow) if args.spellcheck else None
    relabels = load_relabels(args.relabel_table) if args.relabel else None
    if args.relabel and not relabels:
        sys.exit(f"--relabel: no 'label => replacement' lines in {args.relabel_table}")

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

    all_rows, failed, sizes, used_by = [], [], [], {}
    jobs = [(src, dst, args, relabels) for src, dst in pairs]
    workers = getattr(args, "jobs", 1) if getattr(args, "jobs", 1) > 0 else (os.cpu_count() or 1)
    workers = max(1, min(workers, len(jobs)))
    if workers > 1:
        # Files don't depend on each other, so they go out to a pool. The
        # results come back in the order they were sent, whatever order they
        # were done in, so a run reads the same as a single-process one.
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=workers) as pool:
            done = list(pool.map(restyle_one, jobs))
    else:
        done = [restyle_one(job) for job in jobs]
    for i, (src, rows, used, err) in enumerate(done, 1):
        if err:
            failed.append((os.path.basename(src), err))
            print(f"[{i}/{len(pairs)}] {os.path.basename(src)}  FAILED: {err}")
            continue
        all_rows.extend(rows)
        sizes.append(used)
        used_by[os.path.basename(src)] = used
        flag = f"  [{len(rows)} flagged]" if rows else ""
        note = f"  @{used}pt" if used != args.size else ""
        print(f"[{i}/{len(pairs)}] {os.path.basename(src)}{note}{flag}")

    if args.report:
        with open(args.report, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["file", "page", "issue", "detail"])
            w.writerows(all_rows)
            w.writerows([[f, "", "file_failed", e] for f, e in failed])
        print(f"Report: {args.report}")
        where = args.digest or os.path.splitext(args.report)[0] + ".digest.txt"
        with open(where, "w", encoding="utf-8") as fh:
            for line in digest_lines(all_rows, failed, used_by, args.size, relabels):
                print(line, file=fh)
        print(f"Digest: {where}")

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
    if args.relabel:
        done = sum(1 for r in all_rows if r[2] == "relabelled")
        human = sum(1 for r in all_rows if r[2] == "relabel_for_human")
        review = sum(1 for r in all_rows if r[2] == "relabel_review")
        print(f"Relabelled {done} label(s); {human} left for a human; "
              f"{review} other line(s) mention a witness, staff or owner.")
    if args.spellcheck:
        spelled = [r for r in all_rows if r[2] == "spelling"]
        print(f"Spelling: {len(spelled)} word(s) to check in "
              f"{len({r[0] for r in spelled})} file(s).")

    clean =len(pairs) - len({r[0] for r in all_rows}) - len(failed)
    print(f"\n{len(pairs)} file(s): {clean} clean, "
          f"{len({r[0] for r in all_rows})} with flagged lines, {len(failed)} failed.")


if __name__ == "__main__":
    main()
