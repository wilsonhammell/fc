"""Rebuild a form's layout from scratch at the target size.

This is not the restyle. The restyle keeps every line where the form put it
and only changes the font; this reads what the form is made of - its
headings, its prose, its fill-in rows, its tables, its notes, its signature
block - and sets it out again, at a size the original couldn't hold.

Nothing here can be checked the way the restyle is checked: a rebuild moves
everything by design, so the census, the edge, rule and line-art checks have
nothing to compare. What is checked is the inventory - every word, every
field, every rule that was in the form has to be in the rebuild - and the
rest is for a person to look at, which is what the proof is for.

    python redesign.py "input forms/A form.pdf" -o rebuilt.pdf --proof proof.png
"""
import argparse
import json
import os
import re
import sys
from collections import Counter
from types import SimpleNamespace

import pymupdf as fitz

import restyle_pdf as R
import looks

BAR_GREY = (0.82, 0.81, 0.81)
# The fill each section bar was drawn in, by the id of its rect: a bar comes
# back in the form's own grey, not one house grey for every form (RRIF, RRSP
# and TFSA shade their headings 0.60, and came back 0.82).
BAR_FILL = {}
HIDDEN = []          # text the form draws in white on white
UNDER_LOGO = []      # template text under a logo set into a field, never seen
LOGO_DROPPED = []    # (name, kind) of a template's logo field and what it hid
MARKS = []           # symbols Arial has no letter for, and what went in instead
SPELT = []           # ligature glyphs the text layer named wrongly
TAKEN = set()        # field names the form already uses
ADDED = []           # text boxes put in place of merge tags
NOTES_OFF = []       # the form author's red notes, and what became of each
MERGE_NOTES = False  # --merge-fields: author notes come out with the merge tags
ARIAL = None         # the body face, for asking what it can and cannot draw


def drawable(ch, box):
    """Put back a plain character for a glyph Arial has no letter for.

    Forms draw their tick boxes and bullets in a symbol font, and the
    extractor cannot name those glyphs. Set in Arial they come out as an empty
    notdef box, so the form loses its tick boxes - which is what the check
    catches. Every such glyph in the corpus is a box or a bullet, so put in
    the plain character that looks the same and list what was changed."""
    if ARIAL.has_glyph(ord(ch)):
        return ch
    if box.width < 0.35 * box.height:
        swap = " "              # a space, drawn from a font with no name for it
    elif box.height >= 5 and 0.4 <= box.width / max(box.height, 1) <= 2.5:
        swap = "□"          # about as wide as it is tall: a tick box
    else:
        swap = "•"
    MARKS.append((ch, swap))
    return swap


# Rules a page draws in pieces end to end across the sheet, by page number:
# the line under a row of a form, which belongs to the row, not to any word
# in it (see separators()).
SEPARATORS = {}


def separators(page, flat, words=()):
    """The rules a page draws across its width in pieces: RRIF's payment block
    parts each row from the next with one line drawn as three or five pieces,
    one per column of the Word table it was set in. Read a piece at a time
    they became underlines under the words above, or blanks to write on.
    Returns the set of piece rectangles, rounded, that make up such lines."""
    out = set()
    rows = {}
    for r in sorted(flat, key=lambda r: ((r.y0 + r.y1) / 2, r.x0)):
        y = round((r.y0 + r.y1) / 2 / 0.8)
        rows.setdefault(y, []).append(r)
    for pieces in rows.values():
        pieces.sort(key=lambda r: r.x0)
        chain = [pieces[0]]
        for r in pieces[1:] + [None]:
            if r is not None and r.x0 - max(c.x1 for c in chain) <= 16:
                chain.append(r)
                continue
            span = max(c.x1 for c in chain) - chain[0].x0
            y = (chain[0].y0 + chain[0].y1) / 2
            # signature lines are drawn the same way, a piece per column, but
            # their captions hang right under them ("Witness" a point below
            # the line on Turkey); a separator has the next row well clear
            captioned = any(-1.5 <= w[1] - y <= 4 and w[2] > chain[0].x0
                            and w[0] < chain[-1].x1 for w in words)
            if len(chain) >= 2 and span >= 0.6 * page.rect.width and not captioned:
                out.update(tuple(round(v, 1) for v in c) for c in chain)
            if r is not None:
                chain = [r]
    return out


def runs_of(item):
    """An item's words with the weight and slant each one is set in."""
    return item.get("runs") or [(w, item["bold"], item["italic"], False, item.get("colour"))
                                for w in item["text"].split()]


def join_runs(a, b):
    """The word styles of two items joined into one, or None where both are
    set in one style throughout - the usual case, drawn as before."""
    if (a.get("runs") is None and b.get("runs") is None
            and a["bold"] == b["bold"] and a["italic"] == b["italic"]):
        return None
    return runs_of(a) + runs_of(b)


def word_styles(chars, spans, text, letters_of, flat=(), linebox=None):
    """Each word's weight and slant, read letter by letter.

    The rebuild sets an item in one weight - the one most of its letters have
    - which is right for rows and headings but loses a run-in heading inside a
    paragraph ("Renewal: The term ..."), an Act named in italics mid-sentence,
    a bold phrase in a clause - and an underline under some words of a line,
    like AFT's "Please note-". Underlined is looks.py's test: a rule from the
    baseline to a little under it, within the line's own ends (a cell border
    runs past them), under most of the word. Returns [(word, bold, italic,
    underlined)] in the order of text.split(), or None when every word is set
    alike and none is underlined, or the two readings disagree."""
    def under(box, base):
        drop = max(2.5, 0.35 * box.height)
        return any(base - 0.5 <= (r.y0 + r.y1) / 2 <= base + drop
                   and (linebox is None or (r.x0 >= linebox.x0 - 3
                                            and r.x1 <= linebox.x1 + 3))
                   and min(r.x1, box.x1) - max(r.x0, box.x0) >= 0.6 * box.width
                   for r in flat)
    def style(c):
        mid = (c["bbox"][0] + c["bbox"][2]) / 2
        for sp in spans:
            if sp["bbox"][0] - 0.5 <= mid <= sp["bbox"][2] + 0.5:
                ink = sp.get("color", 0)
                rgb = tuple(((ink >> k) & 255) / 255 for k in (16, 8, 0))
                return (bool(sp["flags"] & 16 or "bold" in sp["font"].lower()),
                        bool(sp["flags"] & 2 or "italic" in sp["font"].lower()),
                        rgb if max(rgb) > 0.25 else None)
        return None
    words, cur, seen, boxes, base_of = [], "", [], [], []
    for c in chars + [None]:
        if c is None or c["c"].isspace():
            if cur:
                known = [x for x in seen if x is not None]
                if not known:
                    return None
                b = sum(x[0] for x in known) * 2 > len(known)
                i = sum(x[1] for x in known) * 2 > len(known)
                ink = Counter(x[2] for x in known).most_common(1)[0][0]
                box = fitz.Rect(boxes[0])
                for r in boxes[1:]:
                    box |= r
                u = "_" not in cur and bool(flat) and under(box, max(base_of))
                words.append((cur, b, i, u, ink))
            cur, seen, boxes, base_of = "", [], [], []
            continue
        cur += letters_of(c)
        seen.append(style(c))
        boxes.append(fitz.Rect(c["bbox"]))
        base_of.append(c["origin"][1])
    words = [w for w in words if w[0].strip("_")]
    final = text.split()
    if len(words) != len(final):
        return None
    out = [(f, b, i, u, c) for f, (_w, b, i, u, c) in zip(final, words)]
    if (len({(b, i, c) for _f, b, i, _u, c in out}) <= 1
            and not any(u for _f, _b, _i, u, _c in out)):
        return None
    return out


def blocks_of(page, args):
    """What the form is made of, in reading order."""
    p = R.printed(page)
    lines, _ = R.collect_lines(p, False, "Arial", 10.0, 0.6)
    lines.sort(key=lambda l: (round(l["spans"][0]["origin"][1], 1), l["bbox"].x0))
    bars = [fitz.Rect(d["rect"]) for d in p.get_drawings()
            if d.get("fill") and 6 < fitz.Rect(d["rect"]).height < 30
            and fitz.Rect(d["rect"]).width > 60]
    rules = R.rule_segments(p)
    hori = [s for s in rules if s.width > 30 and s.height < 2.5]

    def in_bar(ln):
        return any(b.y0 - 3 <= ln["bbox"].y0 and ln["bbox"].y1 <= b.y1 + 3
                   and b.x0 - 3 <= ln["bbox"].x0 for b in bars)

    def rule_under(ln):
        base = ln["spans"][0]["origin"][1]
        return [s for s in hori if 0 <= s.y0 - base <= 6 and s.x1 > ln["bbox"].x0]

    rows, current, last = [], [], None
    for ln in lines:
        base = round(ln["spans"][0]["origin"][1], 1)
        if last is not None and abs(base - last) > 2:
            rows.append(current)
            current = []
        current.append(ln)
        last = base
    if current:
        rows.append(current)

    out = []
    for row in rows:
        text = "  ".join("".join(s["text"] for s in ln["spans"]).strip() for ln in row).strip()
        if not text:
            continue
        first = row[0]
        size = max(s["size"] for ln in row for s in ln["spans"])
        bold = any(s["flags"] & 16 or "bold" in s["font"].lower()
                   for ln in row for s in ln["spans"])
        italic = any(s["flags"] & 2 or "italic" in s["font"].lower()
                     for ln in row for s in ln["spans"])
        kind = "text"
        if in_bar(first):
            kind = "bar"
        elif "_" * 4 in text or rule_under(first):
            kind = "fill"
        elif len(row) >= 4:
            kind = "cells"
        out.append({"kind": kind, "text": text, "size": size, "bold": bold,
                    "italic": italic, "y": first["spans"][0]["origin"][1],
                    "cells": ["".join(s["text"] for s in ln["spans"]).strip() for ln in row],
                    "x": first["bbox"].x0})
    return out


def covered_by_pictures(page):
    """The rects of fields whose appearance is a picture - a logo set into a
    button, as a form template does it. A field draws over the page, so the
    text the template left under it ("Insert Logo Here", and a text box of
    its own holding the same words) is never seen on the form; drawn again
    by the rebuild, without the picture, it would be."""
    doc, out = page.parent, []
    for w in page.widgets():
        if w.field_type != fitz.PDF_WIDGET_TYPE_BUTTON:
            continue
        kind, val = doc.xref_get_key(w.xref, "AP/N")
        if kind != "xref":
            continue
        try:
            stream = doc.xref_stream(int(val.split()[0])) or b""
        except Exception:
            continue
        if re.search(rb"/\w+\s+Do\b", stream):
            out.append(fitz.Rect(w.rect))
    return out


def under_a_picture(rect, covers):
    rect = fitz.Rect(rect)
    return rect.get_area() > 0 and any((rect & c).get_area() >= 0.8 * rect.get_area()
                                       for c in covers)


def rows_with_fields(page, grids=()):
    """Each row of the form as an ordered run of things: text, then a field,
    then more text. "I, ___ of ___, in the Province of Ontario" is a row of
    five items, and keeping them in that order is what lets the rebuild put
    the blank back inside the sentence instead of at the end of the line."""
    p = R.printed(page)
    lines, _ = R.collect_lines(p, False, "Arial", 10.0, 0.6)
    # A PDF written by Word can map a ligature to the wrong character: the
    # page reads "certificate" while the text layer says "cerƟficate". Arial
    # has a glyph for that Ɵ, so nothing downstream notices and the rebuild
    # prints the wrong word. What the glyph really draws is worked out from
    # the word it sits in, the same way the restyle does it.
    unnamed = [(fitz.Rect(c[3]), (run["font"], c[1]))
               for run in p.get_texttrace() if run.get("type") in (0, 1)
               for c in run.get("chars", []) if c[0] == 0xFFFD]
    spelt = R.resolve_ligatures(p, unnamed, R.word_frequency()) if unnamed else {}
    for (font, gid), letters in sorted(spelt.items()):
        SPELT.append(f"a glyph {font} can't name (#{gid}) draws {letters!r}")

    def letters_of(c):
        """What this glyph really draws, whatever the text layer claims."""
        if unnamed:
            key = next((k for u, k in unnamed if R.glyph_in(c["bbox"], u)), None)
            if key is not None:
                return spelt.get(key, drawable(c["c"], fitz.Rect(c["bbox"])))
        return drawable(c["c"], fitz.Rect(c["bbox"]))

    items = []
    covers = covered_by_pictures(page)
    raw = {tuple(round(v, 1) for v in l["bbox"]): l
           for b in p.get_text("rawdict")["blocks"] if b.get("type") == 0
           for l in b["lines"]}
    # thin horizontal rules, for telling which words the form underlined
    # - not a table's own row lines: PAD's grid starts a hair under a line of
    # text, and its top edge, in cell-wide pieces, read as an underline. Only
    # the lattice is left out; an underline inside a cell ("Please note-") is
    # still one.
    edges_ = [(g["box"].x0, g["box"].x1, y) for g in grids
              for top, foot, _c in g["bands"] for y in (top, foot)]
    flat = [r for r in R.rule_segments(p) if r.height < 2.5 and r.width >= 4
            and not any(x0 - 2 <= r.x0 and r.x1 <= x1 + 2 and abs((r.y0 + r.y1) / 2 - y) <= 1.5
                        for x0, x1, y in edges_)]
    seps = separators(page, [fitz.Rect(d["rect"]) for d in p.get_drawings()
                             if d["rect"].height <= 3 and d["rect"].width >= 4],
                      p.get_text("words"))
    SEPARATORS[page.number] = seps
    flat = [r for r in flat if tuple(round(v, 1) for v in r) not in seps]
    # The top or bottom of a box, drawn side to side between its uprights, is
    # the box's art, not a line to write on: LIRA Fed's "ANNUITANT
    # IDENTIFICATION" box, Word's double border in thin strips, handed its
    # bottom edge to the CONTRACT# field, which then started left of its label.
    ups = R.upright_edges(p)
    def box_edge(r):
        y = (r.y0 + r.y1) / 2
        meets = lambda x: any(abs(vx - x) <= 3 and vy0 - 3 <= y <= vy1 + 3 and vy1 - vy0 > 12
                              for vx, vy0, vy1 in ups)
        return meets(r.x0) and meets(r.x1)
    # The author's red notes are not the form's words. The restyle takes them
    # off; so does the rebuild - an instruction becomes the box it asks for,
    # unless the form has one there already, and a remark simply goes.
    notes = R.author_notes(page) if MERGE_NOTES else []
    note_ink = {tuple(round(v, 1) for v in box)
                for n in notes for l in n["lines"] for _, _, box in l["glyphs"]}
    for n in notes:
        first = n["lines"][0]["box"]
        if n["asks"] and not n["under"]:
            name = R.note_field_name(p.get_text("words"), n, TAKEN)
            ADDED.append((name, "Text"))
            items.append({"what": "field", "rect": fitz.Rect(first.x0, first.y0 - 1,
                                                             max(first.x1, first.x0 + 90),
                                                             first.y1 + 1),
                          "base": (first.y0 + first.y1) / 2 + 2, "name": name,
                          "type": fitz.PDF_WIDGET_TYPE_TEXT, "kind": "Text",
                          "size": 10.0, "boxed": False, "value": None,
                          "bold": False, "italic": False, "flags": 0, "text": ""})
            NOTES_OFF.append(("author_note_to_field", f"page {page.number + 1}: "
                              f"{n['text']!r} taken out; text box {name!r} put in its place"))
        elif n["under"]:
            NOTES_OFF.append(("author_note_removed", f"page {page.number + 1}: "
                              f"{n['text']!r} taken out; the form's own field "
                              f"{n['under']!r} already sits there"))
        else:
            NOTES_OFF.append(("AUTHOR_NOTE_REMOVED", f"page {page.number + 1}: the form's "
                              f"author wrote {n['text']!r} - taken out; pass it on if it "
                              f"still needs an answer"))
    for ln in lines:
        # A line can hold several labels set apart across the row - "Branch:",
        # "Owner Number:", "Contract Number:" - each with its own field. Split
        # where the gap between one letter and the next is wide enough to be a
        # tab stop, whether the extractor called them separate spans or ran
        # them together with spaces, so each label goes back beside its field.
        exact = raw.get(tuple(round(v, 1) for v in ln["bbox"]))
        if exact is None:
            # The two reads of a line can disagree about its box by a hair.
            # Missed, the line fell back on its span texts, which carry a
            # space wherever the form changed font mid-word - "Ye s: Gove
            # rnin g Legislation" on the transfer authorization.
            b = ln["bbox"]
            exact = next((l for key, l in raw.items()
                          if all(abs(u - v) <= 1.0 for u, v in zip(key, b))), None)
        chars = [c for sp in exact["spans"] for c in sp["chars"]] if exact else []
        # A space that takes up no room - it starts where the next letter
        # starts - is not a space on the page. The transfer authorization's
        # writer put one between every change of font, mid-word, and read as
        # text they printed "Gove rnin g Legislation".
        chars = [c for k, c in enumerate(chars)
                 if not (c["c"] == " " and k + 1 < len(chars)
                         and abs(chars[k + 1]["origin"][0] - c["origin"][0]) < 0.3
                         and not chars[k + 1]["c"].isspace())]
        if note_ink:
            kept = [c for c in chars
                    if tuple(round(v, 1) for v in c["bbox"]) not in note_ink]
            if chars and not any(not c["c"].isspace() for c in kept):
                continue                  # the whole line was a note
            if not chars and any(fitz.Rect(ln["bbox"]).intersects(n["box"])
                                 and (fitz.Rect(ln["bbox"]) & n["box"]).get_area()
                                 >= 0.5 * fitz.Rect(ln["bbox"]).get_area() for n in notes):
                continue
            chars = kept
        sp0 = ln["spans"][0]
        if under_a_picture(ln["bbox"], covers):
            # under a logo the template set into a field: never seen
            UNDER_LOGO.append("".join(x["text"] for x in ln["spans"]).strip())
            continue
        if all(x.get("color", 0) == 0xFFFFFF for x in ln["spans"]):
            # White on white. One form carries a broken mail-merge tag under
            # its signature field this way; the restyle keeps it invisible by
            # keeping its colour, and a rebuild that draws everything black
            # would publish it.
            HIDDEN.append("".join(x["text"] for x in ln["spans"]).strip())
            continue
        style = {"size": max(x["size"] for x in ln["spans"]),
                 "bold": bool(sp0["flags"] & 16 or "bold" in sp0["font"].lower()),
                 "italic": bool(sp0["flags"] & 2 or "italic" in sp0["font"].lower())}
        if not chars:
            text = "".join(x["text"] for x in ln["spans"]).strip()
            if text:
                items.append(dict(style, what="text", text=text,
                                  rect=fitz.Rect(ln["bbox"]), base=sp0["origin"][1]))
            continue
        run, held = [], []
        for c in chars + [None]:
            # A wide run of spaces is a tab stop as surely as a geometric gap,
            # and so is a run of underscores: that is the form drawing its own
            # blank, and the rebuild puts a real field there instead. Left in,
            # they glue the label after them onto the label before.
            if c is not None and (c["c"].isspace() or c["c"] == "_"):
                held.append(c)
                continue
            spaces = (fitz.Rect(held[-1]["bbox"]).x1 - fitz.Rect(held[0]["bbox"]).x0
                      if held else 0.0)
            bars = [h for h in held if h["c"] == "_"]
            rules = len(bars)
            if rules >= 3:
                # the rule itself, not only the gap it makes. A line a form
                # types for someone to sign on often has no field behind it,
                # and thrown away as a separator it leaves the caption under
                # it with nothing to sign above - which no census can see,
                # because a census counts words and this has none.
                box = fitz.Rect(bars[0]["bbox"])
                for x in bars[1:]:
                    box |= fitz.Rect(x["bbox"])
                items.append({"what": "rule", "rect": box, "text": "",
                              "base": bars[0]["origin"][1],
                              "size": style["size"], "bold": False,
                              "italic": False})
            # A lone space stretched by justification or letter spacing is
            # still a space: FHSA's "Terms and Conditions for Deposit FHSA"
            # sets them 9pt wide and came back a word to an item, spread out.
            # A tab is several spaces, or one far wider than a space.
            tab = spaces > 8 and (len(held) >= 2 or spaces > 1.2 * style["size"])
            if run and (c is None or tab or rules >= 3
                        or (c["bbox"][0] - run[-1]["bbox"][2] > 8
                            and (len(held) != 1
                                 or c["bbox"][0] - run[-1]["bbox"][2] > 1.2 * style["size"]
                                 # a list marker and its tab: "1)  For the purpose"
                                 or re.fullmatch(r"\(?[0-9a-zA-Z]{1,4}[.)]",
                                                 "".join(x["c"] for x in run).strip())))):
                # a soft hyphen in the middle of a word draws as a hyphen;
                # set it as one so the word can still be searched for
                text = "".join(letters_of(x)
                               for x in run).strip().replace("­", "-")
                # A run of one or two underscores is the ragged end of a rule,
                # not writing. Runs of three or more have already been taken
                # out as blanks; what is left clings to the words beside it
                # and prints as "__ in the name(s) of".
                text = text.strip("_ ").strip()
                if text:
                    box = fitz.Rect(run[0]["bbox"])
                    for x in run[1:]:
                        box |= fitz.Rect(x["bbox"])
                    # The weight most of its letters have. Taken from the
                    # first span, "Deposits (funds leaving Libro) have been
                    # met." came out bold from end to end because "Deposits"
                    # was. Split into a run per weight instead, it rebuilt
                    # right but broke the row counts the title and page-break
                    # tests rely on, and two forms lost the fresh sheet their
                    # terms start on - so one weight per run, the right one.
                    # Read from the line's own spans, as collect_lines gives
                    # them: that is where faux bold - a regular face filled and
                    # stroked - is marked bold. The raw spans do not know it,
                    # and read from them the terms' titles lost their weight
                    # and with it the fresh sheet they start on.
                    reach = {"bold": 0.0, "italic": 0.0, "all": 0.0}
                    for sp in ln["spans"]:
                        b = fitz.Rect(sp["bbox"])
                        over = max(0.0, min(b.x1, box.x1) - max(b.x0, box.x0))
                        if not over or not sp["text"].strip():
                            continue
                        reach["all"] += over
                        if sp["flags"] & 16 or "bold" in sp["font"].lower():
                            reach["bold"] += over
                        if sp["flags"] & 2 or "italic" in sp["font"].lower():
                            reach["italic"] += over
                    if reach["all"]:
                        bold = reach["bold"] * 2 > reach["all"]
                        italic = reach["italic"] * 2 > reach["all"]
                    else:
                        bold, italic = style["bold"], style["italic"]
                    # and each word's own, for drawing: the item's one weight
                    # still decides how it is laid out
                    # its colour, where the form set it in one: a link in
                    # blue came back black
                    inks = Counter()
                    for sp in ln["spans"]:
                        b = fitz.Rect(sp["bbox"])
                        over = max(0.0, min(b.x1, box.x1) - max(b.x0, box.x0))
                        if over and sp["text"].strip():
                            inks[sp.get("color", 0)] += over
                    ink = inks.most_common(1)[0][0] if inks else 0
                    rgb = tuple(((ink >> k) & 255) / 255 for k in (16, 8, 0))
                    items.append(dict(style, what="text", text=text, rect=box,
                                      base=run[0]["origin"][1], bold=bold,
                                      italic=italic,
                                      colour=rgb if max(rgb) > 0.25 else None,
                                      runs=word_styles(run, ln["spans"], text,
                                                       letters_of, flat,
                                                       fitz.Rect(ln["bbox"]))))
                run = []
            elif held and rules < 3 and run:
                # (spaces before the first word belong to no word: glued on,
                # they gave "Terms" a slot 170pt wide)
                run += held
            held = []
            if c is not None:
                run.append(c)
    # A form draws its signature lines as often as it types them. A drawn one
    # is line art, so it used to be carried across and left to land wherever
    # the rows it sat between ended up - which is why signing blocks came out
    # with their lines above the wrong captions, or missing. Read as a place
    # to write it is laid out with its row like any other blank.
    #
    # Not the long dividers: a rule running the width of the text is a
    # section break, and belongs to the page rather than to a line of it.
    wide = page.rect.width * 0.8
    for dr in R.printed(page).get_drawings():
        r = fitz.Rect(dr["rect"])
        if not (r.height <= 3 and 30 <= r.width <= wide):
            continue
        if any(fitz.Rect(g["box"]).intersects(r) for g in grids):
            continue                      # part of a table, drawn with it
        if tuple(round(v, 1) for v in r) in SEPARATORS.get(page.number, ()):
            continue                      # a line under a row, carried with it
        if box_edge(r):
            continue                      # a box's top or bottom, carried with the box
        items.append({"what": "rule", "rect": fitz.Rect(r.x0, r.y0 - 9, r.x1, r.y1),
                      "text": "", "base": r.y1, "drawn": True,
                      "size": 10.0, "bold": False, "italic": False})

    for w in page.widgets():
        r = fitz.Rect(w.rect)
        # A template's logo, set into a button ("LogoField"), and the
        # placeholder box under it ("border", holding "Insert Logo Here"):
        # the house header puts Libro's logo in, and rebuilt without its
        # picture the pair came back as a stray button and a box printing the
        # placeholder's words.
        if any(abs(r.x0 - c.x0) < 0.5 and abs(r.y0 - c.y0) < 0.5 and abs(r.x1 - c.x1) < 0.5
               and abs(r.y1 - c.y1) < 0.5 for c in covers) or (
                w.field_type != fitz.PDF_WIDGET_TYPE_BUTTON and under_a_picture(r, covers)):
            LOGO_DROPPED.append((w.field_name, w.field_type_string))
            continue
        # Two tick boxes stacked on one square - Buyer Request has "NonReg" and
        # a leftover "Check Box34" over the same drawn box - are one box with
        # two fields. As two items the rebuild set them side by side, and
        # every choice came out with a doubled box. The second rides on the
        # first and is put back exactly over it, name and all.
        if w.field_type in (fitz.PDF_WIDGET_TYPE_CHECKBOX, fitz.PDF_WIDGET_TYPE_RADIOBUTTON):
            host = next((i for i in items if i["what"] == "field"
                         and i["type"] in (fitz.PDF_WIDGET_TYPE_CHECKBOX, fitz.PDF_WIDGET_TYPE_RADIOBUTTON)
                         and (i["rect"] & r).get_area() >= 0.5 * min(i["rect"].get_area(), r.get_area())),
                        None)
            if host is not None:
                host.setdefault("twins", []).append(
                    {"what": "field", "name": w.field_name, "type": w.field_type,
                     "kind": w.field_type_string, "value": w.field_value,
                     "props": R.field_props(w), "flags": w.field_flags})
                continue
        # a field sits astride the baseline of its own label, so its middle
        # lines up with the text far better than its bottom edge does
        items.append({"what": "field", "rect": r, "base": (r.y0 + r.y1) / 2 + 2,
                      "name": w.field_name, "type": w.field_type,
                      "kind": w.field_type_string, "size": 10.0,
                      "boxed": w.field_type_string == "Text" and boxed_field(page, w),
                      # a button is pressed, not written on: no line under it
                      "unruled": w.field_type == fitz.PDF_WIDGET_TYPE_BUTTON,
                      "value": w.field_value, "props": R.field_props(w),
                      "bold": False, "italic": False,
                      "flags": w.field_flags, "text": ""})
    # A form can draw a tick box twice over: once as a character in the text
    # and once as the fillable box itself. The restyle leaves both where they
    # were and they line up; a rebuild sets the character in Arial and adds
    # its own box, and the two come out side by side. Where a real box covers
    # the character, the character goes.
    # A typed rule and what sits on it are one thing, not two.
    #
    # A form draws one long rule and then puts several fields along it - the
    # FHSA's "Holder's Last Name / First Name / SIN / Date of Birth" is one
    # rule with four fields on it. Kept as a slot of its own, the rule takes
    # a column of its own and the fields draw their own blanks on top of it,
    # which is what made those lines overlap and sit away from their labels.
    # Where a rule carries fields, the fields own it; where it runs under a
    # line of text, it is that text's underline.
    fields = [i for i in items if i["what"] == "field"]
    # A text box the form left with nothing ruled under it - Milk's "Name of
    # First Borrower/Officer", typed under the signature line - is marked, so
    # a row of nothing but that box gets no line drawn the form never had.
    ruled_here = [r for r in R.rule_segments(p) if r.height < 2.5 and r.width >= 4]
    for f in fields:
        if f.get("kind") != "Text" or f.get("boxed"):
            continue
        b = f["rect"]
        f["unruled"] = not any(b.y0 + 0.4 * b.height <= (r.y0 + r.y1) / 2 <= b.y1 + 4
                               and min(r.x1, b.x1) - max(r.x0, b.x0) >= 0.4 * b.width
                               for r in ruled_here)
    texts = [i for i in items if i["what"] == "text"]

    def sits_on(a, r, share=0.35):
        over = min(a["rect"].x1, r.x1) - max(a["rect"].x0, r.x0)
        # a field stands on its line by its bottom edge: Est107's name and
        # signature fields are 29pt tall, their middles 15pt above the rule
        # they rest on, and read by the middle each came out as a line of
        # its own with the form's rule drawn again under it
        return (over > share * min(a["rect"].width or 1, r.width or 1)
                and (abs((a["rect"].y0 + a["rect"].y1) / 2 - r.y1) < 14
                     or (a["what"] == "field" and abs(a["rect"].y1 - r.y1) <= 3)))

    keep = []
    for item in items:
        if item["what"] == "rule":
            r = item["rect"]
            # a tick box never sits on a line to write on: Transfer
            # Authorization's "$ ______" brushes the tops of the RRSP / LIRA
            # boxes under it, and handed its baseline they joined its row
            on = [f for f in fields if sits_on(f, r)
                  and f.get("kind") not in ("CheckBox", "RadioButton")]
            if on:
                for f in on:
                    f["base"] = item["base"]
                    f["unruled"] = False
                    # fields along one typed line keep it as one line: Est115's
                    # Name / Relationship / Birthdate / S.I.N. rows are one
                    # rule each with four fields on it, and drawn a blank per
                    # field they came back as a row of dashes
                    if len(on) > 1:
                        f["on_rule"] = (round(r.x0, 1), round(r.y0, 1))
                        # and how far the line runs on past them: ABM's name
                        # line ran to the margin under two fields, and drawn to
                        # the last field it stopped 84pt short of the Branch
                        # line under it
                        f["rule_x1"] = r.x1
                if len(on) == 1:
                    f = on[0]
                    f["rect"] = fitz.Rect(min(f["rect"].x0, r.x0), f["rect"].y0,
                                          max(f["rect"].x1, r.x1), f["rect"].y1)
                continue
            # A rule underlines the words above it, never the caption below -
            # and it underlines ALL of them. One rule often runs under
            # several things set apart on the same line ("Libro Credit Union
            # Limited First Home Savings Account" and "FHSA 35220011" share
            # one), and giving it to the first one found left the rest with
            # no line at all.
            unders = [t for t in texts
                      if sits_on(t, r, 0.5) and r.y1 >= t["base"] - 2]
            if unders:
                # one rule under several things on a row ("Libro Credit Union
                # Limited First Home Savings Account" and "FHSA 35220011") is
                # one line under all of them, not an underline under each
                group = (r.x0, r.x1) if len(unders) > 1 else None
                for t in unders:
                    if group:
                        t["underline_group"] = group
                    # how far under the words the form drew it: a rule well
                    # below the baseline is a line under the row, not an
                    # underline, and set 2pt under the words it read as one
                    t["underline"] = min(8.0, max(1.5, (r.y0 + r.y1) / 2 - t["base"]))
                    # and how far it ran past the words: a label set on its
                    # own fill-in line ("Address/Postal Code: ____" with the
                    # line starting under the label) is not underlined, and
                    # cut to the label's width it became an underline
                    # Only for a short label on a short line of its own: a
                    # section divider under one item of a two-column block
                    # ran back to the margin through the column beside it.
                    ext = (max(0.0, t["rect"].x0 - r.x0), max(0.0, r.x1 - t["rect"].x1))
                    if (len(unders) == 1 and len(t["text"].split()) <= 6
                            and ext[0] <= 24 and ext[1] <= 120):
                        t["underline_ext"] = ext
                continue
        keep.append(item)
    items = keep

    # A label typed with its own blank - "Attorney Name: ________" in one run
    # of text - and a field set over those underscores: the field is that
    # blank. Read apart, the field sat a few points high on a row of its own,
    # was drawn with no line, and the label lost its underscores: Transfer In
    # POA's attorney and witness rows came back with nothing to write on.
    boxes_ = [i for i in items if i["what"] == "field"
              and i.get("kind") not in ("CheckBox", "RadioButton")]
    added_rules = []
    for item in items:
        if item["what"] != "text":
            continue
        m = re.match(r"^(.*?\S)\s*_{4,}[.:,]?\s*$", item["text"])
        if not m:
            continue
        r = item["rect"]
        label_end = r.x0 + r.width * len(m.group(1)) / max(len(item["text"].rstrip()), 1)
        over = [f for f in boxes_ if f["rect"].x0 >= label_end - 12 and f["rect"].x0 < r.x1
                and abs((f["rect"].y0 + f["rect"].y1) / 2 - (r.y0 + r.y1) / 2) <= 10]
        if not over and len(item["text"]) - len(m.group(1)) >= 8:
            # no field over it: the underscores are a line of their own, as
            # any other typed blank is - left in the label, "Witness
            # Signature: ____" came back with no line to sign on
            item["text"] = m.group(1)
            item["rect"] = fitz.Rect(r.x0, r.y0, label_end, r.y1)
            added_rules.append({"what": "rule", "rect": fitz.Rect(label_end + 3, r.y0, r.x1, r.y1),
                                "text": "", "base": item["base"], "size": item.get("size", 10.0),
                                "bold": False, "italic": False})
            continue
        if len(over) != 1:
            continue
        f = over[0]
        item["text"] = m.group(1)
        item["rect"] = fitz.Rect(r.x0, r.y0, min(label_end, f["rect"].x0 - 1), r.y1)
        f["base"] = item["base"]
        f["unruled"] = False
    items.extend(added_rules)
    # A run of dashes typed just under a field is the form's line for it:
    # Bill Payment Trace types "-----------" under each amount box, and with
    # the field's own line drawn as well every amount came back underlined
    # twice.
    gone_ = []
    for item in items:
        if item["what"] != "text" or not re.fullmatch(r"\s*-{6,}\s*", item["text"]):
            continue
        r = item["rect"]
        # (under it or in its lower half: the dashes can sit inside the box)
        over_ = [f for f in boxes_ if (f["rect"].y0 + f["rect"].y1) / 2 <= r.y0 + 3
                 and r.y0 - f["rect"].y1 <= 10
                 and min(r.x1, f["rect"].x1) - max(r.x0, f["rect"].x0) >= 0.5 * r.width]
        if over_:
            gone_.append(item)
            for f in over_:
                f["unruled"] = False
    if gone_:
        DASHES_DROPPED.extend(i["text"].strip() for i in gone_)
        items = [i for i in items if not any(i is g for g in gone_)]

    ticks = [i["rect"] for i in items
             if i["what"] == "field" and i["kind"] in ("CheckBox", "RadioButton")]
    kept = []
    for item in items:
        hit = (next((t for t in ticks if (item["rect"] & t).get_area() > 1), None)
               if item["what"] == "text" and "□" in item["text"] else None)
        tidy = lambda t: re.sub(r"\s{2,}", " ", t.replace("□", " ")).strip()
        hits = (sorted((t for t in ticks if (item["rect"] & t).get_area() > 1),
                       key=lambda t: t.x0)
                if hit is not None else [])
        if hit is not None and len(hits) > 1 and len(hits) == item["text"].count("□"):
            # Every box in the line, each with its own words after it: split
            # at the first box only, "Transfer to RRIF: ☐ New ☐ Existing RRIF
            # Contract #" came out with both boxes after "Contract #".
            r = item["rect"]
            parts = item["text"].split("□")
            edges = [r.x0] + [x for t in hits for x in (t.x0 - 1, t.x1 + 1)] + [r.x1]
            for k, part in enumerate(parts):
                x0, x1 = edges[2 * k], edges[2 * k + 1]
                if tidy(part) and x1 > x0:
                    kept.append(dict(item, text=tidy(part),
                                     rect=fitz.Rect(x0, r.y0, max(x0 + 1, x1), r.y1)))
            continue
        if hit is not None:
            # The words either side of the box go either side of the real box:
            # left spanning it, the text sorted ahead of the box and "Debit ☐
            # (Other FI's ...)" came out "Debit (Other FI's ...) ☐".
            k = item["text"].index("□")
            head, tail = tidy(item["text"][:k]), tidy(item["text"][k + 1:])
            r = item["rect"]
            if head:
                kept.append(dict(item, text=head,
                                 rect=fitz.Rect(r.x0, r.y0, max(r.x0 + 1, hit.x0 - 1), r.y1)))
            if tail:
                kept.append(dict(item, text=tail,
                                 rect=fitz.Rect(min(r.x1 - 1, hit.x1 + 1), r.y0, r.x1, r.y1)))
            continue
        kept.append(item)
    items = kept

    # A tall tick box goes with the line its top is level with: set beside a
    # two-line item, its own baseline falls by the second line, and Indemnity's
    # "OFFICIAL CREDIT UNION CHEQUE" box came back beside "Order Number".
    for b in items:
        if b["what"] != "field" or b.get("kind") not in ("CheckBox", "RadioButton"):
            continue
        r = b["rect"]
        level = [t for t in items if t["what"] == "text"
                 and 0 <= t["rect"].x0 - r.x1 <= 20 and abs(t["rect"].y0 - r.y0) <= 10]
        if level and r.height > 1.3 * level[0].get("size", 10.0):
            b["base"] = min(level, key=lambda t: abs(t["rect"].y0 - r.y0))["base"]

    # group into rows by baseline first, then read each row left to right:
    # sorting by baseline throughout puts a field a point higher than its own
    # sentence ahead of it, and the blank ends up before the words it sits in
    items.sort(key=lambda i: i["base"])
    rows, current, anchor = [], [], None
    for it in items:
        # measure against the baseline the row started on, not the item
        # before. Two columns set a few points out of step with each other
        # walk a page down in steps of less than a line, and a row that
        # chases the item before it swallows the whole column: every line of
        # both columns ends up on one row, each squeezed to one word wide and
        # marching off the edge of the sheet.
        if anchor is not None and abs(it["base"] - anchor) > 5:
            rows.append(current)
            current, anchor = [], None
        current.append(it)
        if anchor is None:
            anchor = it["base"]
    if current:
        rows.append(current)

    # A tall field's middle can sit several points above the baseline of the
    # words it belongs beside, which puts "Dated at" on one row and the blank
    # after it on another - so the blank is ruled above the words instead of
    # beside them. Where a row of nothing but blanks sits just above a row of
    # words and the two do not overlap across the page, they are one row.
    joined = []
    for row in rows:
        last = joined[-1] if joined else None
        if (last and all(i["what"] != "text" for i in last)
                and any(i["what"] == "text" for i in row)
                and 0 < min(i["base"] for i in row) - max(i["base"] for i in last) <= 8
                and not any(a["rect"].x0 + 1 < b["rect"].x1
                            and b["rect"].x0 + 1 < a["rect"].x1
                            for a in last for b in row)):
            joined[-1] = last + row
            continue
        # and the other way: blanks typed a few points under the words they
        # follow ("Contributor Name: ____ SIN: ____") came out on a row of
        # their own under the words
        if (last and all(i["what"] == "rule" for i in row)
                and any(i["what"] == "text" for i in last)
                and 0 < min(i["base"] for i in row) - max(i["base"] for i in last) <= 5
                and not any(a["rect"].x0 + 1 < b["rect"].x1
                            and b["rect"].x0 + 1 < a["rect"].x1
                            for a in last for b in row)):
            joined[-1] = last + row
            continue
        joined.append(row)
    rows = joined

    # A tick box's words split off onto a row of their own: two columns set a
    # few points out of step put RRSP Withdrawal's "☐ Refund of Undeducted
    # Contributions" 5pt below the row its box was read into, and the box was
    # rebuilt beside "Contract Termination" with its words on a line under it.
    # Words that start right where a box on the row above ends are that box's.
    k = 1
    while k < len(rows):
        row, up = rows[k], rows[k - 1]
        if (len(row) == 1 and row[0]["what"] == "text"
                and any(i["what"] == "field" and i.get("kind") in ("CheckBox", "RadioButton")
                        and -2 <= row[0]["rect"].x0 - i["rect"].x1 <= 6
                        and 0 < row[0]["base"] - i["base"] <= 6
                        and not any(j is not i and j["rect"].x0 >= i["rect"].x1 - 1
                                    and j["rect"].x0 < row[0]["rect"].x1 for j in up)
                        for i in up)):
            rows[k - 1] = up + row
            del rows[k]
            continue
        k += 1

    # And the other way: a tall tick box set beside a two-line item - Indemnity's
    # "CREDIT UNION CERTIFIED CHEQUE / Order Number & $Amount" - has its middle
    # between the lines, and read as a row of its own it was rebuilt on a line
    # by itself under its words. It goes with the first line it stands beside.
    k = 1
    while k < len(rows):
        row, up = rows[k], rows[k - 1]
        words_up = sorted((i for i in up if i["what"] == "text"), key=lambda i: i["rect"].x0)
        if (len(row) == 1 and row[0]["what"] == "field"
                and row[0].get("kind") in ("CheckBox", "RadioButton") and words_up):
            box, first = row[0]["rect"], words_up[0]
            if (0 <= first["rect"].x0 - box.x1 <= 20
                    and min(box.y1, first["rect"].y1) - max(box.y0, first["rect"].y0) >= 4
                    and not any(i["rect"].x1 > box.x0 - 1 and i["rect"].x0 < box.x1 + 1
                                for i in up)):
                rows[k - 1] = up + [dict(row[0], base=first["base"])]
                del rows[k]
                continue
        k += 1

    # Label-then-blank rows one under another, their blanks started close
    # together, are a column of blanks: each starts after the widest label
    # in it. Set from each row's own label, ABM's "Name:" line began 14pt
    # left of "Member Number:"'s once the label grew.
    font_ = ARIAL or R.resolve_fonts({})["regular"][2]
    srt = lambda r: sorted(r, key=lambda i: i["rect"].x0)
    lead_ = lambda r: (len(r) >= 2 and srt(r)[0]["what"] == "text" and srt(r)[1]["what"] != "text"
                       and srt(r)[1].get("kind") not in ("CheckBox", "RadioButton"))
    k = 0
    while k < len(rows):
        j = k
        while (j + 1 < len(rows) and lead_(rows[k]) and lead_(rows[j + 1])
               and abs(srt(rows[j + 1])[0]["rect"].x0 - srt(rows[k])[0]["rect"].x0) <= 3
               and abs(srt(rows[j + 1])[1]["rect"].x0 - srt(rows[k])[1]["rect"].x0) <= 12):
            j += 1
        if j > k:
            group = [srt(r) for r in rows[k:j + 1]]
            widest = max(font_.text_length(r[0]["text"].strip(), 10.0)
                         * (1.07 if r[0].get("bold") else 1.0) for r in group)
            gap = max(min(24.0, max(4.2, r[1]["rect"].x0 - r[0]["rect"].x1)) for r in group)
            start = max(min(r[0]["rect"].x0 for r in group) + widest + 1 + gap,
                        max(r[1]["rect"].x0 for r in group))
            for r in group:
                r[1]["min_x"] = start
        k = j + 1

    # and they end together: labels one under another, each with a blank after
    # it - Bill Payment's Name / Home Branch / Number - end their blanks at
    # one point, as far as every row has room for, where each had its own
    k = 0
    while k < len(rows):
        j = k
        while (j + 1 < len(rows) and lead_(rows[k]) and lead_(rows[j + 1])
               and abs(srt(rows[j + 1])[0]["rect"].x0 - srt(rows[k])[0]["rect"].x0) <= 3):
            j += 1
        # (a line run to the margin keeps its length: Est107's "Late of:" ran
        #  the width of the page, and cut back to the "of:" line over it that
        #  stops for "S.I.N.:" it looked unfinished)
        full_ = lambda r: srt(r)[1]["rect"].x1 >= page.rect.width - 70 and len(r) == 2
        if j > k and any(full_(r) for r in rows[k:j + 1]):
            j = k
        if j > k:
            group = [srt(r) for r in rows[k:j + 1]]
            # (clear of the next label by the rebuild's widest gap, 24pt)
            limits = [(r[2]["rect"].x0 - 24 if len(r) > 2 else 10 ** 6) for r in group]
            end = min(max(r[1]["rect"].x1 for r in group), min(limits))
            if all(end - r[1]["rect"].x0 >= 36 for r in group):
                for r in group:
                    b = r[1]["rect"]
                    r[1]["rect"] = fitz.Rect(b.x0, b.y0, end, b.y1)
                    r[1]["end_x"] = end
        k = j + 1

    # A short label set beside a block of two rows of choices, halfway down
    # it - Transfer Authorization's "Registered Type (check one)" beside its
    # TFSA / RRSP ... boxes over FHSA / RRIF ... - is a row of its own by
    # baseline, between the two, and was rebuilt on a line by itself with the
    # first row of boxes above it. It goes on the first row, where it reads.
    def choices(r, lab):
        return (len(r) >= 2
                and any(i["what"] == "field" and i.get("kind") in ("CheckBox", "RadioButton")
                        for i in r)
                and min(i["rect"].x0 for i in r) >= lab["rect"].x1 + 2)
    k = 1
    while k + 1 < len(rows):
        row, up, down = rows[k], rows[k - 1], rows[k + 1]
        lab = row[0] if len(row) == 1 and row[0]["what"] == "text" else None
        if (lab is not None and len(lab["text"].split()) <= 6
                and not lab["text"].rstrip().endswith(".")
                and choices(up, lab) and choices(down, lab)):
            size = lab.get("size", 10.0) or 10.0
            words_up = [i for i in up if i["what"] == "text"] or up
            words_down = [i for i in down if i["what"] == "text"] or down
            ub = min(words_up, key=lambda i: i["rect"].x0)["base"]
            db = min(words_down, key=lambda i: i["rect"].x0)["base"]
            # the two rows of boxes a line apart, the label between them
            if ub < lab["base"] < db and db - ub <= 2.2 * size:
                font = ARIAL or R.resolve_fonts({})["regular"][2]
                wide = font.text_length(lab["text"].strip(), 10.0) * 1.05
                need = lab["rect"].x0 + wide + 6 - min(i["rect"].x0 for i in up + down)
                reach = max(i["rect"].x1 for i in up + down)
                # Too wide for the room in front of the boxes once set at
                # 10pt it wrapped there, and the row it pushed along fell out
                # of line with the one under it. Both rows move over together,
                # as far as the label needs, where the page has the room.
                if 0 < need and reach + need <= page.rect.width - 46:
                    for i in up + down:
                        i["rect"] = i["rect"] + (need, 0, need, 0)
                    need = 0
                if need <= 0:
                    d = ub - lab["base"]
                    rows[k - 1] = up + [dict(lab, base=ub, rect=lab["rect"] + (0, d, 0, d))]
                    del rows[k]
                    continue
                # With no room for that either, set on a line of its own over
                # the block, it still comes before the first box.
                # Given the block's own baseline, it keeps the gap the form
                # left above the block and sits a line over the boxes: placed
                # halfway up that gap it read as the last line of the address
                # block over it.
                nb = ub - 0.5
                d = nb - lab["base"]
                rows[k - 1:k + 1] = [[dict(lab, base=nb, rect=lab["rect"] + (0, d, 0, d))], up]
                k += 1
                continue
        k += 1

    for row in rows:
        row.sort(key=lambda i: i["rect"].x0)
    # Labels set flush right against a common edge - GIC's "Owner Name:",
    # "Joint Owner Name:", "Product Type:" all end at 185.8 - stay flush
    # right. Placed from their left edges at the new size, and with the
    # relabel shortening some of them, the column came apart.
    texts_ = [(k, i) for k, row in enumerate(rows) for i in row if i["what"] == "text"]
    # whether a label has a place to write after it on its row: GIC's
    # labels all do; the title lines Chicken's "Member Number:" happens to
    # end with do not, and set flush right with them it had no room for its
    # blank and broke in two
    followed = {id(i): any(j["what"] != "text" and j["rect"].x0 >= i["rect"].x1 - 2
                           for j in rows[k]) for k, i in texts_}
    for k, i in texts_:
        if len(i["text"]) > 40:
            continue
        mates = [j for k2, j in texts_
                 if j is not i and 0 < abs(k2 - k) <= 3 and len(j["text"]) <= 40
                 # (one with a box after it does not line up with lines
                 # that have none; the first line of a two-line label may)
                 and not (followed[id(i)] and not followed[id(j)])
                 and abs(j["rect"].x1 - i["rect"].x1) <= 1.5
                 and abs(j["base"] - i["base"]) <= 60]
        # a column, not a coincidence: three labels ending together, or two
        # that are both "Label:" - "BETWEEN:" and the "and" under it end
        # together by chance and are set flush left
        if not any(abs(j["rect"].x0 - i["rect"].x0) > 3 for j in mates):
            continue
        if len(mates) >= 2 or (mates and i["text"].rstrip().endswith(":")
                               and all(j["text"].rstrip().endswith(":") for j in mates)):
            i["ralign"] = i["rect"].x1
    return rows


# "(a)", "A.", "12.", "iv)" - the start of a new item in a list, which is
# never the rest of the sentence above it, whatever follows the marker
def align_stacks(rows, size=10.0, tol=12.0):
    """Set a stack of short lines that belong together flush on one edge.

    An address block - "Libro Credit Union Limited", then the street, the
    city, the phone and the fax, one to a line - is one thing, and the client
    asked for the name and the address to line up. Forms often set the first
    line a few points in from the rest: a leading space, a tab stop, a bold
    name nudged over. Rebuilt faithfully, the stagger looks like a mistake.

    A stack is three or more rows in a row, each a single run of text, whose
    left edges are within `tol` of each other. It is only aligned when the
    left edges agree more closely than the centres do - that is what makes
    it a flush-left block and not a set of centred lines, which are left
    alone. Every line goes to the edge most of them already share."""
    singles = [len(r) == 1 and r[0]["what"] == "text" for r in rows]
    k = 0
    while k < len(rows):
        if not singles[k]:
            k += 1
            continue
        j = k
        while (j + 1 < len(rows) and singles[j + 1]
               and rows[j + 1][0]["base"] - rows[j][0]["base"] <= size * 2.2
               and abs(rows[j + 1][0]["rect"].x0 - rows[k][0]["rect"].x0) <= tol):
            j += 1
        stack = [rows[i][0] for i in range(k, j + 1)]
        if len(stack) >= 3:
            lefts = [i["rect"].x0 for i in stack]
            mids = [(i["rect"].x0 + i["rect"].x1) / 2 for i in stack]
            spread = max(lefts) - min(lefts)
            if 0.5 < spread < max(mids) - min(mids):
                edge = Counter(round(x) for x in lefts).most_common(1)[0][0]
                for item in stack:
                    r = item["rect"]
                    item["rect"] = fitz.Rect(edge, r.y0, edge + r.width, r.y1)
        k = j + 1
    return rows


def merge_tags_to_fields(rows, taken, notes, added):
    """Take each mail-merge tag out of the text and put a text box in its place.

    "Owner No: \u00abCustomer_Number\u00bb" is a label and an instruction to a
    mail merge that never ran. The client's word was to remove the wording
    between the brackets and put a text box there. The tag comes out of the
    text item - so the relabel that follows sees "Owner No:" on its own and
    can make it "Member Number:" - and a field item takes its place in the
    same row, where the rebuild sets it out as a blank to write on.

    `taken` is every field name the form already has; `added` collects the
    new ones so the field check expects them rather than calling them gained."""
    for row in rows:
        k = 0
        while k < len(row):
            item = row[k]
            k += 1
            if item["what"] != "text":
                continue
            m = R.MERGE_TAG.search(item["text"])
            if not m:
                continue
            text = item["text"]
            head, tail = text[:m.start()].rstrip(), text[m.end():].lstrip()
            r = fitz.Rect(item["rect"])
            # where the tag sat along the line, near enough for the rebuild to
            # keep the box in the same column as the words around it
            share = lambda i: r.x0 + r.width * i / max(len(text), 1)
            x0, x1 = share(m.start()), share(m.end())
            name = R.merge_tag_name(m.group(0), item["text"][:m.start()])
            stem, n = name, 2
            while name in taken:
                name, n = f"{stem}_{n}", n + 1
            taken.add(name)
            added.append((name, "Text"))
            notes.append(("merge_tag_to_field",
                          f"{m.group(0)} taken out; text box {name!r} put in its place"))
            box = {"what": "field", "rect": fitz.Rect(x0, r.y0, max(x1, x0 + 90), r.y1),
                   "base": item["base"], "name": name,
                   "type": fitz.PDF_WIDGET_TYPE_TEXT, "kind": "Text",
                   "size": item.get("size", 10.0), "boxed": False, "value": None,
                   "bold": False, "italic": False, "flags": 0, "text": ""}
            pieces = []
            if head:
                # (no longer flush right with what it ended with: it has a
                # box after it now - Chicken's "Owner No: <<Customer_Number>>")
                pieces.append(dict(item, text=head, ralign=None,
                                   rect=fitz.Rect(r.x0, r.y0, x0, r.y1)))
            pieces.append(box)
            if tail:
                pieces.append(dict(item, text=tail,
                                   rect=fitz.Rect(max(x1, x0 + 90), r.y0, r.x1, r.y1)))
            row[k - 1:k] = pieces
            k = k - 1 + len(pieces) - (1 if tail else 0)
    return rows


def relabel_rows(rows, table, notes, page_no=0, before=None, declared=False):
    """Swap whole labels for the house wording, the way the restyle does.

    Only a row that is one standalone label is touched - never a word inside
    a sentence - but unlike the restyle the rebuild never has to refuse one
    for want of room, because the row is set out again around the new
    wording. Anything mentioning a witness or staff that the table does not
    cover is listed for a human instead."""
    for row in rows:
        for item in row:
            if item["what"] != "text":
                continue
            text = " ".join(item["text"].split())
            new = table.get(R.label_key(text))
            if new is not None and R.signer_prompt(text, new):
                new = None      # a party's name asked for, not a signature caption
            if new is not None and len(text.split()) == 1 and text[:1].islower():
                new = None      # a wrapped word, not a label (see plan_relabels)
            if new is None:
                if R.REVIEW_WORDS.search(text):
                    where = ("in running text" if len(text) > 42
                             else "a label the table doesn't cover")
                    notes.append(("relabel_review", f"{text[:70]!r} ({where})"))
                continue
            if declared and R.kept_on_declaration(text, new):
                notes.append(("relabel_left_declaration",
                              f"{text!r} left as the form has it - on a declaration, "
                              f"who signs and who witnesses is the law's to say"))
                continue
            if text.endswith(":") and not new.endswith(":"):
                new += ":"
            # set in capitals if most of the label's words were
            words = [w for w in text.split() if any(c.isalpha() for c in w)]
            if words and sum(w.upper() == w for w in words) * 2 > len(words):
                new = new.upper()
            if new == text:
                continue
            notes.append(("relabelled", f"{text!r} -> {new!r}"))
            item["swap"] = len(before) + 1 if before is not None else None
            if before is not None:
                # numbered, so the proof can put the same number on the old
                # wording and on what replaced it
                before.append({"page": page_no, "rect": fitz.Rect(item["rect"]),
                               "n": item["swap"], "was": text, "now": new})
            item["was"] = text
            item["text"] = new
            item["runs"] = None
    return rows


# "Page 1 of 2", "Page 3", "- 4 -": a page number the form printed at the
# foot (or the head) of its own sheet. The rebuild re-paginates, so carrying one over
# states a page count that is no longer true.
PAGE_FOOT = re.compile(r"^(?:-\s*)?(?:page\s*)?\d+(?:\s*(?:of|/)\s*\d+)?"
                       r"(?:\s*-)?$", re.I)


def footer_lines(doc):
    """Each page's footer lines, as the restyle finds them: in the bottom
    tenth of the sheet and repeated there on another page, or reading like a
    revision or copyright line ("EST 107 (06/15)", "LIRA-ON Rev. (04/22)").
    {form page: [(box, size, x of the first letter)]}"""
    ns = SimpleNamespace(target_font="Arial", size=10.0, size_tolerance=0.6)
    found_all = R.find_fine_print(doc, ns)
    # the same words at the foot of another page are the same footer, even
    # set a little higher or lower there - the debit agreement's last page
    # prints its name 28pt lower than the first two do
    said = {" ".join(t.split()).lower() for found in found_all
            for kind, _, t in found.values() if kind == "footer"}
    out = {}
    for page, found in zip(doc, found_all):
        keys = {k for k, (kind, _, _) in found.items() if kind == "footer"}
        lines, _ = R.collect_lines(R.printed(page), False, "Arial", 10.0, 0.6)
        here = [(fitz.Rect(ln["bbox"]), max(sp["size"] for sp in ln["spans"]))
                for ln in lines if R.line_key(ln) in keys
                or (ln["bbox"].y0 >= page.rect.height * 0.9
                    and " ".join("".join(sp["text"] for sp in ln["spans"]).split()).lower() in said)]
        if here:
            out[page.number] = here
    return out


def row_sig(row):
    """What a row says, for telling the same row on another page."""
    return tuple((i["what"], " ".join(i["text"].split()) if i["what"] == "text"
                  else i.get("name")) for i in row)


def running_block(doc, read):
    """The rows a form repeats at the top of its pages after the first: how
    many, and on which pages. None where there is no such block.

    Only rows in the top third of the page, the same text and the same field
    names, opening at least two pages and most of them."""
    later = [p for p in doc if p.number > 0]
    if len(later) < 2:
        return None
    first = read[later[0].number]["rows"]
    best = None
    for k in range(1, min(12, len(first)) + 1):
        want = [row_sig(r) for r in first[:k]]
        if any(max(i["rect"].y1 for i in r) > later[0].rect.height / 3 for r in first[:k]):
            break
        on = [p.number for p in later
              if [row_sig(r) for r in read[p.number]["rows"][:k]] == want]
        if len(on) >= max(2, 0.6 * len(later)):
            best = (k, on)
        else:
            break
    return best


def calc_names(doc):
    """The form's calculation order, as field names."""
    kind, val = doc.xref_get_key(doc.pdf_catalog(), "AcroForm/CO")
    if kind != "array":
        return []
    named = {}
    for p in doc:
        for w in p.widgets():
            named[w.xref] = w.field_name
            k, parent = doc.xref_get_key(w.xref, "Parent")
            if k == "xref":
                named[int(parent.split()[0])] = w.field_name
    return [named.get(int(x)) for x in re.findall(r"(\d+) 0 R", val)]


def keep_calc_order(want, out):
    """Put the rebuilt form's calculations back in the form's own order. A
    field that adds up others has to be worked out after them; the rebuild
    makes fields in the order it lays them out, and the order the new file
    lists its calculations in followed that."""
    want = [n for n in want if n]
    kind, val = out.xref_get_key(out.pdf_catalog(), "AcroForm/CO")
    if not want or kind != "array":
        return False
    have = {}
    for p in out:
        for w in p.widgets():
            if w.script_calc:
                have.setdefault(w.field_name, []).append(w.xref)
    order = [x for n in want for x in have.pop(n, [])]
    order += [x for xs in have.values() for x in xs]
    if order and order != [int(x) for x in re.findall(r"(\d+) 0 R", val)]:
        out.xref_set_key(out.pdf_catalog(), "AcroForm/CO",
                         "[" + " ".join(f"{x} 0 R" for x in order) + "]")
        return True
    return False


def page_numbers(row, page):
    """The items in this row that are the page number the form printed at
    its own foot.

    Only those items: a foot line often carries the form's revision code
    beside the number ("EST120 (01/26)   Page 2 of 3"), and that is worth
    keeping. The number is not - the rebuild re-paginates, so two of them
    can end up on one sheet saying different things."""
    # at the foot of the sheet, or at its head: Turkey's security agreement
    # prints "Page 1 of 2" in the top corner of the form's third page, and
    # carried over it landed in the middle of a rebuilt page
    low, high = page.rect.height * 0.88, page.rect.height * 0.08
    if not row or (min(i["rect"].y0 for i in row) < low
                   and max(i["rect"].y1 for i in row) > high):
        return []
    # A number set in bold - "Page 1 of 3" with the digits heavier - reads
    # as several items, and item by item only the digits match, leaving
    # "Page of" behind. Runs of neighbouring items are tried together.
    texts = sorted((i for i in row if i["what"] == "text"), key=lambda i: i["rect"].x0)
    found = set()
    for a in range(len(texts)):
        for b in range(a, min(a + 5, len(texts))):
            joined = " ".join(t["text"].strip() for t in texts[a:b + 1])
            if PAGE_FOOT.match(joined):
                found.update(id(t) for t in texts[a:b + 1])
    return [i for i in texts if id(i) in found]


# "(a)", "A.", "12.", "iv)", and the "1.1" / "18.1" of a numbered
# agreement: the start of a new item in a list, which is never the rest
# of the sentence above it, whatever follows the marker.
LIST_MARK = re.compile(r"^(?:\(?[a-zA-Z0-9]{1,3}[.)](?![\w.)])"
                       r"|\d{1,3}(?:\.\d{1,3})+\.?$)")


GRID_TOL = 2.5


def snap(values, tol):
    """Collapse values that are the same line drawn twice. A form rules a
    table border as two hairlines a few points apart, and read literally
    that is a column six points wide with nothing in it."""
    out = []
    for v in sorted(values):
        if out and v - out[-1][-1] <= tol:
            out[-1].append(v)
        else:
            out.append([v])
    return [sum(g) / len(g) for g in out]


def rules_of(page, merged=True):
    """Every straight line the page draws, merged, as horizontals and
    verticals: (at, from, to). Unmerged, each piece as drawn."""
    hs, vs = [], []
    for d in R.printed(page).get_drawings():
        # white on white is no line at all (see R.upright_edges)
        ink = d.get("color") if d["type"] in ("s", "fs") else None
        if ((ink is None or min(ink) > 0.95)
                and (d["type"] == "s" or not d.get("fill") or min(d["fill"]) > 0.95)):
            continue
        for it in d["items"]:
            if it[0] == "l":
                a, b = it[1], it[2]
                if abs(a.y - b.y) <= 1 and abs(a.x - b.x) > 2:
                    hs.append((round((a.y + b.y) / 2, 1), min(a.x, b.x), max(a.x, b.x)))
                elif abs(a.x - b.x) <= 1 and abs(a.y - b.y) > 2:
                    vs.append((round((a.x + b.x) / 2, 1), min(a.y, b.y), max(a.y, b.y)))
            elif it[0] == "re":
                r = fitz.Rect(it[1])
                if r.height <= 1.5 and r.width > 2:
                    hs.append((round((r.y0 + r.y1) / 2, 1), r.x0, r.x1))
                elif r.width <= 1.5 and r.height > 2:
                    vs.append((round((r.x0 + r.x1) / 2, 1), r.y0, r.y1))
                elif r.width > 2 and r.height > 2:
                    if d.get("fill") and r.height > 4 and r.width > 40:
                        continue       # a shaded section bar, not a ruled box
                    hs += [(round(r.y0, 1), r.x0, r.x1), (round(r.y1, 1), r.x0, r.x1)]
                    vs += [(round(r.x0, 1), r.y0, r.y1), (round(r.x1, 1), r.y0, r.y1)]

    def run(segs):
        out = []
        for at, lo, hi in sorted(segs):
            # they must meet, not merely start before the last one ends: a
            # short upright 1.4pt off the column, sorted after the long ones,
            # joined TFSA Designation's three separate tables into one
            if (out and abs(out[-1][0] - at) <= GRID_TOL and lo <= out[-1][2] + GRID_TOL
                    and hi >= out[-1][1] - GRID_TOL):
                a, b, c = out[-1]
                out[-1] = (a, min(b, lo), max(c, hi))
            else:
                out.append((at, lo, hi))
        return out

    return (run(hs), run(vs)) if merged else (hs, vs)


def form_grids(page, min_bands=1):
    """The tables the form drew, read from its own lines.

    A table cannot be carried across a re-flow - its rows move and spread, so
    the lines end up round the wrong things - and it cannot be guessed from
    the shape of a row either, because a row of a table looks like any other
    row. What can be done is read the lattice the form ruled: which lines
    cross which, where the rows are, and which cuts reach across each row.
    The table is then drawn again around the content at its new height."""
    hs, vs = rules_of(page)
    hs = [h for h in hs if h[2] - h[1] > 40]
    vs = [v for v in vs if v[2] - v[1] > 8]
    if not hs or not vs:
        return []
    used, found = set(), []
    for i in range(len(hs)):
        if i in used:
            continue
        band, queue, crossing = {i}, [i], set()
        while queue:
            y, x0, x1 = hs[queue.pop()]
            for k, (x, ya, yb) in enumerate(vs):
                if x0 - GRID_TOL <= x <= x1 + GRID_TOL and ya - GRID_TOL <= y <= yb + GRID_TOL:
                    crossing.add(k)
            for k, (y2, a, b) in enumerate(hs):
                if k in band:
                    continue
                if any(a - GRID_TOL <= vs[c][0] <= b + GRID_TOL
                       and vs[c][1] - GRID_TOL <= y2 <= vs[c][2] + GRID_TOL
                       for c in crossing):
                    band.add(k)
                    queue.append(k)
        used |= band
        if not crossing:
            continue
        edges = snap([hs[k][0] for k in band], 6.0)
        if len(edges) < min_bands + 1:
            continue
        # A box of one row is still a box - the collateral box on a security
        # agreement is two rules and two sides. Guard against reading a lone
        # underline with something crossing it as a table: the sides have to
        # reach the whole way down.
        if len(edges) == 2:
            tall = [c for c in crossing
                    if vs[c][1] - 6 <= edges[0] and vs[c][2] + 6 >= edges[-1]]
            if len(tall) < 2 or edges[-1] - edges[0] < 6:
                continue
        left = min(hs[k][1] for k in band)
        right = max(hs[k][2] for k in band)
        bands = []
        for top, foot in zip(edges, edges[1:]):
            cuts = snap([v[0] for v in (vs[c] for c in crossing)
                         if v[1] - 6 <= top and v[2] + 6 >= foot], 9.0)
            cuts = [c for c in cuts if left + 4 < c < right - 4]
            bands.append((top, foot, [left] + cuts + [right]))
        found.append({"box": fitz.Rect(left, edges[0], right, edges[-1]),
                      "bands": bands})
    return found


def is_table_row(row):
    """A row that reads label, box, label, box ... is a table row: the
    beneficiary grids are built that way, and they have to come back as a
    grid rather than a line of labels with rules trailing after it."""
    kinds = [i["what"] for i in row]
    # A sentence with blanks in it reads the same way - "Safe Deposit Box #
    # ___ was accessed on the ___ day of ___, 20__" - and boxed up as a grid
    # it came back as a table (Est171). Its pieces run on: they start in lower
    # case or with punctuation, where a table's labels do not.
    runs_on = any(i["what"] == "text" and i["text"].strip()[:1]
                  and (i["text"].strip()[0].islower() or i["text"].strip()[0] in ",.;:")
                  for i in row[1:])
    return (len(row) >= 6 and kinds[0] == "text"        # a cell starts with its label
            and kinds.count("field") >= 3 and kinds.count("text") >= 3
            and all(a != b for a, b in zip(kinds, kinds[1:]))
            and not runs_on)


def ran_out_of_room(line, nxt, right_edge, slack=6.0):
    """Did this line break because it had no room for the next word?

    A paragraph breaks where the next word will not fit; everything else -
    an address, a signature block, a heading, a list of names - breaks
    because the form meant it to. Telling those apart by how long the line
    is does not work, because a right-aligned address block reaches the
    right margin too. Asking whether the next word would have fitted does:
    "217 York Street, 4th Floor" had room for "London" and did not take it,
    so it was never a paragraph."""
    words = nxt["text"].split()
    if not words:
        return True
    size = line.get("size", 10.0) or 10.0
    font = ARIAL or R.resolve_fonts({})["regular"][2]
    # where its LAST line ends: a paragraph already joined up has a box as
    # wide as its longest line, and measured from that, TFSA's "... written
    # direction." looked full and the next paragraph ran on into it
    end = (line.get("last_x1", line["rect"].x1) + 0.3 * size
           + font.text_length(words[0], size) + slack)
    return end > right_edge


def merge_prose(rows, right_edge=10 ** 6):
    """Join rows that are a single run of text into paragraphs, so the
    rebuild wraps them to its own column instead of keeping the original's
    line breaks.

    Two lines are one paragraph only when they start in the same place and
    the first ran out of room. Without those tests an address comes back as
    one long line and a list item's second line is stranded on its own."""
    def item_indent(row):
        """Where a list item's own words start, if this row starts one.

        The item can be any shape - "(a) text", "3. text ___ day of ___" -
        so what matters is the marker at the front and where the words after
        it begin. A continuation line sits at that same indent."""
        spots = sorted(row, key=lambda i: i["rect"].x0)
        if len(spots) < 2:
            return None
        head = spots[0]
        if head["what"] == "field":
            if head.get("kind") not in ("CheckBox", "RadioButton"):
                return None
        elif head["what"] == "text":
            mark = head["text"].strip()
            # or a run-in label with the words set beside it: Hatching Egg's
            # "NOTE:  Nothing in this Direction ... / including O.B.H.E.C.C.'s
            # authority to cancel quota." came back as two paragraphs
            lead_in = mark.endswith(":") and len(mark) <= 12 and len(mark.split()) <= 2
            # and a bare clause number set apart by a tab ("1   As further
            # consideration ...")
            bare = (re.fullmatch(r"\d{1,2}", mark) is not None and len(spots) > 1
                    and spots[1]["rect"].x0 - head["rect"].x1 >= 6)
            # ("o" and "○": Word's second-level bullet - Transfer In's wire
            #  page ran its three sub-items together into one line)
            if not (mark in ("•", "●", "-", "*", "o", "○", "◦", "▪", "■")
                    or LIST_MARK.match(mark) or lead_in or bare):
                return None
        else:
            return None
        words = [i for i in spots[1:] if i["what"] == "text"]
        if len(words) != 1:
            # The item's words are broken up by blanks it has to be filled in
            # on - "3. THAT ... ___ day of ___, a notarial copy". Joining a
            # following line onto the last of those runs is right about the
            # sentence and wrong about the page: the run sits mid-row in a
            # narrow slot, and the rest of the sentence wraps into a column
            # there. Such a line is left where the form put it.
            return None
        return words[0]["rect"].x0

    def last_words(row):
        return next((i for i in sorted(row, key=lambda i: i["rect"].x0, reverse=True)
                     if i["what"] == "text"), None)

    # How far the lines that start where this one starts actually reach.
    # A form's text column is often much narrower than the page, and asking
    # whether a line ran out of room has to mean room in its own column: an
    # FHSA paragraph set to 500pt never reaches a 576pt page edge, so every
    # line of it looks deliberate and nothing joins up.
    ends, edges = {}, {}
    for row in rows:
        if len(row) == 1 and row[0]["what"] == "text":
            ends.setdefault(round(row[0]["rect"].x0), []).append(row[0]["rect"].x1)
    for k, xs in ends.items():
        top = max(xs)
        # A wrapped column has several lines finishing flush with each other.
        # A block the form broke by hand is ragged, and its longest line is
        # not an edge at all - treating it as one joins a right-aligned
        # address into a single line.
        if sum(1 for x in xs if top - x <= 2.0) >= 2:
            edges[k] = top

    def carries_on(a, b, same_start=True, column=None, fallback=None):
        """Is b the rest of a's sentence rather than a new line of its own?

        `column` says which block's right edge to measure room against, for
        a line that ends mid-row - the last words of "3. THAT ... ___ day of
        ___, a notarial copy" start nowhere near the item's own margin."""
        # A line that opens in lower case, after one that did not end its
        # sentence, is that sentence going on - whatever room the line above
        # had. "...signed paperwork is returned" / "to Libro Credit Union" is
        # one sentence the form broke by hand, and rebuilt as two it came out
        # as three lines with "returned" stranded on one of its own.
        head = b["text"].lstrip()[:1]
        if (head.isalpha() and head.islower()
                and a["text"].rstrip()[-1:] not in ".:;!?"
                and abs(a["size"] - b["size"]) < 0.6
                and a["bold"] == b["bold"] and a["italic"] == b["italic"]
                and 0 < b["base"] - a["base"] < 2.2 * b["size"]
                and (not same_start or abs(a["rect"].x0 - b["rect"].x0) <= 2.5)
                and not LIST_MARK.match(b["text"].lstrip())
                # (Word's second-level bullet, an "o", is not lower case)
                and not re.match(r"o\s", b["text"].lstrip())):
            return True
        reach = edges.get(round(column if column is not None else a["rect"].x0))
        if reach is None and fallback is not None:
            # A list item is often one line long, so it never finishes flush
            # with anything and has no column edge of its own. That is not a
            # reason to strand its second line - the marker before it already
            # says what it is.
            reach = fallback
        if (reach is None and a["bold"] and b["bold"] and not a["italic"]
                and abs(a["rect"].x0 - b["rect"].x0) <= 2.5
                and a["text"].rstrip()[-1:].isalpha()
                and abs(a["size"] - b["size"]) < 0.6
                and 0 < b["base"] - a["base"] < 2.2 * b["size"]
                and a["rect"].width >= 0.6 * (right_edge - a["rect"].x0)):
            # A bold sentence the form ran over two lines: "This Declaration
            # must be sworn ... If necessary, attach" / "Form EST-130 for
            # additional declarations." The first line runs most of the way
            # across and stops mid-sentence; an address or a list never does.
            return True
        if reach is None:
            # nothing that starts here finishes flush with anything else, so
            # this is not a wrapped column at all - every break in it was the
            # form's own
            return False
        reach = min(reach, right_edge)
        return (abs(a["size"] - b["size"]) < 0.6
                and a["bold"] == b["bold"] and a["italic"] == b["italic"]
                and 0 < b["base"] - a["base"] < 2.2 * b["size"]
                and (not same_start or abs(a["rect"].x0 - b["rect"].x0) <= 2.5)
                and not a["text"].rstrip().endswith(":")
                and not LIST_MARK.match(b["text"].lstrip())
                and b["text"].lstrip()[:1] not in ("•", "●", "○", "◦", "▪", "■")
                # Word's second-level bullet is a plain "o" and a space
                and not re.match(r"o\s", b["text"].lstrip())
                and ran_out_of_room(a, b, reach))

    def join(a, b):
        out = dict(a)
        out["text"] = a["text"].rstrip() + " " + b["text"].lstrip()
        out["runs"] = join_runs(a, b)
        out["base"] = b["base"]
        out["rect"] = a["rect"] | b["rect"]
        out["last_x1"] = b.get("last_x1", b["rect"].x1)
        return out

    def hangs(a, b):
        """Is b the next line of a list item set with a hanging indent - the
        marker and its words one run, "a. all assets acquired by your Fund
        are and continue to be Qualified", and the rest of the sentence set
        in under the words, not under the marker? Where its words start is
        worked out from the marker's width."""
        head = a["text"].lstrip()
        m = LIST_MARK.match(head)
        if not m or len(head.split()) < 2:
            return False
        mark = head.split()[0]
        font = ARIAL or R.resolve_fonts({})["regular"][2]
        size = a.get("size", 10.0) or 10.0
        words_at = a["rect"].x0 + font.text_length(mark + " ", size)
        # the gap after the marker can be a tab: anywhere from a space to a
        # couple of spaces beyond where the marker's words would start
        return words_at - 2.5 <= b["rect"].x0 <= words_at + 3 * size

    out = []
    for row in rows:
        plain = len(row) == 1 and row[0]["what"] == "text"
        last = out[-1] if out else None
        indent = item_indent(last) if last else None
        tail = last_words(last) if last else None
        lower = row[0]["what"] == "text" and row[0]["text"].lstrip()[:1].islower()
        if (plain and indent is not None and tail is not None
                and (abs(row[0]["rect"].x0 - indent) <= 2.5
                     # a line opening in lower case is the sentence going on,
                     # even set a little off the indent, as Hatching Egg's is
                     or (lower and abs(row[0]["rect"].x0 - indent) <= 24))
                and carries_on(tail, row[0], same_start=False, column=indent,
                               # with no flush edge to go by, the furthest any
                               # line at this indent reaches: measured against
                               # the page edge, TFSA Designation's NOTE bullet
                               # had "room" for a word its column did not
                               fallback=max(ends.get(round(indent), [right_edge])
                                            + [tail["rect"].x1]))):
            # the rest of the item's sentence, which the form broke across
            # lines; it belongs to the item, indent and all. The item keeps
            # its own left edge: TFSA's "The holder hereby designates ..."
            # runs on under its tick box, and taking that line's edge put the
            # words in front of the box - the box went to the end of the line
            # and the item's next line no longer read as part of it.
            held = join(tail, row[0])
            held["rect"] = fitz.Rect(tail["rect"].x0, held["rect"].y0,
                                     held["rect"].x1, held["rect"].y1)
            out[-1] = [j if j is not tail else held for j in last]
        elif (plain and last and len(last) == 1 and last[0]["what"] == "text"
                and carries_on(last[0], row[0])):
            out[-1] = [join(last[0], row[0])]
        elif (plain and last and len(last) == 1 and last[0]["what"] == "text"
                and hangs(last[0], row[0])
                and carries_on(last[0], row[0], same_start=False,
                               column=last[0]["rect"].x0, fallback=right_edge)):
            # the rest of a list item set with a hanging indent
            out[-1] = [join(last[0], row[0])]
        else:
            out.append(row)
    return out


UNFOLDED = []         # pages whose two columns of terms were read as one
SIGNING_DROPPED = []  # (name, kind) of fields the standard signing block took out
DASHES_DROPPED = []   # dashed lines typed under fields, drawn as the fields' lines


def column_region(rows, width):
    """A run of rows set as two columns of running text: (first row, last
    row, gutter x0, gutter x1), or None.

    The gutter is a band down the middle of the page that nothing in those
    rows crosses; the rows are prose - nearly all text, a good many lines on
    each side running most of their column's width - which is what tells a
    column of terms from a form's label-and-blank rows, which leave the
    middle of the page clear too."""
    best = None
    for x in range(int(width * 0.3), int(width * 0.7), 2):
        k = 0
        while k < len(rows):
            if any(i["rect"].x0 < x - 1 and i["rect"].x1 > x + 1 for i in rows[k]):
                k += 1
                continue
            j = k
            while (j + 1 < len(rows) and not any(
                    i["rect"].x0 < x - 1 and i["rect"].x1 > x + 1 for i in rows[j + 1])):
                j += 1
            run = rows[k:j + 1]
            both = sum(1 for r in run if any(i["rect"].x1 <= x for i in r)
                       and any(i["rect"].x0 >= x for i in r))
            if both >= 6 and (best is None or both > best[0]):
                best = (both, k, j, x)
            k = j + 1
    if best is None:
        return None
    _, k, j, x = best
    # The columns start at their first row with words on both sides. What
    # comes before - a page's own title beside its logo - is not part of
    # either column, and read into them it lost its place at the top.
    while k < j and not (any(i["rect"].x1 <= x for i in rows[k])
                         and any(i["rect"].x0 >= x for i in rows[k])):
        k += 1
    items = [i for r in rows[k:j + 1] for i in r]
    # Where the right column starts is where most of its lines start. Its
    # item numbers hang out in front of that - "11." sits 16pt left of its
    # words on the debit agreement - and a split taken at the widest gap fell
    # between the numbers and their words, leaving the numbers stranded in
    # the left column.
    starts = Counter(round(i["rect"].x0) for i in items
                     if i["what"] == "text" and i["rect"].x0 >= x - 40
                     and len(i["text"].split()) > 2)
    if starts:
        col = starts.most_common(1)[0][0]
        cut = min(x, col - 22)
        if not any(i["rect"].x0 < cut < i["rect"].x1 for i in items):
            x = cut
    left = [i for i in items if i["rect"].x1 <= x]
    right = [i for i in items if i["rect"].x0 >= x]
    if not left or not right:
        return None
    g0 = max(i["rect"].x1 for i in left)
    g1 = min(i["rect"].x0 for i in right)
    if g1 - g0 < 6:
        return None
    if sum(1 for i in items if i["what"] == "text") < 0.85 * len(items):
        return None
    for side, lo, hi in ((left, min(i["rect"].x0 for i in left), g0),
                         (right, g1, max(i["rect"].x1 for i in right))):
        long = sum(1 for i in side if i["what"] == "text"
                   and i["rect"].width >= 0.5 * (hi - lo))
        if long < 5:
            return None
    return k, j, x, g1


def rejoin(row):
    """A justified line the extractor read as words set far apart - the wide
    spaces justification leaves look like tab stops - put back together as
    the one line it is. A list marker stays apart from its words."""
    out = []
    for i in sorted(row, key=lambda i: i["rect"].x0):
        last = out[-1] if out else None
        if (last is not None and last["what"] == "text" and i["what"] == "text"
                and i["rect"].x0 - last["rect"].x1 <= 1.6 * i["size"]
                and abs(last["size"] - i["size"]) < 0.6
                and last["bold"] == i["bold"] and last["italic"] == i["italic"]
                and not (len(last["text"].split()) == 1
                         and (LIST_MARK.match(last["text"].strip())
                              or last["text"].strip() in ("•", "●", "-")))):
            out[-1] = dict(last, text=last["text"].rstrip() + " " + i["text"].lstrip(),
                           rect=last["rect"] | i["rect"], runs=join_runs(last, i))
            continue
        out.append(i)
    return out


def unfold_columns(rows, page, feet=()):
    """Two columns of terms, read the way they are meant to be read: the
    left column top to bottom, then the right one.

    Row by row, a line of the left column and the line level with it in the
    right were one row, and at 10pt each wrapped on its own in half the
    width - the RRIF, RRSP and pre-authorized debit terms came out as two
    columns of fragments interleaved down the page. Read as one column, in
    order, the paragraphs join up again and are set across the width."""
    out, rest, n = [], list(rows), 0
    while n < 3:
        found = column_region(rest, page.rect.width)
        if found is None:
            break
        k, j, cut, g1 = found
        mid = cut
        run = rest[k:j + 1]
        # the page's foot - its form code, its page number - is not the end
        # of the left column: left there, it sat between the last words of
        # one column and the first of the next, and the sentence they share
        # stayed broken
        def footing(r):
            return all(any(fitz.Point((i["rect"].x0 + i["rect"].x1) / 2,
                                      (i["rect"].y0 + i["rect"].y1) / 2) in b + (-2, -2, 2, 2)
                           for b, _ in feet) for i in r)
        foot = [r for r in run if footing(r)]
        run = [r for r in run if not footing(r)]
        lefts = [[i for i in r if i["rect"].x1 <= mid] for r in run]
        rights = [[i for i in r if i["rect"].x0 >= mid] for r in run]
        l0 = min(i["rect"].x0 for r in lefts for i in r)
        r0 = min(i["rect"].x0 for r in rights for i in r)
        dx = l0 - r0
        # The right column goes under the left one, down as well as across:
        # kept at its own height it reads as coming before the left column's
        # last lines, and a sentence the form carried over from the foot of
        # one column to the head of the next stayed broken in two.
        bases = sorted(i["base"] for r in lefts for i in r)
        steps = [b - a for a, b in zip(bases, bases[1:]) if 2 < b - a < 20]
        pitch = sorted(steps)[len(steps) // 2] if steps else 12.0
        dy = (bases[-1] + pitch) - min(i["base"] for r in rights for i in r)
        moved = []
        for r in rights:
            if r:
                moved.append([dict(i, base=i["base"] + dy,
                                   rect=fitz.Rect(i["rect"].x0 + dx, i["rect"].y0 + dy,
                                                  i["rect"].x1 + dx, i["rect"].y1 + dy))
                              for i in r])
        body = [rejoin(r) for r in lefts if r] + [rejoin(r) for r in moved]
        # A section number the form set a few points above its heading -
        # "4." and then "Record Keeping" - is read as a row of its own and
        # stood alone on a line. It goes back beside its heading.
        tied = []
        for r in body:
            last = tied[-1] if tied else None
            if (last is not None and len(last) == 1 and last[0]["what"] == "text"
                    and len(last[0]["text"].split()) == 1
                    and LIST_MARK.match(last[0]["text"].strip())
                    and r[0]["rect"].x0 > last[0]["rect"].x1
                    and 0 < r[0]["base"] - last[0]["base"] < 1.6 * r[0]["size"]):
                tied[-1] = [dict(last[0], base=r[0]["base"])] + r
                continue
            tied.append(r)
        body = tied
        UNFOLDED.append((page.number, len(run), fitz.Rect(l0, min(i["rect"].y0 for r in run for i in r),
                                                          max(i["rect"].x1 for r in run for i in r),
                                                          max(i["rect"].y1 for r in run for i in r))))
        out += rest[:k] + body + foot
        rest = rest[j + 1:]
        n += 1
    return out + rest


def is_rule_row(row):
    """A row that is only places to write: fields, or the underscores a form
    types to draw a line."""
    return bool(row) and all(
        i["what"] == "field" or not i["text"].strip("_ ") for i in row)


def slot_count(row):
    """How many places to write a row really has: a field and the underscores
    drawn under it are one place, not two."""
    spots = []
    for item in sorted(row, key=lambda i: i["rect"].x0):
        if not spots or item["rect"].x0 - spots[-1] >= 12:
            spots.append(item["rect"].x0)
    return len(spots)


def is_caption_row(row, slots):
    """The short labels that sit under a row of lines - "Checked By",
    "Entered By", "Date Entered"."""
    return (all(i["what"] == "text" for i in row) and len(row) == slots
            and all(len(i["text"]) < 90 for i in row))


class KeepShape:
    """Set the form out again at the target size, keeping its shape.

    Every label stays in the column it was in, every box stays beside the
    label it belonged to, and the page keeps its arrangement left to right.
    All that changes is downward: bigger type needs taller rows, and where a
    label no longer fits the space between it and its neighbour it wraps
    inside that space rather than pushing the neighbour along. The page grows
    as it must, and runs onto another when it is full."""

    def __init__(self, doc, fonts, size, page_rect, top=40.0, bottom=40.0):
        self.doc, self.fonts, self.size = doc, fonts, size
        self.rect, self.top, self.bottom = page_rect, top, bottom
        self.fields = []
        self.said = Counter()    # every word the rebuild has drawn
        self.swapped = []        # radio buttons that came back as tick boxes
        self.dropped = []        # line art that could not be brought across
        self.drew = Counter()    # blanks drawn, by kind: fields and typed rules
        self.slid = []           # (band top, band foot, [(was x0, was x1, is x0)], form page)
        self.swaps = []          # (page, box) for wording the table replaced
        self.ink = {}            # page -> boxes of the words already set on it
        self.spare = 0.0         # the gap above the current row, reclaimable
        self.origin = {}         # rebuilt page -> the page of the form it came from
        self.source = 0
        self.moved = []          # (baseline it had, baseline it has, which page, form page)
        self.clear = {}          # page -> images set there that rows keep clear of
        self.heads = []          # (pixmap, rect): a letterhead every sheet carries
        self.handled = set()     # images of the form placed some other way
        self.placed = 0          # images set on the rebuilt pages, all told
        self.image_log = Counter()   # how each image of the form was placed
        self.lost_images = []    # images of the form that could not be placed
        self.running = []        # (rows, right edge): a block every later sheet opens with
        self.running_on = set()  # the form's pages that carried that block
        self.running_placed = 0  # how many sheets it was set on
        self.quiet = False       # rows being set that the form's art must not follow
        self.page = None
        self.new_page()

    def new_page(self):
        self.page = self.doc.new_page(width=self.rect.width, height=self.rect.height)
        self.y = self.top
        # A form that prints its letterhead on every sheet gets it on every
        # rebuilt sheet too, and the page's content starts below it.
        if self.heads and self.doc.page_count > 1:
            for pix, rect in self.heads:
                self.reserve_image(pix, rect)
                self.y = max(self.y, rect.y1 + 8)
        # A block the form prints at the top of every page - LIRA's contract
        # details, "Branch/Owner#", "Name of Annuitant", with their fields -
        # opens every sheet its pages go onto, as it opened every page.
        if (self.running and self.doc.page_count > 1 and not self.quiet
                and self.source in self.running_on):
            self.quiet = True
            try:
                for rows, right_edge in self.running:
                    for row in rows:
                        self.place([dict(i) for i in row], right_edge)
                self.y += self.size
                self.running_placed += 1
            finally:
                self.quiet = False

    def reserve_image(self, pix, rect):
        """Set an image on the sheet in hand and keep the rows off it."""
        self.page.insert_image(rect, pixmap=pix, overlay=True)
        self.clear.setdefault(self.doc.page_count - 1, []).append(fitz.Rect(rect))
        self.placed += 1

    def clear_of(self, height, span=None):
        """Move the row about to be set below any image it would run into.

        Only a row that actually overlaps an image across the page moves: the
        title a form sets beside its logo stays beside it, and the first row
        that reaches under the logo starts below it."""
        x0, x1 = span or (0.0, self.rect.width)
        moved = False
        for r in self.clear.get(self.doc.page_count - 1, ()):
            if (r.x0 < x1 and x0 < r.x1
                    and self.y < r.y1 + 2 and self.y + height > r.y0 - 2):
                self.y = r.y1 + 4
                moved = True
        return moved

    def face(self, bold, italic):
        return self.fonts["bolditalic" if bold and italic else "bold" if bold
                          else "italic" if italic else "regular"]

    def put(self, item, x, y, piece, face, size=None):
        """Set one line of an item at (x, y): in its one face, or - where its
        words were set in different weights - word by word, each in its own.
        The line was measured in the item's face, so where the words' own
        faces come out wider (a bold heading in a regular sentence) the line
        is narrowed to the width it was given rather than run past it."""
        size = size or self.size
        alias, path, font = face
        ink = (item or {}).get("colour") or (0, 0, 0)
        styles = self.styles_for(item, piece) if item is not None else None
        if not styles:
            self.page.insert_text(fitz.Point(x, y), piece, fontname=alias,
                                  fontfile=path, fontsize=size, color=ink)
            return
        words = piece.split()
        faces = [self.face(b, i) for b, i, _u, _c in styles]
        space = font.text_length(" ", size)
        total = (sum(f[2].text_length(w, size) for f, w in zip(faces, words))
                 + space * (len(words) - 1))
        want = font.text_length(" ".join(words), size)
        squeeze = min(1.0, want / total) if total > 0 else 1.0
        cx, ends = x, []
        for k, (f, w) in enumerate(zip(faces, words)):
            wink = styles[k][3] or (0, 0, 0)
            # each word carries its space, so the text layer still reads as
            # words: drawn bare, "□ Direction" was extracted as "□Direction"
            shown = w + " " if k < len(words) - 1 else w
            if squeeze < 0.999:
                self.page.insert_text(fitz.Point(cx, y), shown, fontname=f[0], fontfile=f[1],
                                      fontsize=size, color=wink,
                                      morph=(fitz.Point(cx, y), fitz.Matrix(squeeze, 1)))
            else:
                self.page.insert_text(fitz.Point(cx, y), shown, fontname=f[0], fontfile=f[1],
                                      fontsize=size, color=wink)
            ends.append((cx, cx + f[2].text_length(w, size) * squeeze))
            cx += (f[2].text_length(w, size) + space) * squeeze
        # underlined words, a run of them under one line, as Word draws it
        k = 0
        while k < len(words):
            if not styles[k][2]:
                k += 1
                continue
            j = k
            while j + 1 < len(words) and styles[j + 1][2]:
                j += 1
            self.page.draw_line(fitz.Point(ends[k][0], y + 1.5),
                                fitz.Point(ends[j][1], y + 1.5),
                                color=styles[k][3] or (0, 0, 0), width=0.6)
            k = j + 1

    def styles_for(self, item, piece):
        """The (bold, italic) of each word of `piece`, a line of `item`, or
        None to set it in one face. Lines are found in the item's words in the
        order they are drawn; a piece split off an item (a label cut at its
        tick box) is found wherever its words are."""
        runs = item.get("runs")
        words = piece.split()
        if not runs or not words:
            return None
        cursors = self.__dict__.setdefault("_run_at", {})
        names = [r[0] for r in runs]
        at = cursors.get(id(item), 0)
        if names[at:at + len(words)] != words:
            at = next((k for k in range(len(names) - len(words) + 1)
                       if names[k:k + len(words)] == words), None)
            if at is None:
                return None
        cursors[id(item)] = at + len(words)
        styles = [(b, i, u, c) for _w, b, i, u, c in runs[at:at + len(words)]]
        plain = (item["bold"], item["italic"], False, item.get("colour"))
        return styles if (len(set(styles)) > 1 or styles[0] != plain) else None

    def strip_item(self, texts, bold, italic):
        """A table strip's labels as one item, for put(): the strip is set in
        one face, and its words keep their own."""
        runs = [r for t in texts for r in runs_of(t)]
        common = texts[0].get("colour") if texts and len(
            {t.get("colour") for t in texts}) == 1 else None
        return {"runs": runs if any((b, i, u, c) != (bold, italic, False, common)
                                    for _w, b, i, u, c in runs) else None,
                "bold": bold, "italic": italic,
                "colour": texts[0].get("colour") if texts and len(
                    {t.get("colour") for t in texts}) == 1 else None}

    def wrap(self, text, width, bold, italic, size):
        font = self.face(bold, italic)[2]
        out, line = [], ""
        for word in text.split():
            trial = (line + " " + word).strip()
            if font.text_length(trial, size) > width and line:
                out.append(line)
                line = word
            else:
                line = trial
        if line:
            out.append(line)
        return out or [""]

    def shown(self, item):
        """The label without the blank the form typed after it."""
        return item["text"].rstrip("_ ").rstrip() or item["text"]

    def plan(self, row, right_edge, floor_field=54.0):
        """Where each item in a row goes, how wide it is, and how many lines
        its text takes at the new size.

        Every item keeps the column it started in unless what comes before it
        has grown past that column - bigger type is wider, and something has
        to give. What gives first is the blanks, which are only places to
        write and can be shorter; only when the row still will not fit does a
        label wrap inside its own slot. That is what keeps "Owner Number:" on
        one line instead of breaking it in half over its own field."""
        spots = sorted(row, key=lambda i: i["rect"].x0)
        want, floor, pad = [], [], []
        for n, item in enumerate(spots):
            if item["what"] == "text":
                font = self.face(item["bold"], item["italic"])[2]
                text = self.shown(item)
                want.append(font.text_length(text, self.size) + 1)
                longest = max((font.text_length(w, self.size)
                               for w in text.split()), default=0.0)
                floor.append(min(want[-1], max(longest + 1, 24.0)))
            elif item.get("kind") in ("CheckBox", "RadioButton"):
                want.append(self.size * 0.9)
                floor.append(self.size * 0.9)
            else:
                want.append(item["rect"].width)
                floor.append(min(item["rect"].width, floor_field))
            after = (spots[n + 1]["rect"].x0 - item["rect"].x1
                     if n + 1 < len(spots) else 0.0)
            # never less than a comfortable space at the new size: at the old
            # size a 3pt gap was one, at 10pt it is not, and two labels that
            # close up read - and copy out - as one word
            pad.append(min(24.0, max(self.size * 0.42, after)))

        def anchor(n, widths):
            # a flush-right label is anchored by its right edge
            if spots[n].get("ralign") is not None and spots[n]["what"] == "text":
                return max(spots[n]["ralign"] - widths[n], spots[n]["rect"].x0 - 60, 36.0)
            return max(spots[n]["rect"].x0, spots[n].get("min_x", 0.0))

        def run(widths, anchored):
            xs, x = [], anchor(0, widths) if anchored else spots[0]["rect"].x0
            for n in range(len(spots)):
                if anchored:
                    x = max(x, anchor(n, widths))
                xs.append(x)
                x += widths[n] + pad[n]
            if anchored and xs[-1] + widths[-1] > right_edge:
                # The last items of a row set near the right edge may come
                # back from it, as far as the item before them allows: at the
                # new size "Member Number:" and "(the "Borrower")" no longer
                # fit where they started, and wrapped in two rather than move.
                # packed from the right: each item moves back only as far as
                # what follows it needs, and never past the margin
                # Only labels and blanks move: a sentence pulled back to
                # dodge a wrap took its list marker with it, and "3." / "4."
                # no longer lined up with the items above and below.
                def movable(item):
                    if item["what"] != "text":
                        return True
                    return len(item["text"].split()) <= 8
                packed, limit = list(xs), right_edge
                for n in range(len(spots) - 1, -1, -1):
                    packed[n] = min(xs[n], limit - widths[n])
                    if n:
                        limit = packed[n] - pad[n - 1]
                listed = any(i["what"] == "text" and LIST_MARK.match(i["text"].lstrip())
                             for i in spots)
                # and a row's first item stays on the left margin it shares
                # with the rows round it; only one set out on the right
                # ("Member Number:" by the page edge) comes back
                lead_moves = packed[0] < xs[0] - 0.01
                if lead_moves and spots[0]["rect"].x0 < 0.4 * self.rect.width:
                    packed = None
                # and only items that overflow from where they stood: one a
                # long caption pushed off its column (FHSA Designation's
                # "Applicant Signature") goes back by the caption wrapping
                pushed = any(xs[n] > spots[n]["rect"].x0 + 0.5 for n in range(len(spots))
                             if packed is not None and packed[n] < xs[n] - 0.01
                             and spots[n]["what"] == "text")
                if (packed is not None and not pushed
                        and packed[0] >= getattr(self, "left", 36.0) - 0.5 and not listed
                        and all(movable(spots[n]) for n in range(len(spots))
                                if packed[n] < xs[n] - 0.01)):
                    xs = packed
            return xs, xs[-1] + widths[-1]

        def relax(widths, anchored, blanks):
            """Give back width, widest first, until the row fits: from the
            blanks when blanks is set, from the labels when it is not."""
            xs, end = run(widths, anchored)
            for n in sorted(range(len(spots)),
                            key=lambda k: widths[k] - floor[k], reverse=True):
                if end <= right_edge:
                    break
                if (spots[n]["what"] == "text") == blanks:
                    continue
                widths[n] -= min(widths[n] - floor[n], end - right_edge)
                xs, end = run(widths, anchored)
            return widths

        # keep the columns where they were if the row still fits that way;
        # shorten the blanks next; only then let the row close up and flow,
        # and only after all of that does a label wrap
        fit = None
        # how short a blank may go before a label is broken instead: a blank
        # of 36pt still takes a date or a number, and "Member Number:" broken
        # over two lines beside a long blank reads as a mistake
        short = [min(w, 36.0) if spots[n]["what"] != "text"
                 and spots[n].get("kind") not in ("CheckBox", "RadioButton") else floor[n]
                 for n, w in enumerate(want)]
        for anchored in (True, False):
            tries = [list(want)]
            tries.append(relax(list(want), anchored, True))       # blanks give
            kept, floor[:] = list(floor), short
            tries.append(relax(list(tries[-1]), anchored, True))  # ...to 36pt
            floor[:] = kept
            tries.append(relax(list(tries[-1]), anchored, False))  # labels wrap
            for widths in tries:
                xs, end = run(widths, anchored)
                if end <= right_edge:
                    fit = (xs, widths)
                    break
            if fit:
                break
        if fit is None:
            widths = relax(relax(list(want), False, True), False, False)
            fit = (run(widths, False)[0], widths)
        xs, widths = fit
        # a blank set to end at a column's shared end point ends there, where
        # it starts is up to its label (and never short of a usable blank)
        widths = list(widths)
        for n, item in enumerate(spots):
            if item.get("end_x") and item["what"] != "text":
                nxt_ = xs[n + 1] - min(pad[n], 12.0) if n + 1 < len(spots) else right_edge
                widths[n] = max(36.0, min(item["end_x"], nxt_) - xs[n])
        out = []
        for n, item in enumerate(spots):
            if item["what"] == "text":
                lines = self.wrap(self.shown(item), widths[n],
                                  item["bold"], item["italic"], self.size)
            else:
                lines = [""]
            out.append((item, xs[n], widths[n], lines))
        return out

    def measure(self, row, right_edge, lead=1.25):
        """How tall a row will be, without placing it - so a heading can be
        held back to the next page when its section would not follow it."""
        laid = self.plan(row, right_edge)
        return max(len(lines) for *_, lines in laid) * self.size * lead

    def line_up(self, laid, right_edge, gap=4.0):
        """Keep an item in the column it shared with the row above.

        Each row is planned on its own, and the blanks before an item give
        back different amounts in different rows - so on the quota forms'
        "BETWEEN: ___ and ___" / "and ___ and ___" the second "and" landed
        17pt short of the first. Where an item started where an item in a
        recent row started, it goes where that one went, taking the
        difference out of the blanks either side of it - and only where
        nothing would collide or shrink past a usable blank."""
        cols = [c for c in self.__dict__.get("_cols", ())
                if c[2] == self.doc.page_count and 0 < self.y - c[3] <= self.size * 5]
        laid = [list(t) for t in laid]
        # A row that opens with a tick box under another one - Bill Payment's
        # second "☐ Member error" under "Please indicate ☐ Branch error" -
        # moves over whole, so the boxes stay one above the other: left where
        # the form put it, it sat left of the first box once "Please
        # indicate" had grown.
        if laid and laid[0][0].get("kind") in ("CheckBox", "RadioButton"):
            first = laid[0][0]
            match = [c for c in cols if abs(c[0] - first["rect"].x0) <= 2.0 and c[4] != "text"]
            if match:
                d = match[-1][1] - laid[0][1]
                if 0.5 <= abs(d) <= 90 and laid[-1][1] + laid[-1][2] + d <= right_edge + 1 \
                        and laid[0][1] + d >= 18:
                    for t in laid:
                        t[1] += d
        for n in range(1, len(laid)):
            item, x, room, lines = laid[n]
            # (a blank lines up with a blank started within 12pt of it: ABM's
            #  name and number lines began 9pt apart, and once "Member
            #  Number:" grew they were 14pt apart)
            match = [c for c in cols if abs(c[0] - item["rect"].x0) <= 2.0
                     or (item["what"] != "text" and c[4] != "text"
                         and item.get("kind") not in ("CheckBox", "RadioButton")
                         and abs(c[0] - item["rect"].x0) <= 12.0)]
            if not match:
                continue
            d = match[-1][1] - x
            if abs(d) < 0.5 or abs(d) > 90:
                continue
            prev, nxt = laid[n - 1], laid[n + 1] if n + 1 < len(laid) else None
            blank = lambda t: t[0]["what"] != "text" and t[0].get("kind") not in (
                "CheckBox", "RadioButton")
            # the item before: a blank stretches or gives; anything else must
            # still clear it
            if blank(prev):
                if prev[2] + d < 36.0:
                    continue
            elif x + d < prev[1] + prev[2] + gap:
                continue
            # the item after: a blank starts later or earlier by as much and
            # still ends where it did; anything else must still be clear
            if nxt is not None:
                if blank(nxt):
                    if nxt[2] - d < 36.0:
                        continue
                elif x + d + room + gap > nxt[1]:
                    continue
            elif x + d + room > right_edge + 1:
                continue
            if blank(prev):
                prev[2] += d
            if nxt is not None and blank(nxt):
                nxt[1] += d
                nxt[2] -= d
            laid[n][1] = x + d
        return [tuple(t) for t in laid]

    def remember_columns(self, laid):
        cols = self.__dict__.setdefault("_cols", [])
        for item, x, _room, _lines in laid:
            cols.append((item["rect"].x0, x, self.doc.page_count, self.y, item["what"]))
        del cols[:-40]

    def place(self, row, right_edge, lead=1.25, last=False):
        laid = self.line_up(self.plan(row, right_edge), right_edge)
        tall = max(len(lines) for _, _, _, lines in laid)
        height = tall * self.size * lead
        span = (min(x for _, x, _, _ in laid),
                max(x + max(room, 1.0) for _, x, room, _ in laid))
        # The very last row may lean into the bottom margin rather than open
        # a page of its own: a form whose footer misses the foot by five
        # points otherwise ends with a sheet holding one line.
        self.room(height, floor=self.bottom / 2 if last else None, span=span)
        base = self.y + self.size
        # where this row came from and where it went, top and bottom, so the
        # form's own rules and boxes can follow it
        self.track(row, height,
                   [(i["rect"].x0, i["rect"].x1, x) for i, x, _, _ in laid])
        self.remember_columns(laid)
        def joined(n):
            """Does this blank continue the one before it, with no gap the
            form left between them?"""
            if n == 0:
                return False
            a, b = laid[n - 1][0], laid[n][0]
            return (a["what"] != "text" and b["what"] != "text"
                    and a.get("kind") not in ("CheckBox", "RadioButton")
                    and b.get("kind") not in ("CheckBox", "RadioButton")
                    and (b["rect"].x0 - a["rect"].x1 <= 4.0
                         or (a.get("on_rule") is not None
                             and a.get("on_rule") == b.get("on_rule"))))

        for n, (item, x, room, lines) in enumerate(laid):
            if item["what"] == "text":
                alias, path, font = self.face(item["bold"], item["italic"])
                for k, piece in enumerate(lines):
                    at = base + k * self.size * lead
                    self.put(item, x, at, piece, (alias, path, font))
                    self.said.update(re.findall(r"[^\s_]+", piece))
                    self.note_ink(x, at, piece, font)
                    self.note_swap(item, x, at, piece, font)
                    if item.get("underline") and not item.get("underline_group") and not any(
                            r[3] for r in (item.get("runs") or ())):
                        # the form ruled a line under these words; it belongs
                        # under them, not in a column of its own
                        drop = float(item["underline"])
                        ext_l, ext_r = item.get("underline_ext", (0.0, 0.0))
                        end = x + max(room, font.text_length(piece, self.size))
                        # never under the next thing on the row, which the
                        # bigger type may have brought closer
                        stop = laid[n + 1][1] - 3 if n + 1 < len(laid) else right_edge
                        start = laid[n - 1][1] + laid[n - 1][2] + 3 if n else 18.0
                        self.page.draw_line(fitz.Point(max(start, x - ext_l), at + drop),
                                            fitz.Point(max(end, min(stop, end + ext_r)), at + drop),
                                            color=(0, 0, 0), width=0.6)
            elif item.get("kind") in ("CheckBox", "RadioButton"):
                side = self.size * 0.9
                self.field(fitz.Rect(x, base - side, x + side, base), item)
            elif item.get("boxed"):
                # the form shows this one as a box to write in, not a rule
                deep = max(item["rect"].height, self.size * 1.4)
                cell = fitz.Rect(x, base - self.size, x + room, base - self.size + deep)
                self.page.draw_rect(cell, color=(0, 0, 0), width=0.6)
                self.drew[item["what"]] += 1
                self.field(cell + (1, 1, -1, -1), item)
                self.y = max(self.y, cell.y1 - height + 2)
            else:
                bottom = base + (len(lines) - 1) * self.size * lead + 2
                if not joined(n):
                    # Several fields can sit along one rule the form drew -
                    # "BETWEEN: ___ ___ ___" is one line to write on, not
                    # three. Drawn a blank each, they come out as a line
                    # broken into pieces with gaps in it.
                    run_to = x + room
                    last_ = item
                    for k in range(n + 1, len(laid)):
                        if not joined(k):
                            break
                        run_to = laid[k][1] + laid[k][2]
                        last_ = laid[k][0]
                    past = (last_.get("rule_x1") or 0) - last_["rect"].x1
                    if past > 2 and not any(i["rect"].x0 >= last_["rect"].x1 - 1
                                            for i, *_ in laid if i is not last_
                                            and i["rect"].x0 < last_["rect"].x1 + past):
                        run_to = min(max(run_to, last_["rule_x1"]), right_edge)
                    lone = (item.get("unruled") and not any(
                        i["what"] == "text" for i, *_ in laid)) or item.get(
                        "type") == fitz.PDF_WIDGET_TYPE_BUTTON
                    # (a button never has a line under it)
                    if not item.get("in_cell") and not lone:
                        self.page.draw_line(fitz.Point(x, bottom),
                                            fitz.Point(run_to, bottom),
                                            color=(0, 0, 0), width=0.6)
                self.drew[item["what"]] += 1
                if item["what"] == "field":
                    self.field(fitz.Rect(x, bottom - self.size - 2,
                                         x + room, bottom), item)
        # a rule the form ran under several things on this row: one line from
        # the first of them to past the last, as far past as the form had it
        groups = {}
        for item, x, room, lines in laid:
            g = item.get("underline_group")
            if g and item["what"] == "text":
                font = self.face(item["bold"], item["italic"])[2]
                ink = max((font.text_length(p_, self.size) for p_ in lines), default=0.0)
                groups.setdefault(g, []).append((item, x, x + max(room, ink)))
        for (gx0, gx1), members in groups.items():
            first = min(members, key=lambda m: m[1])
            last = max(members, key=lambda m: m[2])
            drop = float(first[0]["underline"])
            x0 = first[1] - max(0.0, first[0]["rect"].x0 - gx0)
            x1 = min(right_edge, last[2] + max(0.0, gx1 - last[0]["rect"].x1))
            y = base + drop
            self.page.draw_line(fitz.Point(max(18.0, x0), y), fitz.Point(x1, y),
                                color=(0, 0, 0), width=0.6)
        self.y += height
        return height

    def place_rules(self, rules, under, right_edge, lead=1.25):
        """Draw a row of blanks where the captions beneath them will be.

        A line and the caption under it were set flush in the form. Laid out
        one row at a time at a bigger size they drift apart, because whatever
        runs along the row above pushes one of them and not the other - and a
        signature line starting forty points to the left of its own caption
        looks like a mistake, because it is one."""
        # where the captions will really go: place() lines them up with the
        # row above as well, and planned without that TFSA's "Date of Birth"
        # line started 16pt right of its caption, which had gone back under
        # "TFSA Contract Number"
        plan = self.line_up(self.plan(under, right_edge), right_edge)
        spots = sorted(rules, key=lambda i: i["rect"].x0)
        height = self.size * lead
        # A line and the caption that says what it is are one thing. Room has
        # to be found for both, or the ruled line ends a page and the words
        # under it start the next one.
        self.room(height + self.measure(under, right_edge) + 4)
        base = self.y + self.size
        # Match each blank to the caption that starts nearest it, rather than
        # by position in the row. A signing block often has more blanks than
        # captions - two signature boxes sharing one ruled line, a date blank
        # with nothing written under it - and pairing them off in order puts
        # every line above the wrong words from the first odd one onwards.
        where = []
        for n, item in enumerate(spots):
            near = min(plan, key=lambda p: abs(p[0]["rect"].x0 - item["rect"].x0),
                       default=None)
            if near is not None and abs(near[0]["rect"].x0 - item["rect"].x0) <= 40:
                x = near[1]
                after = min((p[1] for p in plan if p[1] > x + 4), default=right_edge)
                after = max(after, x + 40)
                # clear of the next line by as much as the form left, up to
                # 8pt: 4pt read as one line running on
                nxt = spots[n + 1]["rect"].x0 - item["rect"].x1 if n + 1 < len(spots) else 4.0
                clear = min(8.0, max(4.0, nxt))
                wide = max(min(max(item["rect"].width, near[2]), after - x - clear), 36.0)
            else:
                x, wide = item["rect"].x0, item["rect"].width
            where.append((x, wide))
        self.track(rules, height,
                   [(i["rect"].x0, i["rect"].x1, x)
                    for i, (x, _) in zip(spots, where)])
        for n, item in enumerate(spots):
            x, wide = where[n]
            # blanks that touched on the form are one line to write on: the
            # first name, middle initial and surname along Turkey's signature
            # line came out as three dashes
            if n and spots[n]["rect"].x0 - spots[n - 1]["rect"].x1 <= 4.0:
                x0p, wp = where[n - 1]
                if x > x0p + wp:
                    wide += x - (x0p + wp)
                    x = x0p + wp
            if not item.get("in_cell") and not item.get("unruled"):
                self.page.draw_line(fitz.Point(x, base + 2),
                                    fitz.Point(x + wide, base + 2),
                                    color=(0, 0, 0), width=0.6)
            self.drew[item["what"]] += 1
            if item["what"] == "field":
                self.field(fitz.Rect(x, base - self.size, x + wide, base + 2), item)
        self.y += height

    def field(self, rect, item):
        w = fitz.Widget()
        w.rect = rect
        w.field_name = item["name"] or f"field_{len(self.fields)}"
        w.field_type = item["type"]
        if item["kind"] == "RadioButton":
            # PyMuPDF can only make a radio button that already has a group to
            # join, and a rebuilt page has none. Made as a tick box it looks
            # and fills in the same way, but the group no longer picks one
            # answer only, so say so rather than ship it quietly.
            w.field_type = fitz.PDF_WIDGET_TYPE_CHECKBOX
            self.swapped.append(w.field_name)
        if w.field_type == fitz.PDF_WIDGET_TYPE_CHECKBOX:
            w.border_width = 0.8
            w.border_color = (0, 0, 0)
        w.field_flags = item.get("flags") or 0
        # What the field does, not just what it is called: its scripts - the
        # T4 worksheets add their own boxes up, dates and dollar amounts
        # format themselves - a dropdown's list, its tooltip, a length limit.
        # Rebuilt with a name and a type only, every one of those was lost,
        # and the field check, which counts names and types, passed it.
        props = item.get("props") or {}
        for what in R.FIELD_SCRIPTS + ("field_label", "text_maxlen", "text_format",
                                       "button_caption", "fill_color", "border_color",
                                       "text_color", "text_fontsize"):
            if props.get(what):
                setattr(w, what, props[what])
        if props.get("choice_values") and w.field_type in (
                fitz.PDF_WIDGET_TYPE_COMBOBOX, fitz.PDF_WIDGET_TYPE_LISTBOX):
            w.choice_values = props["choice_values"]
        if item.get("value") and w.field_type in (
                fitz.PDF_WIDGET_TYPE_TEXT, fitz.PDF_WIDGET_TYPE_COMBOBOX):
            # a form that comes with Libro's own address already typed into it
            # has to come back with it still there
            w.field_value = item["value"]
            w.text_fontsize = self.size
        made = self.page.add_widget(w)
        if w.field_type == fitz.PDF_WIDGET_TYPE_CHECKBOX and (props.get("on_state")
                                                             or props.get("checked")):
            self.tick_state(made, props.get("on_state") or "Yes", props.get("checked"))
        self.fields.append(w.field_name)
        # the tick boxes the form stacked on this one go back on top of it
        for twin in item.get("twins", ()):
            self.field(rect, twin)

    def tick_state(self, w, on, checked):
        """Give a rebuilt tick box the export value the form's had - what it
        sends when ticked, "1" or "Male" rather than PyMuPDF's "Yes" - and
        tick it if the form shipped it ticked ("Funds Transfer PAD")."""
        doc = self.page.parent
        # read back as the PDF writes it, "did#20not#20differentiated": the
        # #20s are spaces, and escaped again they became "#2320" - LIF
        # Locking-in's tick box sent a value nobody's system knows
        on = re.sub(r"#([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), on)
        name = "/" + "".join(c if c.isalnum() or c in "-_." else "#%02X" % ord(c)
                             for c in on)
        for key in ("AP/N", "AP/D"):
            kind, val = doc.xref_get_key(w.xref, key)
            if kind == "dict" and "/Yes" in val and on != "Yes":
                doc.xref_set_key(w.xref, key, val.replace("/Yes", name, 1))
        state = name if checked else "/Off"
        doc.xref_set_key(w.xref, "AS", state)
        doc.xref_set_key(w.xref, "V", state)

    def shade(self, row, right_edge, lead=1.25, was=None):
        """A section bar, drawn around the words it now holds rather than at
        the width it had: at a bigger size the heading is wider than its old
        bar, and a bar that stops short of its own heading looks like a
        mistake."""
        laid = self.plan(row, right_edge)
        x0 = min(x for _, x, _, _ in laid)
        x1 = max(x + w for _, x, w, _ in laid)
        if was is not None:
            # a section bar runs the width of the column, not the width of
            # its heading: fitted to the words it turns into a highlighter
            # stripe over them
            x0 = min(x0, was.x0)
            x1 = max(x1 + 4, min(was.x1, right_edge))
        tall = max(len(lines) for *_, lines in laid)
        self.page.draw_rect(fitz.Rect(x0 - 2, self.y - 1, x1 + 4,
                                      self.y + tall * self.size * lead + 1),
                            color=None, fill=BAR_FILL.get(id(was), BAR_GREY))

    def track(self, row, height, moves=()):
        """Note where a row's band started and ended, before and after, so the
        rules and boxes the form drew round it can be put back in the same
        relation to it. `moves` says where each thing in the row went across
        as well as down, so a mark the form drew on one of them - the orange
        "SIGN" flag on a signature line - travels with it instead of staying
        behind pointing at empty paper."""
        page_no = self.doc.page_count - 1
        self.origin.setdefault(page_no, self.source)
        if self.quiet:
            return            # a running block: the form's art stays with its own page
        top = min(i["rect"].y0 for i in row)
        foot = max(i["rect"].y1 for i in row)
        self.moved.append((top, self.y, page_no, self.source))
        self.moved.append((foot, self.y + height, page_no, self.source))
        if moves:
            self.slid.append((top, foot, list(moves), self.source))

    def note_ink(self, x, base, piece, font):
        """Remember where words were set, so nothing is drawn over them."""
        if not piece.strip():
            return
        self.ink.setdefault(self.doc.page_count - 1, []).append(
            fitz.Rect(x, base - self.size * 0.85,
                      x + font.text_length(piece, self.size),
                      base + self.size * 0.22))

    def note_swap(self, item, x, base, piece, font):
        """Remember where replaced wording was set, so the proof can point at
        it. The new words are a different length from the old, so the box has
        to come from what was actually drawn, not from where the label was."""
        if not item.get("was") or not piece:
            return
        self.swaps.append((self.doc.page_count - 1, item.get("swap"),
                           fitz.Rect(x, base - self.size * 0.85,
                                     x + font.text_length(piece, self.size),
                                     base + self.size * 0.2)))

    def room(self, height, floor=None, span=None, clear=True):
        """Start a new page unless this much will fit on the one in hand, and
        keep the row clear of any image already set on the sheet.

        `clear=False` asks only the first question. The main loop uses it to
        ask whether a whole section will fit before it sets the section's
        first row; that row is placed by something that keeps clear of the
        images itself, and knows how wide it is. Asked here at full width, a
        title set beside a logo was pushed down under it."""
        if not clear:
            return self.fits(height, floor)
        broke = self.fits(height, floor)
        if self.clear_of(height, span):
            # pushed below an image, which can run it off the sheet
            broke = self.fits(height, floor) or broke
            self.clear_of(height, span)
        return broke

    def fits(self, height, floor=None):
        """Start a new page unless this much will fit on the one in hand.

        The space left between rows is the first thing to go. A form that
        ends a page four points short of its last block would rather close up
        than send two lines to a sheet of their own, so the gap opened above
        this row is given back before a page is opened."""
        limit = self.rect.height - (self.bottom if floor is None else floor)
        if self.y + height <= limit:
            self.spare = 0.0
            return False
        if self.spare > 0 and self.y - self.spare + height <= limit:
            self.y -= self.spare
            self.spare = 0.0
            return False
        self.spare = 0.0
        self.new_page()
        return True

    def table(self, rows, right_edge, lead=1.3, pad=3.0):
        """A grid of cells, drawn again rather than carried over.

        Each column is as wide as its label now needs and the blanks share
        out what is left, so "Date of Birth" stays on one line. The rules are
        drawn around the rows at the height the rows have now: carrying the
        old grid across instead leaves the lines where the old rows were,
        with the new text straddling them."""
        spread = [sorted(r, key=lambda i: i["rect"].x0) for r in rows]
        cols = max(len(r) for r in spread)
        need, blank = [0.0] * cols, [False] * cols
        for r in spread:
            for n, item in enumerate(r):
                if item["what"] == "text":
                    font = self.face(item["bold"], item["italic"])[2]
                    need[n] = max(need[n], font.text_length(self.shown(item),
                                                            self.size) + 2 * pad)
                else:
                    blank[n] = True
                    need[n] = max(need[n], 36.0)
        left = min(r[0]["rect"].x0 for r in spread)
        total = right_edge - left
        spare = total - sum(need)
        blanks = sum(1 for b in blank if b)
        if spare > 0 and blanks:
            for n in range(cols):
                if blank[n]:
                    need[n] += spare / blanks
        elif spare < 0:
            squeeze = total / sum(need)
            need = [max(24.0, w * squeeze) for w in need]
        edges = [left]
        for w in need:
            edges.append(edges[-1] + w)

        for r in spread:
            laid, tall = [], 1
            for n, item in enumerate(r):
                width = edges[n + 1] - edges[n] - 2 * pad
                if item["what"] == "text":
                    lines = self.wrap(self.shown(item), width,
                                      item["bold"], item["italic"], self.size)
                else:
                    lines = [""]
                tall = max(tall, len(lines))
                laid.append((item, n, lines))
            height = tall * self.size * lead + 2
            self.room(height)
            top, base = self.y, self.y + self.size + 1
            self.track(r, height,
                       [(i["rect"].x0, i["rect"].x1, edges[n]) for i, n, _ in laid])
            for item, n, lines in laid:
                cell = fitz.Rect(edges[n], top, edges[n + 1], top + height)
                self.page.draw_rect(cell, color=(0, 0, 0), width=0.6)
                if item["what"] == "text":
                    alias, path, _ = self.face(item["bold"], item["italic"])
                    for k, piece in enumerate(lines):
                        self.put(item, cell.x0 + pad, base + k * self.size * lead, piece,
                                 self.face(item["bold"], item["italic"]))
                        self.said.update(re.findall(r"[^\s_]+", piece))
                        face = self.face(item["bold"], item["italic"])[2]
                        self.note_ink(cell.x0 + pad, base + k * self.size * lead,
                                      piece, face)
                        self.note_swap(item, cell.x0 + pad,
                                       base + k * self.size * lead, piece, face)
                elif item["what"] == "field":
                    self.field(fitz.Rect(cell.x0 + 2, cell.y0 + 2,
                                         cell.x1 - 2, cell.y1 - 2), item)
                else:
                    self.page.draw_line(fitz.Point(cell.x0 + pad, cell.y1 - 3),
                                        fitz.Point(cell.x1 - pad, cell.y1 - 3),
                                        color=(0, 0, 0), width=0.6)
                    self.drew[item["what"]] += 1
            self.y += height

    def widen(self, xs, items, at, pad):
        """Give every column room for its longest word.

        Scaling the form's columns by the same factor is right for shape but
        wrong for words: a narrow column set for 8pt type has no room for the
        same label at 10, and "Acct #" comes back broken over two lines. Each
        column takes what its words need, out of whatever the columns with
        room to spare can give."""
        need = [0.0] * (len(xs) - 1)
        for i in items:
            if i["what"] != "text":
                continue
            mid = at((i["rect"].x0 + i["rect"].x1) / 2)
            n = max(0, min(len(need) - 1,
                           sum(1 for x in xs[1:-1] if x <= mid)))
            font = self.face(i["bold"], i["italic"])[2]
            words = i["text"].split()
            if len(words) <= 3:
                # a short label is a label: "Acct #" and "Date of Birth" are
                # meant to sit on one line, and a column that only fits the
                # longest word in them breaks them in half
                want = font.text_length(i["text"], self.size)
            else:
                want = max((font.text_length(w, self.size) for w in words), default=0.0)
            need[n] = max(need[n], want + 2 * pad)
        have = [xs[n + 1] - xs[n] for n in range(len(need))]
        short = sum(max(0.0, need[n] - have[n]) for n in range(len(need)))
        if short <= 0:
            return xs
        spare = sum(max(0.0, have[n] - need[n]) for n in range(len(need)))
        if spare <= 0:
            return xs
        take = min(1.0, short / spare)
        out, x = [xs[0]], xs[0]
        for n in range(len(need)):
            if have[n] < need[n]:
                width = have[n] + (need[n] - have[n]) * min(1.0, spare / short)
            else:
                width = have[n] - (have[n] - need[n]) * take
            x += width
            out.append(x)
        out[-1] = xs[-1]
        return out

    def flow_strip(self, order, x0, x1, gap=4.0):
        """Labels and tick boxes set in the form's order, wrapping onto as
        many lines as the cell needs: (item, x, width, pieces), each piece
        a line - the shape `plan` returns - with every box on the line its
        label is on. A box stays with the label that follows it."""
        side = self.size * 0.9
        units, k = [], 0
        while k < len(order):
            i = order[k]
            if (i["what"] != "text" and k + 1 < len(order)
                    and order[k + 1]["what"] == "text"
                    and order[k + 1]["rect"].x0 - i["rect"].x1 < 20):
                units.append([i, order[k + 1]])
                k += 2
            else:
                units.append([i])
                k += 1

        def wide(i):
            return (side if i["what"] != "text" else
                    self.face(i["bold"], i["italic"])[2].text_length(i["text"], self.size))

        out, line, x = [], 0, x0
        for u in units:
            w = sum(wide(i) for i in u) + gap * (len(u) - 1)
            if x > x0 and x + w > x1:
                line, x = line + 1, x0
            for i in u:
                if i["what"] != "text":
                    out.append((dict(i, line=line, src=i), x, side, [""] * (line + 1)))
                    x += side + gap
                    continue
                ww = wide(i)
                if x + ww > x1:
                    # a label longer than the room left wraps on its own lines
                    lines = self.wrap(i["text"], x1 - x, i["bold"], i["italic"], self.size)
                    out.append((dict(i, line=line, src=i), x, x1 - x, [""] * line + lines))
                    line += len(lines) - 1
                    x = x + self.face(i["bold"], i["italic"])[2].text_length(
                        lines[-1], self.size) + gap
                else:
                    out.append((dict(i, line=line, src=i), x, ww, [""] * line + [i["text"]]))
                    x += ww + gap
        return out

    def grid(self, spec, bands, right_edge, bars=(), lead=1.3, pad=3.0, fills=()):
        # A shaded band can be drawn as one fill or as one per cell - the
        # AFT form greys its "Name | Phone | Email" header three times over,
        # a cell at a time. Fills that meet end to end on the same band are
        # one band, or no single cell is wide enough to count as shading.
        merged = []
        for b in sorted((fitz.Rect(b) for b in bars), key=lambda b: (round(b.y0), b.x0)):
            last = merged[-1] if merged else None
            if (last is not None and abs(last.y0 - b.y0) <= 2 and abs(last.y1 - b.y1) <= 2
                    and b.x0 - last.x1 <= 3):
                last |= b
            else:
                merged.append(fitz.Rect(b))
        bars = merged
        """Draw a table the form ruled, around the content it now holds.

        The lattice is the form's own - which cuts reach across which row -
        so a merged cell stays merged. Only the widths are re-scaled, into
        the room the rebuild has; a cell whose words no longer fit wraps and
        its row grows, which is the whole point of drawing the table again
        rather than carrying it."""
        box = spec["box"]
        left = max(box.x0, 18.0)
        width = max(60.0, min(right_edge, box.x1) - left)
        scale = width / max(box.width, 1.0)

        def at(x):
            return left + (x - box.x0) * scale

        for top, foot, cuts, items in bands:
            xs = self.widen([at(c) for c in cuts], items, at, pad)
            plan, tall = [], 0
            for n in range(len(xs) - 1):
                room = xs[n + 1] - xs[n] - 2 * pad
                # exclusive on the left, and the last cell takes the rest,
                # so nothing lands in two cells at once
                last = n == len(xs) - 2
                mine = [i for i in items
                        if xs[n] <= at((i["rect"].x0 + i["rect"].x1) / 2)
                        < (xs[n + 1] if not last else 10 ** 6)
                        or (n == 0 and at((i["rect"].x0 + i["rect"].x1) / 2) < xs[0])]
                # A cell holds rows of its own - a heading, a tick box and its
                # wording, a label and its blank. Run together into one string
                # they come out jumbled and in the wrong order, so the cell is
                # laid out line by line the way the form set it.
                cell = []
                for item in sorted(mine, key=lambda i: (round(i["base"], 1), i["rect"].x0)):
                    if cell and abs(item["base"] - cell[-1]["base"]) <= 4:
                        cell[-1]["items"].append(item)
                    else:
                        cell.append({"base": item["base"], "items": [item]})
                # A bullet on its own goes with the words it stands in front
                # of. Those words are often a paragraph merged from several
                # lines, which carries the baseline of its LAST line, so the
                # bullet came out as a strip of its own above the paragraph.
                k_ = 0
                while k_ < len(cell) - 1:
                    only = cell[k_]["items"]
                    if (len(only) == 1 and only[0]["what"] == "text"
                            and only[0]["text"].strip() in ("•", "●", "▪", "-", "–")):
                        nxt = cell[k_ + 1]["items"]
                        if any(i["what"] == "text" and abs(i["rect"].y0 - only[0]["rect"].y0) <= 3
                               and i["rect"].x0 > only[0]["rect"].x0 for i in nxt):
                            cell[k_ + 1]["items"] = only + nxt
                            del cell[k_]
                            continue
                    k_ += 1
                drawn = []
                after_full = False
                # each strip's ink, side to side, for telling a centred block
                # from a paragraph: centred lines share a middle and not a
                # left edge, a paragraph's lines the other way round
                extents = []
                for strip in cell:
                    ts = [i for i in strip["items"] if i["what"] == "text"]
                    extents.append((min(i["rect"].x0 for i in ts), max(i["rect"].x1 for i in ts))
                                   if ts else None)

                def stacked_centre(k):
                    me = extents[k]
                    if me is None:
                        return False
                    for j in (k - 1, k + 1):
                        if 0 <= j < len(extents) and extents[j] is not None:
                            o = extents[j]
                            if (abs((o[0] + o[1]) - (me[0] + me[1])) / 2 <= 2.0
                                    and abs(o[0] - me[0]) > 3.0):
                                return True
                    return False
                for k_strip, strip in enumerate(cell):
                    texts = [i for i in strip["items"] if i["what"] == "text"]
                    blanks = [i for i in strip["items"] if i["what"] != "text"]
                    words = " ".join(i["text"] for i in texts).strip()
                    bold = any(i["bold"] for i in texts)
                    italic = bool(texts) and all(i["italic"] for i in texts)
                    # Several labels set well apart inside one wide cell are
                    # columns, not a sentence. Joined up they read as one -
                    # the Milk form's "WITNESS' NAME (non-family member)
                    # WITNESS' SIGNATURE DIRECTOR'S (Producer) NAME ..." -
                    # so they are laid out across the cell the way a row is.
                    spread = None
                    order_ = sorted(texts, key=lambda i: i["rect"].x0)
                    hang = None
                    if (len(order_) > 1 and not blanks
                            and (order_[0]["text"].strip() in ("•", "●", "▪", "-", "–")
                                 or re.fullmatch(r"\(?(?:\d{1,3}|[a-zA-Z]|[ivx]{1,4})[.)]",
                                                 order_[0]["text"].strip()))):
                        # Est120's "•  If funds are transferred via AFT ..."
                        # was laid out as two columns of a row, the sentence
                        # too wide for the second, and came back as a bullet
                        # on a line of its own over the paragraph
                        gap = at(order_[1]["rect"].x0) - at(order_[0]["rect"].x0)
                        hang = (order_[0], min(30.0, max(10.0, gap)))
                    if hang is not None:
                        pass
                    elif (len(texts) > 1
                            and max(i["rect"].x1 for i in texts)
                            - min(i["rect"].x0 for i in texts) > room * 0.5):
                        # a tick box between the labels is planned with them,
                        # so it keeps its place in the line rather than being
                        # set after the last of them
                        between = [i for i in blanks
                                   if i.get("kind") in ("CheckBox", "RadioButton")
                                   and min(t["rect"].x0 for t in texts) < i["rect"].x0
                                   < max(t["rect"].x0 for t in texts)]
                        spread = self.plan(sorted(texts + between,
                                                  key=lambda i: i["rect"].x0),
                                           xs[n + 1] - pad)
                        # only where it fits: nine options with their boxes
                        # in line ran a word past the margin on the Transfer
                        # Authorization, so a row that cannot hold its boxes
                        # in place keeps them after its words, as before
                        ends = [x + self.face(i["bold"], i["italic"])[2].text_length(
                                    p, self.size)
                                for i, x, _w, ps in spread if i["what"] == "text"
                                for p in ps if p]
                        if between and ends and max(ends) > xs[n + 1] - pad + 1:
                            spread = None
                    # Tick boxes set among the labels that will not fit on
                    # one line keep their places all the same, wrapped in the
                    # form's order. Set after the last label instead, the
                    # transfer authorization's nine plan types came out as
                    # "RRSP LIRA LRSP ... FHSA" and then a row of nine boxes
                    # past the edge of the table.
                    ticks_ = [i for i in blanks if i.get("kind") in ("CheckBox", "RadioButton")]
                    if (spread is None and texts and ticks_
                            and any(b["rect"].x0 < max(t["rect"].x0 for t in texts)
                                    for b in ticks_)):
                        order = sorted(texts + ticks_, key=lambda i: i["rect"].x0)
                        side = self.size * 0.9
                        width = sum(self.face(i["bold"], i["italic"])[2].text_length(
                                        i["text"], self.size) + 4
                                    if i["what"] == "text" else side + 4 for i in order)
                        if xs[n] + pad + width > xs[n + 1] - pad + 2:
                            spread = self.flow_strip(order, xs[n] + pad, xs[n + 1] - pad)
                    elif (spread is not None and len(texts) > 1 and ticks_
                          and any(x + w > xs[n + 1] - pad + 1 for _i, x, w, _p in spread)):
                        spread = self.flow_strip(sorted(texts + ticks_, key=lambda i: i["rect"].x0),
                                                 xs[n] + pad, xs[n + 1] - pad)
                    # A heading the form centred in its cell ("Owner
                    # Identification", "Name | Phone | Email") is centred in
                    # the new one; set from the left edge it reads as a label.
                    # Centred means the gaps either side agree and are well
                    # more than the cell's padding - a label set flush left
                    # has a gap of a few points on that side.
                    # A line of a paragraph is not a heading even where its
                    # margins happen to agree: it fills the cell, or it is the
                    # short last line under one that does (Est120's "funds
                    # and listed in the area below. No exceptions.").
                    centred = False
                    span_w = (max(i["rect"].x1 for i in texts)
                              - min(i["rect"].x0 for i in texts)) if texts else 0.0
                    cell_w = cuts[n + 1] - cuts[n] if n + 1 < len(cuts) else 0.0
                    if (texts and not blanks and spread is None and cell_w > 0
                            and span_w <= 0.75 * cell_w and not after_full):
                        gl = min(i["rect"].x0 for i in texts) - cuts[n]
                        gr = cuts[n + 1] - max(i["rect"].x1 for i in texts)
                        centred = (min(gl, gr) > 3 * pad
                                   and abs(gl - gr) <= max(4.0, 0.08 * cell_w))
                    # a line of a centred block: "Primary (P) or / Alternate
                    # (A) and/or Successor / Holder*" is wide in the middle,
                    # and only its first line came back centred
                    if (not centred and texts and not blanks and spread is None
                            and stacked_centre(k_strip)):
                        centred = True
                    after_full = (bool(texts) and cell_w > 0 and span_w > 0.75 * cell_w
                                  and not centred)
                    if hang is not None:
                        rest = [t for t in order_ if t is not hang[0]]
                        words = " ".join(t["text"] for t in rest).strip()
                        drawn.append((self.wrap(words, room - hang[1], bold, italic, self.size)
                                      or [""], bold, italic, blanks, rest, None, hang))
                        continue
                    drawn.append((self.wrap(words, room, bold, italic, self.size)
                                  if words else [""], bold, italic, blanks, texts, spread,
                                  centred))
                tall += sum(max((len(p) for _i, _x, _w, p in d[5]), default=len(d[0]))
                            if d[5] else len(d[0]) for d in drawn) or 1
                plan.append((n, drawn))
            def strip_tall(d):
                return (max((len(p) for _i, _x, _w, p in d[5]), default=1)
                        if d[5] else len(d[0]))

            tall = max(tall // max(len(xs) - 1, 1), max(
                (sum(strip_tall(d) for d in cells) for _, cells in plan), default=1), 1)
            height = tall * self.size * lead + 2 * pad
            self.room(height)
            y0 = self.y
            if items:
                self.track(items, height,
                           [(i["rect"].x0, i["rect"].x1,
                             at((i["rect"].x0 + i["rect"].x1) / 2)) for i in items])
            # a band the form shaded is a section heading inside the table,
            # and it keeps its grey
            shaded = any(b.y0 - 3 <= top and foot <= b.y1 + 3
                         and b.width > 0.6 * box.width for b in bars)
            for n, drawn in plan:
                box_cell = fitz.Rect(xs[n], y0, xs[n + 1], y0 + height)
                # A cell takes the fill that covered most of it on the form.
                # Word shades a table's header row a cell at a time, inset
                # from the walls and often taller than a bar is allowed to be
                # (FHSA's 55pt header), so a band-wide test lost those rows.
                was_cell = fitz.Rect(cuts[n], top, cuts[n + 1], foot) \
                    if n + 1 < len(cuts) else None
                tint = None
                if was_cell is not None and not was_cell.is_empty:
                    best = 0.0
                    for r, f in fills:
                        over = (r & was_cell).get_area() if r.intersects(was_cell) else 0.0
                        if over > best and over >= 0.5 * was_cell.get_area():
                            best, tint = over, f
                if tint is None and shaded:
                    tint = BAR_GREY
                if tint is not None:
                    self.page.draw_rect(box_cell, color=None, fill=tint)
                self.page.draw_rect(box_cell, color=(0, 0, 0), width=0.6)
                base = y0 + pad + self.size
                wordy = any(any(piece.strip() for piece in l) for l, *_ in drawn)
                for lines, bold, italic, blanks, texts, spread, centred in drawn:
                    alias, path, font = self.face(bold, italic)
                    strip_ = self.strip_item(texts, bold, italic)
                    # A heading shaded inside a bigger cell - TFSA's "PRIMARY
                    # BENEFICIARY DESIGNATION" bar in a one-cell box - keeps
                    # its shading behind its own line, though the cell as a
                    # whole was not shaded.
                    if texts and tint is None and was_cell is not None and spread is None:
                        tb = fitz.Rect(texts[0]["rect"])
                        for t in texts[1:]:
                            tb |= t["rect"]
                        for r, f in fills:
                            if (r.intersects(tb) and (r & tb).get_area() >= 0.6 * tb.get_area()
                                    and r.get_area() < 0.6 * was_cell.get_area()):
                                deep = max(len(lines), 1)
                                widest = max((font.text_length(p_, self.size) for p_ in lines),
                                             default=0.0)
                                bx0 = max(xs[n], at(r.x0))
                                bx1 = min(xs[n + 1], max(at(r.x1), xs[n] + pad + widest + pad))
                                self.page.draw_rect(
                                    fitz.Rect(bx0, base - self.size - 1.5, bx1,
                                              base + (deep - 1) * self.size * lead + 3.5),
                                    color=None, fill=f)
                                break
                    run = xs[n] + pad
                    deep = len(lines)
                    if spread is not None:
                        deep = 0
                        for item, at_x, wide, pieces in spread:
                            if item["what"] != "text":
                                side = self.size * 0.9
                                yb = base + item.get("line", 0) * self.size * lead
                                self.field(fitz.Rect(at_x, yb + 2 - side,
                                                     at_x + side, yb + 2), item)
                                blanks = [b for b in blanks if b is not item.get("src", item)]
                                deep = max(deep, item.get("line", 0) + 1)
                                continue
                            face = self.face(item["bold"], item["italic"])
                            for k, piece in enumerate(pieces):
                                if not piece:
                                    continue          # a line this label is not on
                                y = base + k * self.size * lead
                                self.put(item, at_x, y, piece, face)
                                self.said.update(re.findall(r"[^\s_]+", piece))
                                self.note_ink(at_x, y, piece, face[2])
                                self.note_swap(item, at_x, y, piece, face[2])
                            deep = max(deep, len(pieces))
                            run = max(run, at_x + wide + 4)
                        lines = [""] * max(deep, 1)
                    # A tick box the form set before or between the words -
                    # "Debit ☐ (Other FI's - Pre-authorized debits)", "☐ File
                    # Upload" - goes back where it was. The strip is set piece
                    # by piece in the form's order, each piece in its own
                    # weight, as long as it fits the line; left to the words
                    # and then the boxes, the box ended up after the sentence.
                    boxes_ = [i for i in blanks if i.get("kind") in ("CheckBox", "RadioButton")]
                    inline = (spread is None and len(lines) == 1 and boxes_ and texts
                              and any(b["rect"].x0 < max(t["rect"].x0 for t in texts)
                                      for b in boxes_))
                    if inline:
                        order = sorted(texts + boxes_, key=lambda i: i["rect"].x0)
                        side = self.size * 0.9
                        cursor = xs[n] + pad
                        width = sum(self.face(i["bold"], i["italic"])[2].text_length(i["text"], self.size) + 4
                                    if i["what"] == "text" else side + 4 for i in order)
                        inline = cursor + width <= xs[n + 1] - pad + 2
                    if inline:
                        y = base
                        for i in order:
                            if i["what"] == "text":
                                face = self.face(i["bold"], i["italic"])
                                self.put(i, cursor, y, i["text"], face)
                                self.said.update(re.findall(r"[^\s_]+", i["text"]))
                                self.note_ink(cursor, y, i["text"], face[2])
                                self.note_swap(i, cursor, y, i["text"], face[2])
                                cursor += face[2].text_length(i["text"], self.size) + 4
                            else:
                                self.field(fitz.Rect(cursor, y + 2 - side, cursor + side, y + 2), i)
                                cursor += side + 4
                        run = cursor
                        lines = [""]
                        blanks = [i for i in blanks if i not in boxes_]
                    indent = 0.0
                    if isinstance(centred, tuple):
                        mark, indent = centred
                        centred = False
                        mface = self.face(mark["bold"], mark["italic"])
                        self.put(mark, xs[n] + pad, base, mark["text"].strip(), mface)
                        self.said.update(re.findall(r"[^\s_]+", mark["text"]))
                        self.note_ink(xs[n] + pad, base, mark["text"].strip(), mface[2])
                    for k, piece in enumerate(lines):
                        x = xs[n] + pad + indent
                        if centred and piece:
                            x = max(x, (xs[n] + xs[n + 1]) / 2
                                    - font.text_length(piece, self.size) / 2)
                        if piece:
                            self.put(strip_, x, base + k * self.size * lead, piece,
                                     (alias, path, font))
                            self.said.update(re.findall(r"[^\s_]+", piece))
                            self.note_ink(x, base + k * self.size * lead,
                                          piece, font)
                            if texts:
                                self.note_swap(texts[0], x,
                                               base + k * self.size * lead, piece, font)
                        if k == len(lines) - 1 and piece:
                            run = x + font.text_length(piece, self.size) + 4
                    foot_y = base + (len(lines) - 1) * self.size * lead + 2
                    ticks = [i for i in blanks
                             if i.get("kind") in ("CheckBox", "RadioButton")]
                    for i in ticks:
                        side = self.size * 0.9
                        self.field(fitz.Rect(run, foot_y - side, run + side, foot_y), i)
                        run += side + 4
                    # What is left of the line after its label is the place to
                    # write. Where the form put several fields along it they
                    # share it - every one has to come back, under its own
                    # name, or the form stops working.
                    rest = [i for i in blanks if i not in ticks]
                    wide = max(24.0, xs[n + 1] - pad - run)
                    if rest:
                        # A cell with nothing but a blank in it IS the blank:
                        # its four sides are the box to write in, and a rule
                        # inside them is a second line nobody drew. The
                        # fields still go in either way.
                        # nor under fields the form wrote in the cell itself,
                        # with no line of their own
                        if wordy and not all(i.get("in_cell") for i in rest):
                            self.page.draw_line(fitz.Point(run, foot_y),
                                                fitz.Point(run + wide, foot_y),
                                                color=(0, 0, 0), width=0.6)
                        share = wide / len(rest)
                        for k, i in enumerate(rest):
                            self.drew[i["what"]] += 1
                            if i["what"] == "field":
                                self.field(fitz.Rect(run + k * share,
                                                     foot_y - self.size - 2,
                                                     run + (k + 1) * share, foot_y), i)
                    base += len(lines) * self.size * lead
            self.y += height

    def gap(self, points):
        self.y += points
        self.spare = points        # what could be given back to avoid a break

    def carry_art(self, page, skip=()):
        """Bring the form's own rules and boxes across, each following the row
        it belonged to. A box that spanned several rows is stretched to span
        them where they are now, so a table's grid still fits its cells."""
        # Only this page's own rows. Heights repeat from one page of a form
        # to the next, and read against every page's rows the rule under
        # Est107's "Late of:" on its third page followed a row at the same
        # height on its second, and came down through a witness's phone
        # number there.
        moved = sorted((m for m in self.moved if m[3] == page.number), key=lambda m: m[0])
        if not moved:
            for d in R.printed(page).get_drawings():
                r = fitz.Rect(d["rect"])
                if d["items"] and not any(touches(r, fitz.Rect(z)) for z in skip):
                    self.dropped.append(f"page {page.number + 1}: shape at "
                                        f"{r.x0:.0f},{r.y0:.0f} has no row on its page to follow")
            return 0
        # rows can overlap a point or two, so read the map in order of where
        # things were rather than in the order they were laid down
        olds = [old for old, _, _, _ in moved]

        def follow(y):
            before = [k for k, o in enumerate(olds) if o <= y]
            after = [k for k, o in enumerate(olds) if o >= y]
            if not before:
                k = after[0]
                return moved[k][1] + (y - olds[k]), moved[k][2]
            if not after:
                k = before[-1]
                return moved[k][1] + (y - olds[k]), moved[k][2]
            lo, hi = before[-1], after[0]
            if lo == hi:
                return moved[lo][1] + (y - olds[lo]), moved[lo][2]
            span = olds[hi] - olds[lo]
            part = min(1.0, max(0.0, (y - olds[lo]) / span)) if span else 0.0
            return (moved[lo][1] + part * (moved[hi][1] - moved[lo][1]),
                    moved[lo][2])

        def divider_at(y):
            """Where a rule running most of the page's width goes: it parts one
            section from the next, so it keeps its distance above the first row
            that came after it, and stays below every row that came before. Read
            between the rows either side, as other art is, it landed in the
            middle of a two-column block whose right-hand column had grown -
            through "Relinquishing Institution:" on the transfer authorization."""
            after = [k for k, o in enumerate(olds) if o >= y]
            if not after:
                return follow(y)
            hi = after[0]
            at, page_no = moved[hi][1] - (olds[hi] - y), moved[hi][2]
            above = [moved[k][1] for k, o in enumerate(olds) if o < y and moved[k][2] == page_no]
            if above:
                at = max(at, max(above) + 5)
            return at, page_no

        def sideways(r):
            """How far across the thing this shape was drawn on has moved.

            Only a shape that sat wholly within one item counts as drawn on
            it: a rule running the width of the page happens to cross every
            column and belongs to none of them."""
            for top, foot, moves, src in self.slid:
                if src != page.number or not top - 2 <= (r.y0 + r.y1) / 2 <= foot + 2:
                    continue
                for was_x0, was_x1, now_x0 in moves:
                    if was_x0 - 2 <= r.x0 and r.x1 <= was_x1 + 2:
                        return now_x0 - was_x0
            return 0.0

        drawn = 0
        for d in R.printed(page).get_drawings():
            r = fitz.Rect(d["rect"])
            if not d["items"] or any(touches(r, fitz.Rect(z)) for z in skip):
                continue
            if tuple(round(v, 1) for v in r) in SEPARATORS.get(page.number, ()):
                before = [k for k, o in enumerate(olds) if o <= r.y0]
                if before:
                    k = before[-1]
                    top, page_no = moved[k][1] + (r.y0 - olds[k]), moved[k][2]
                else:
                    top, page_no = follow(r.y0)
                bottom, foot_page = top + r.height, page_no
            elif r.height <= 3 and r.width >= 0.6 * page.rect.width:
                top, page_no = divider_at(r.y0)
                bottom, foot_page = top + r.height, page_no
            else:
                top, page_no = follow(r.y0)
                bottom, foot_page = follow(r.y1)
            if page_no >= self.doc.page_count:
                continue
            if foot_page != page_no:
                if r.height <= 40:
                    # a cell border or a short rule whose row happens to sit
                    # at the new page break: keep it whole, on the page its
                    # row went to, at the height it always had
                    bottom = min(top + r.height,
                                 self.rect.height - self.bottom / 2)
                else:
                    # A grid whose rows now run over more than one sheet. It
                    # cannot be carried: the rows it enclosed have moved and
                    # spread, so wherever the lines are put they enclose the
                    # wrong things - drawn in pieces they run down the page
                    # through the text. A table has to be drawn again from
                    # where its rows now are, which is what `table` does for
                    # the grids the row model recognises. This one it did not.
                    self.dropped.append(
                        f"page {page_no + 1}: {r.width:.0f}x{r.height:.0f} "
                        f"shape at {r.x0:.0f},{r.y0:.0f} spans a page break")
                    continue
            target = self.doc[page_no]
            scale = max(0.2, (bottom - top) / r.height) if r.height > 1 else 1.0
            move = fitz.Matrix(1, 0, 0, scale, sideways(r), top - r.y0 * scale)
            # Where the shape would now land. The rows it used to sit between
            # have moved and spread, so a rule that was in a gap can come down
            # on a line of words - which is what "a line runs through the
            # text" looks like. The words were set first, so they win.
            landed = fitz.Rect(r.x0, top, r.x1, max(bottom, top + r.height * scale))
            landed = fitz.Rect(landed.x0 + sideways(r), landed.y0,
                               landed.x1 + sideways(r), landed.y1)
            # touches(), not intersects(): a hairline is an empty Rect to
            # PyMuPDF, and RRIF's arrow shaft came down through "(if Yes)"
            # The whole of the words' box, descenders included: a line just
            # under a word is an underline the rebuild has already drawn with
            # the word itself, and carried as well it landed under the wrong
            # words once the paragraph re-wrapped (Est120's "timely delivery").
            if any(touches(landed, w) for w in self.ink.get(page_no, ())):
                self.dropped.append(
                    f"page {page_no + 1}: rule at {r.x0:.0f},{r.y0:.0f} would "
                    f"have crossed the words now set there")
                continue
            shape = target.new_shape()
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
                         fill_opacity=1 if d.get("fill_opacity") is None
                         else d["fill_opacity"])
            shape.commit(overlay=False)
            drawn += 1

        # Images go the same way as the rules: each follows the row it sat
        # in - the scanned signature beside "Acknowledged by", the wordmark at
        # the foot of a CRA form. Letterheads were set before the rows were,
        # so they are skipped here.
        for info in page.get_image_info(xrefs=True):
            key = image_key(page, info)
            if key in self.handled:
                continue
            r = fitz.Rect(info["bbox"])
            where = f"page {page.number + 1} image at {r.x0:.0f},{r.y0:.0f}"
            if not info.get("xref"):
                self.lost_images.append(f"{where}: drawn inline, cannot be lifted")
                continue
            top, page_no = follow(r.y0)
            if page_no >= self.doc.page_count:
                self.lost_images.append(f"{where}: its row went nowhere")
                continue
            target = self.doc[page_no]
            # an image that trailed the last row keeps its distance below it,
            # but never off the foot of the sheet
            top = max(self.top / 2, min(top, target.rect.height - self.bottom / 2 - r.height))
            dx = sideways(r)
            try:
                target.insert_image(fitz.Rect(r.x0 + dx, top, r.x1 + dx, top + r.height),
                                    pixmap=image_of(page.parent, info["xref"]),
                                    overlay=True)
            except Exception as e:
                self.lost_images.append(f"{where}: {e}")
                continue
            self.placed += 1
            self.image_log["carried with its row"] += 1
        return drawn


class Rebuild:
    """Sets rows out again at the target size, fields and all.

    Text is fixed: it needs the width it needs. Fields are elastic - a blank
    is a place to write, not a measurement - so when a row is too wide for
    the column the fields give up the difference between them, down to a
    width still worth writing in."""

    MIN_FIELD = 42.0

    def __init__(self, doc, fonts, size=10.0, margin=40.0):
        self.doc, self.fonts, self.size, self.margin = doc, fonts, size, margin
        self.fields = []
        self.page = None
        self.new_page()

    def new_page(self):
        self.page = self.doc.new_page(width=612, height=792)
        self.left, self.right = self.margin, 612 - self.margin
        self.y = self.margin

    def face(self, bold, italic):
        return self.fonts["bolditalic" if bold and italic else "bold" if bold
                          else "italic" if italic else "regular"]

    def width_of(self, item):
        if item["what"] == "field":
            return (self.size * 1.1 if item["kind"] == "CheckBox"
                    else max(self.MIN_FIELD, item["rect"].width))
        font = self.face(item["bold"], item["italic"])[2]
        # as it will be drawn: the trailing underscores a form types to draw
        # its own line come off, and measuring them kept a label's field off
        # the end of the row by less than a point
        return font.text_length(self.shown(item), self.size)

    @staticmethod
    def shown(item):
        return item["text"].rstrip("_ ").rstrip() or item["text"]

    def split(self, item, column):
        """One item, in pieces no wider than the column."""
        if item["what"] != "text" or self.width_of(item) <= column:
            return [item]
        font = self.face(item["bold"], item["italic"])[2]
        out, line = [], ""
        for word in item["text"].split():
            trial = (line + " " + word).strip()
            if font.text_length(trial, self.size) > column and line:
                out.append(dict(item, text=line))
                line = word
            else:
                line = trial
        if line:
            out.append(dict(item, text=line))
        return out

    def row(self, items, lead=1.45):
        """Put one row down, wrapping onto more lines if it must."""
        column = self.right - self.left
        # A row can hold a run of prose as one of its items - a bullet and its
        # sentence, say - and that item alone can be wider than the column.
        # Break it into pieces that fit before flowing, or it runs off the page.
        items = [piece for it in items for piece in self.split(it, column)]
        # A row of labels and blanks belongs on one line: the blanks are
        # elastic, so try squeezing them to their minimum before breaking the
        # row - otherwise "Contract Number:" keeps its label and loses its
        # line to the row below.
        tight = sum((self.MIN_FIELD if x["what"] == "field"
                     and x["kind"] != "CheckBox" else self.width_of(x)) + 4
                    for x in items)
        if tight <= column:
            lines = [items]
        else:
            line, lines = [], []
            for it in items:
                if line and sum(self.width_of(x) + 4 for x in line + [it]) > column:
                    lines.append(line)
                    line = []
                line.append(it)
            if line:
                lines.append(line)
        for group in lines:
            fixed = sum(self.width_of(x) for x in group if x["what"] == "text")
            gaps = 4 * max(0, len(group) - 1)
            elastic = [x for x in group if x["what"] == "field"
                       and x["kind"] != "CheckBox"]
            spare = column - fixed - gaps - sum(
                self.size * 1.1 for x in group if x["what"] == "field"
                and x["kind"] == "CheckBox")
            each = max(self.MIN_FIELD, spare / len(elastic)) if elastic else 0
            height = self.size * lead
            if self.y + height > 792 - self.margin:
                self.new_page()
            base = self.y + self.size
            x = self.left
            for it in group:
                if it["what"] == "text":
                    alias, path, font = self.face(it["bold"], it["italic"])
                    text = self.shown(it)
                    self.page.insert_text(fitz.Point(x, base), text, fontname=alias,
                                          fontfile=path, fontsize=self.size)
                    x += font.text_length(text, self.size) + 4
                elif it["kind"] == "CheckBox":
                    box = fitz.Rect(x, base - self.size * 0.85,
                                    x + self.size * 0.85, base + self.size * 0.05)
                    self.field(box, it)
                    x += self.size * 1.1 + 4
                else:
                    wide = min(each, self.right - x)
                    box = fitz.Rect(x, base - self.size, x + wide, base + 2)
                    self.page.draw_line(fitz.Point(x, base + 2),
                                        fitz.Point(x + wide, base + 2),
                                        color=(0, 0, 0), width=0.6)
                    self.field(box, it)
                    x += wide + 4
            self.y += height

    def field(self, rect, item):
        """Put the form field back, with the name and type it had."""
        w = fitz.Widget()
        w.rect = rect
        w.field_name = item["name"] or f"field_{len(self.fields)}"
        w.field_type = item["type"]
        if item["kind"] == "CheckBox":
            w.border_width = 0.8
            w.border_color = (0, 0, 0)
        self.page.add_widget(w)
        self.fields.append(w.field_name)
        for twin in item.get("twins", ()):
            self.field(rect, twin)

    def table(self, rows):
        """A grid: every label keeps its own cell, with its box beside it."""
        column = self.right - self.left
        pairs = max(sum(1 for i in r if i["what"] == "text") for r in rows)
        alias, path, font = self.face(False, False)
        labels = [i["text"] for i in rows[0] if i["what"] == "text"]
        widths = [font.text_length((labels[k] if k < len(labels) else "") + " ",
                                   self.size * 0.85) for k in range(pairs)]
        cell = (column - sum(widths)) / pairs if pairs else 0
        height = self.size * 1.8
        for row in rows:
            if self.y + height > 792 - self.margin:
                self.new_page()
            items, x, k = list(row), self.left, 0
            while items and k < pairs:
                label = items.pop(0) if items[0]["what"] == "text" else None
                box = items.pop(0) if items and items[0]["what"] == "field" else None
                wide = widths[k] + cell
                frame = fitz.Rect(x, self.y, x + wide, self.y + height)
                self.page.draw_rect(frame, color=(0, 0, 0), width=0.6)
                if label:
                    self.page.insert_text(fitz.Point(x + 3, self.y + self.size * 1.15),
                                          label["text"], fontname=alias, fontfile=path,
                                          fontsize=self.size * 0.85)
                if box:
                    self.field(fitz.Rect(x + widths[k], self.y + 1,
                                         frame.x1 - 1, frame.y1 - 1), box)
                x += wide
                k += 1
            self.y += height
        self.y += 4

    def signature(self, rules, captions=None):
        """Lines to sign on, with their captions underneath, spread across the
        column in the proportions the form used."""
        column = self.right - self.left
        # A slot can hold both a field and the underscores the form typed to
        # draw its line, at the same place: group by where they sit, or the
        # slots collapse into each other.
        slots = []
        for item in sorted(rules, key=lambda i: i["rect"].x0):
            if slots and item["rect"].x0 - slots[-1][0] < 12:
                slots[-1][1].append(item)
            else:
                slots.append([item["rect"].x0, [item]])
        rules = [next((i for i in group if i["what"] == "field"), group[0])
                 for _, group in slots]
        spots = [x for x, _ in slots]
        if len(spots) == 1:
            # one line on its own keeps the side of the page it sat on
            page_width = self.page.rect.width
            x0 = self.left + (spots[0] / page_width) * column
            starts = [x0, self.right + 12]
        else:
            span = max(1.0, max(i["rect"].x1 for i in rules) - spots[0])
            starts = [self.left + (x - spots[0]) / span * column for x in spots]
            starts.append(self.right + 12)
        height = self.size * (3.2 if captions else 2.0)
        if self.y + height > 792 - self.margin:
            self.new_page()
        base = self.y + self.size * 1.2
        alias, path, font = self.face(False, False)
        for k, item in enumerate(rules):
            x0, x1 = starts[k], starts[k + 1] - 12
            self.page.draw_line(fitz.Point(x0, base), fitz.Point(x1, base),
                                color=(0, 0, 0), width=0.6)
            if item["what"] == "field":
                self.field(fitz.Rect(x0, base - self.size * 1.2, x1, base - 1), item)
            if captions and k < len(captions):
                at = self.size * 0.9
                words, line, rows = captions[k]["text"].split(), "", []
                for word in words:
                    trial = (line + " " + word).strip()
                    if font.text_length(trial, at) > (x1 - x0) and line:
                        rows.append(line)
                        line = word
                    else:
                        line = trial
                if line:
                    rows.append(line)
                for n, piece in enumerate(rows[:2]):
                    self.page.insert_text(fitz.Point(x0, base + at * (1.1 + n)), piece,
                                          fontname=alias, fontfile=path, fontsize=at)
                height = max(height, self.size * 2 + at * (1.1 + len(rows[:2])))
        self.y += height

    def keep(self, height):
        """Start a page if a heading would be left at the foot of this one."""
        if self.y + height > 792 - self.margin:
            self.new_page()

    def bar(self, text):
        if self.y + self.size * 2 > 792 - self.margin:
            self.new_page()
        alias, path, font = self.face(True, False)
        box = fitz.Rect(self.left, self.y, self.right, self.y + self.size * 1.6)
        self.page.draw_rect(box, color=None, fill=BAR_GREY)
        self.page.insert_text(fitz.Point(self.left + 4, self.y + self.size * 1.2),
                              text, fontname=alias, fontfile=path, fontsize=self.size)
        self.y = box.y1 + 5

    def prose(self, text, bold=False, italic=False, indent=0.0):
        alias, path, font = self.face(bold, italic)
        words, line = text.split(), ""
        rows = []
        for word in words:
            trial = (line + " " + word).strip()
            if font.text_length(trial, self.size) > self.right - self.left - indent and line:
                rows.append(line)
                line = word
            else:
                line = trial
        if line:
            rows.append(line)
        for row in rows:
            if self.y + self.size * 1.3 > 792 - self.margin:
                self.new_page()
            self.page.insert_text(fitz.Point(self.left + indent, self.y + self.size * 0.9),
                                  row, fontname=alias, fontfile=path, fontsize=self.size)
            self.y += self.size * 1.3


def touches(a, b):
    """Do two boxes overlap, counting a hairline as a box?

    A rule is drawn as a line and has no height at all, and PyMuPDF calls a
    zero-height rectangle empty - an empty rectangle intersects nothing. So a
    hairline tested with intersects() is never found inside any zone, however
    squarely it sits in one, and every rule the form drew under a field it
    already had came through a second time on top of the rebuild's own."""
    return (a.x0 <= b.x1 and b.x0 <= a.x1
            and a.y0 <= b.y1 and b.y0 <= a.y1)


def say(line):
    """Print a report line even when the console cannot spell it: a form that
    carries a character Windows has no code page for must still be reported,
    not crash the run half way through its own findings."""
    enc = sys.stdout.encoding or "utf-8"
    print(line.encode(enc, "replace").decode(enc))


BOXED = {}           # (file id, widget xref) -> does it draw its own border


RULES_OF = {}


def drawn_round(page, r, slack=4.0):
    """Does the page itself rule a box snugly round `r`: a line along its top
    and its bottom, each across most of its width, and an upright at each end?

    Snug both ways: the box's own edges, no longer than the field. A table's
    row lines run its width and its uprights its height, and a field in a
    cell is the grid's to draw - read as a box of its own it got a second
    one inside its cell."""
    # (the file's name too: an id is reused once a document is closed)
    key = (id(page.parent), page.parent.name, page.parent.page_count, page.number)
    if key not in RULES_OF:
        # each box's own edges, before end-to-end pieces are joined: the
        # TFSA's Birthdate and SIN boxes, side by side, share a top line
        RULES_OF[key] = rules_of(page, merged=False) + field_boxes(page, form_grids(page))
    hs, vs, grids, boxes = RULES_OF[key]
    # a box the form ruled round a field (a lattice of nothing but fields)
    if any(c.contains(fitz.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)) for c in boxes):
        return True
    # a field in a table is the grid's: the grid draws its cells again
    if any(fitz.Rect(g["box"]).contains(fitz.Rect(r.x0 + 1, r.y0 + 1, r.x1 - 1, r.y1 - 1))
           for g in grids):
        return False
    need = 0.85 * r.width

    def across(y):
        return any(abs(at - y) <= slack and min(hi, r.x1) - max(lo, r.x0) >= need
                   and hi - lo <= r.width + 3 * slack for at, lo, hi in hs)

    # (uprights may run on: boxes stacked one under another - an address's
    # three lines - share their sides. A table across columns is still kept
    # out by its row lines, which run the width of the table.)
    def upright(x):
        return any(abs(at - x) <= slack and lo <= r.y0 + slack and hi >= r.y1 - slack
                   for at, lo, hi in vs)

    return across(r.y0) and across(r.y1) and upright(r.x0) and upright(r.x1)


def field_boxes(page, grids):
    """The grids that are only boxes round fields, and the rest.

    A lattice whose every cell holds a field and no words is not a table: it
    is the box, or a stack of boxes, the form ruled round its answers - the
    Concentra applications' Branch Number, Member Number and Contract Number
    are three of them side by side. Taken as tables they were laid out apart
    from their row, and their fields, marked as written in a cell, got
    neither line nor box: they vanished. Returns (tables, box rects)."""
    words = page.get_text("words")
    rects = [fitz.Rect(w.rect) for w in page.widgets()
             if w.field_type == fitz.PDF_WIDGET_TYPE_TEXT]
    mid = lambda r: fitz.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)
    tables, boxes = [], []
    for g in grids:
        cells = [fitz.Rect(x0, top, x1, foot) for top, foot, cuts in g["bands"]
                 for x0, x1 in zip(cuts, cuts[1:])]
        only_fields = cells and all(
            any(c.contains(mid(r)) for r in rects)
            and not any(c.contains(fitz.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2))
                        for w in words)
            for c in cells)
        if only_fields:
            boxes += cells
        else:
            tables.append(g)
    return tables, boxes


def split_beside_grids(rows, grids, pad=6.0):
    """A grid takes only what stands inside it.

    A row is read across the whole page, and a table that shares its rows
    with things beside it - the TFSA's address boxes with the telephone boxes
    to their right, its Days/Months/Years term grid with "Variable", "Term",
    Interest Rate and Maturity Date either side - pulled the lot in: the
    telephones came back inside the address box and the term grid scrambled.
    What stands left of the grid is set just before it, what stands right of
    it just after, each row of it as it was."""
    for g in sorted(grids, key=lambda g: g["box"].y0):
        b = g["box"]
        cx = lambda i: (i["rect"].x0 + i["rect"].x1) / 2
        inside = lambda i: b.x0 - pad <= cx(i) <= b.x1 + pad
        span = [k for k, r in enumerate(rows)
                if r and b.y0 - 2 <= min(i["base"] for i in r) <= b.y1 + 2
                and any(inside(i) for i in r)]
        if not span or not any(not inside(i) for k in span for i in rows[k]):
            continue
        before, after = [], []
        for k in span:
            left = [i for i in rows[k] if not inside(i) and cx(i) < b.x0]
            right = [i for i in rows[k] if not inside(i) and cx(i) > b.x1]
            rows[k] = [i for i in rows[k] if inside(i)]
            if left:
                before.append(left)
            if right:
                after.append(right)
        first, last = span[0], span[-1]
        rows[first:last + 1] = before + rows[first:last + 1] + after
    return [r for r in rows if r]


def boxed_field(page, w, floor=120.0):
    """Does this field show as a box to write in, rather than a line?

    Nothing in the widget says so. The border is painted by the appearance
    the viewer builds, and a field that renders as a full rectangle carries
    exactly the same dictionary as one that renders as a single rule - all
    1839 text fields in the corpus have no border colour set. So it is asked
    the only way that answers it: render the field's patch of page with the
    annotations and without, and see whether the field itself puts ink along
    BOTH the top and the bottom edge. A line inks one edge; a box inks two.

    Only wide fields are worth asking about, and each answer is kept."""
    r = fitz.Rect(w.rect)
    key = (id(page.parent), w.xref)
    if key in BOXED:
        return BOXED[key]
    # Or the page draws the box, and the field only fills it: the Concentra
    # TFSA/RRSP applications rule a rectangle round every answer, Branch
    # Number to Postal code, and their fields have no border of their own.
    # Read as lines, the rebuild drew nothing round them and they vanished.
    if r.height >= 6 and r.width >= 12 and drawn_round(page, r):
        BOXED[key] = True
        return True
    if r.width < floor or r.height < 4:
        return False
    clip = fitz.Rect(r.x0 - 1, r.y0 - 1, r.x1 + 1, r.y1 + 1)
    m = fitz.Matrix(3, 3)
    try:
        on = page.get_pixmap(matrix=m, clip=clip, annots=True)
        off = page.get_pixmap(matrix=m, clip=clip, annots=False)
    except Exception:
        BOXED[key] = False
        return False

    def inked(pm, y):
        wide = pm.width
        dark = sum(1 for x in range(0, wide, 2)
                   if sum(pm.pixel(x, y)[:3]) / 3 < 160)
        return dark / max(1, wide // 2)

    got = (inked(on, 2) - inked(off, 2) > 0.6
           and inked(on, on.height - 3) - inked(off, off.height - 3) > 0.6)
    BOXED[key] = got
    return got


def image_of(doc, xref):
    """The image at `xref` as a pixmap ready to set on another page.

    A logo is usually drawn with a soft mask - the transparency round the
    lettering - and a pixmap read straight from the xref loses it, so the
    logo comes out on a white or black block. The mask is put back, and a
    CMYK image is turned to RGB first, because the mask can only be attached
    to an image without alpha of its own."""
    pix = fitz.Pixmap(doc, xref)
    if pix.colorspace and pix.colorspace.n == 4:
        pix = fitz.Pixmap(fitz.csRGB, pix)
    kind, value = doc.xref_get_key(xref, "SMask")
    if kind == "xref" and not pix.alpha:
        mask = fitz.Pixmap(doc, int(value.split()[0]))
        if mask.alpha or (mask.colorspace and mask.colorspace.n != 1):
            mask = fitz.Pixmap(fitz.csGRAY, mask)
        pix = fitz.Pixmap(pix, mask)
    return pix


def top_images(page):
    """The images a page carries in its letterhead band, with their xrefs."""
    return [i for i in page.get_image_info(xrefs=True)
            if fitz.Rect(i["bbox"]).y0 < page.rect.height * 0.25]


def image_key(page, info):
    return (page.number, info.get("xref"),
            tuple(round(v, 1) for v in fitz.Rect(info["bbox"])))


def letterhead(page):
    """Does this page of the form carry a logo at its top?"""
    return any(fitz.Rect(i["bbox"]).y0 < page.rect.height * 0.25
               for i in R.printed(page).get_image_info())


def top_title(page, rows):
    """The heading this page of the form opens with, if it has one.

    A heading is a short bold line in the top fifth, set clearly larger than
    the page's OWN body text. Measured against the body rather than a fixed
    size, because these titles come out at 10.98pt against an 8pt body and a
    threshold of 11 misses them by two hundredths of a point."""
    sizes = Counter(round(i["size"], 1) for r in rows for i in r
                    if i["what"] == "text" and i["text"].strip())
    if not sizes:
        return ""
    body = sizes.most_common(1)[0][0]
    found = []
    for r in rows:
        if min(i["base"] for i in r) >= page.rect.height * 0.22:
            continue
        for i in sorted(r, key=lambda i: i["rect"].x0):
            # bold, or plainly bigger: the wire instructions stapled behind
            # the transfer authorization title themselves in 14.4pt regular
            # over a 12pt body, and read as nothing they ran on straight
            # after the form, half way down its last sheet
            if (i["what"] == "text" and 3 < len(i["text"]) < 70
                    and (i["bold"] and i["size"] >= body + 1.5 or i["size"] >= body + 2.0)):
                found.append(" ".join(i["text"].split()))
    if found:
        return " ".join(found)
    # or a block of bold capitals centred on the page, at the body's own
    # size: RRIF's "LIBRO CREDIT UNION / RETIREMENT INCOME FUND / DECLARATION
    # OF TRUST" opens its terms that way, and read as no heading at all the
    # terms ran straight on from the application's signing block
    block = []
    for r in rows:
        if min(i["base"] for i in r) >= page.rect.height * 0.22:
            break
        texts = [i for i in r if i["what"] == "text" and i["text"].strip()]
        if (len(texts) == 1 and texts[0]["bold"] and 3 < len(texts[0]["text"].strip()) < 70
                and texts[0]["text"].upper() == texts[0]["text"]
                and abs((texts[0]["rect"].x0 + texts[0]["rect"].x1) / 2 - page.rect.width / 2) <= 12):
            block.append(" ".join(texts[0]["text"].split()))
        elif block:
            break
    return " ".join(block) if len(block) >= 2 else ""


def starts_document(page, rows, before="", badged=None):
    """Is this page of the form the start of another document?

    These forms are several documents stapled together - a Direction and a
    Security Agreement, an Application and its Terms - and the second should
    begin on a new sheet rather than half way down the one before.

    A logo does not say so on its own: Est107 puts its letterhead on every
    page, and breaking on that gave it a sheet that stopped a quarter of the
    way down. Nor does a heading on its own: Est120 repeats its title at the
    top of every page. What says so is a heading that is DIFFERENT from the
    heading the page before opened with - a new document announces itself by
    changing the name at the top."""
    now = top_title(page, rows)
    if now and (" ".join(now.split()).casefold()
                != " ".join(before.split()).casefold()):
        return True
    # A letterhead on SOME pages marks where the documents begin - Est120
    # puts one on its declaration and again on the covering letter, and
    # nowhere else. A letterhead on every page says nothing, which is why
    # the logo alone was never enough.
    if bool(badged) and letterhead(page):
        return True
    # A later page whose words open a numbered list at 1 starts a new block
    # of terms. PAD repeats one title on all three pages, so nothing above
    # told the terms on its third from the signing page before them, and the
    # terms began half way down that page.
    if page.number > 0:
        for row in rows[:4]:
            texts = sorted((i for i in row if i["what"] == "text"), key=lambda i: i["rect"].x0)
            if texts and re.match(r"\(?1[.)](\s|$)", texts[0]["text"].lstrip()):
                return True
    return False


def is_heading(row, bars=(), strong_only=False):
    """A heading: a shaded bar, or a short bold line with nothing to fill in.

    "6. INSURANCE" is a heading whether or not the form shaded it."""
    texts = [i for i in row if i["what"] == "text"]
    if any(i["what"] == "field" for i in row) or not texts:
        return False
    if any(b.y0 - 3 <= texts[0]["rect"].y0 and texts[0]["rect"].y1 <= b.y1 + 3
           for b in bars):
        return True
    # A bold line that starts in lower case is the rest of a sentence, not a
    # heading: Hatching Egg's NOTE runs on to "including O.B.H.E.C.C.'s
    # authority to cancel quota.", and taken for a heading it carried itself
    # and everything after it to a sheet of its own.
    first = texts[0]["text"].lstrip()[:1]
    if first.isalpha() and first.islower():
        return False
    if (len(row) <= 2 and all(i["bold"] for i in texts)
            and sum(len(i["text"]) for i in texts) <= 60):
        return True
    if strong_only:
        # the softer kinds below open a section, but do not end the one
        # before: RRSP Beneficiary's "CAUTION: ..." runs on through the bold
        # note and the signing block after it, and is moved as one
        return False
    # a run-in "CAUTION:" or "NOTE:" with bold words after it
    lead = sorted(texts, key=lambda i: i["rect"].x0)
    if (len(lead) == 2 and lead[0]["bold"] and lead[1]["bold"]
            and lead[0]["text"].strip().endswith(":") and len(lead[0]["text"].strip()) <= 12):
        return True
    # A bold paragraph that opens a section is its heading too, for keeping
    # the section in one piece: Est115's "This Declaration must be sworn by
    # each of the adult heirs ... If necessary, attach Form EST-130" opens
    # the declarations, and split across a page break it read as a sentence
    # cut in half with the declarations it introduces on the next sheet.
    return (len(texts) == 1 and texts[0]["bold"] and not texts[0].get("runs")
            and 60 < len(texts[0]["text"]) <= 400 and first.isupper())


def page_box(page, margin=36.0, hard=14.0):
    """The box the rebuild may write in: the house margin, or the form's own
    edges where the form itself was set tighter than that. Squeezing a form
    that already ran to a quarter-inch margin into a half-inch one takes away
    width it cannot spare, and widening one that did not is not the rebuild's
    business."""
    ink = fitz.Rect()
    for b in R.printed(page).get_text("dict")["blocks"]:
        if not b.get("type"):
            ink |= fitz.Rect(b["bbox"])
    if ink.is_empty:
        return fitz.Rect(margin, margin, page.rect.width - margin,
                         page.rect.height - margin)
    return fitz.Rect(max(hard, min(margin, ink.x0)), margin,
                     min(page.rect.width - hard,
                         max(page.rect.width - margin, ink.x1)),
                     page.rect.height - margin)


def verify(doc, said, was, box):
    """Read the saved file back and say what went wrong with it.

    A rebuild that quietly drops a paragraph or marches a column off the edge
    of the sheet is worse than one that fails outright, because nobody finds
    out until a member is holding the form. So: every word the rebuild drew
    has to come back out of the file it wrote, every field has to still be
    there under its own name, and nothing may sit outside the margins."""
    issues, got = [], Counter()
    for page in doc:
        # read the printed page, not the fields drawn on top of it: a signing
        # button carries its own label and never came from the form's text
        for b in R.printed(page).get_text("dict")["blocks"]:
            if b.get("type"):
                continue
            for line in b["lines"]:
                for span in line["spans"]:
                    got.update(re.findall(r"[^\s_]+", span["text"]))
                    r = fitz.Rect(span["bbox"])
                    if (r.x1 > box.x1 or r.x0 < box.x0
                            or r.y1 > page.rect.height - box.y0):
                        issues.append(("TEXT_PAST_EDGE",
                                       f"page {page.number + 1}: "
                                       f"{span['text'][:40]!r} at "
                                       f"{r.x0:.0f},{r.y0:.0f}"))
    lost, gained = said - got, got - said
    if lost:
        issues.append(("TEXT_LOST", "drawn but not in the file: "
                       + ", ".join(sorted(lost))[:300]))
    if gained:
        issues.append(("TEXT_GARBLED", "in the file but never drawn: "
                       + ", ".join(sorted(gained))[:300]))
    now = Counter((w.field_name, w.field_type_string)
                  for page in doc for w in page.widgets())
    for name, kind in sorted((was - now)):
        issues.append(("FIELD_LOST", f"{name} ({kind})"))
    for name, kind in sorted((now - was)):
        issues.append(("FIELD_GAINED", f"{name} ({kind})"))
    return issues


def proofs(doc, args, sheet, before):
    """One image per rebuilt page: the page of the form it came from on the
    left, the rebuilt page on the right, with any wording the table replaced
    shaded on both sides so a reviewer can check the swap rather than hunt
    for it. The rebuild re-paginates, so the two sides are not page-for-page;
    each is captioned with which page of the form it started from."""
    os.makedirs(args.png, exist_ok=True)
    title = os.path.splitext(os.path.basename(args.input))[0]
    out = fitz.open(args.output)
    made, scroll = 0, []
    for pno in range(out.page_count):
        came = min(sheet.origin.get(pno, 0), doc.page_count - 1)
        src = doc[came]
        # number each change, and show the same number on both sides. The
        # legend under the pages says what each one was, so nobody has to
        # open the CSV to read a proof.
        now = [(r, "swap", n) for p, n, r in sheet.swaps if p == pno]
        shown = {n for _, _, n in now}
        was = [(e["rect"], "swap", e["n"]) for e in before
               if e["page"] == came and e["n"] in shown]
        seen, legend = set(), []
        for e in sorted(before, key=lambda e: e["n"]):
            if e["n"] in shown and e["n"] not in seen:
                seen.add(e["n"])
                legend.append("%d.  %r  ->  %r" % (e["n"], e["was"], e["now"]))
        note = "   yellow = wording replaced" if (was or now) else ""
        one = None if args.proof_scroll else os.path.join(
            args.png, f"{title} [rebuilt] p{pno + 1}_proof.png")
        scroll.append(R.save_proof(
            src.get_pixmap(dpi=args.png_dpi),
            out[pno].get_pixmap(dpi=args.png_dpi), one,
            f"{os.path.basename(args.input)}   rebuilt page "
            f"{pno + 1 + args.kept} of {out.page_count + args.kept}, from page "
            f"{came + 1 + args.kept} of {doc.page_count + args.kept}"
            f"   (form left, rebuild right)" + note,
            before_marks=was, after_marks=now, dpi=args.png_dpi,
            legend=legend))
        made += 1
    if args.proof_scroll and scroll:
        R.join_down(scroll, os.path.join(args.png,
                                         f"{title} [rebuilt] all_proof.png"))
    return made


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("-o", "--output", default="rebuilt.pdf")
    ap.add_argument("--size", type=float, default=10.0)
    ap.add_argument("--dump", action="store_true", help="print what it found and stop")
    ap.add_argument("--png", metavar="DIR",
                    help="folder for before/after proof images, one per rebuilt "
                         "page, with replaced wording shaded on both sides")
    ap.add_argument("--png-dpi", type=int, default=110)
    ap.add_argument("--proof-scroll", action="store_true",
                    help="one tall proof per form instead of one per page: "
                         "every page joined top to bottom, form on the left, "
                         "rebuild on the right, read in a single scroll")
    ap.add_argument("--skip-third-party", action="store_true",
                    help="don't put the Libro header on a form someone else "
                         "puts out (a marketing board, the CRA)")
    ap.add_argument("--signing", action="store_true",
                    help="bring each member signing area to the client's "
                         "standard: Name | Authorized Signature | "
                         "Date per member, then Authorizing Staff Member | Date")
    ap.add_argument("--merge-fields", action="store_true",
                    help="take mail-merge tags out of the text and put a text "
                         "box in each one's place")
    ap.add_argument("--relabel", action="store_true",
                    help="swap whole standalone labels for the house wording in "
                         "relabel.txt (Witness -> Authorizing Staff Member, ...). "
                         "Words inside sentences are never touched; anything the "
                         "table doesn't cover is listed for a human")
    ap.add_argument("--relabel-table", default=R.RELABEL_TABLE, metavar="FILE",
                    help="where to read the table from, so a proposed list can "
                         "be tried without putting it in force "
                         "(default: relabel.txt)")
    ap.add_argument("--no-house", action="store_true", help=argparse.SUPPRESS)
    # the form's rows a first run found the signing block had no room after:
    # [[form page, top, bottom, members], ...] (see the end of main)
    ap.add_argument("--signing-room", default=None, help=argparse.SUPPRESS)
    # pages of the form kept ahead of the ones rebuilt, so the proof numbers
    # pages as the whole form has them
    ap.add_argument("--kept", type=int, default=0, help=argparse.SUPPRESS)
    # and whether those kept pages ask for a signature themselves
    ap.add_argument("--kept-signs", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    # A form that opens with another issuer's own pages - the Dairy Farmers'
    # direction ahead of Libro's security agreement - keeps those pages as
    # the issuer printed them, untouched, and only Libro's pages behind them
    # are rebuilt. The client's own hand-cleaned Milk form did exactly this.
    if args.skip_third_party and not args.no_house:
        whole = fitz.open(args.input)
        marks, issuers = R.house_marks(R.HOUSE_MARKS), R.load_issuers(R.ISSUERS)
        theirs_ = [R.third_party(p, 100.0, marks, issuers)[0] for p in whole]
        k = 0
        while k < len(theirs_) and theirs_[k]:
            k += 1
        if 0 < k < len(theirs_) and not any(theirs_[k:]):
            import subprocess, tempfile
            tmp = tempfile.mkdtemp(prefix="rebuild_")
            rest_in = os.path.join(tmp, os.path.basename(args.input))
            rest_out = os.path.join(tmp, "rebuilt_" + os.path.basename(args.input))
            rest = fitz.open()
            rest.insert_pdf(whole, from_page=k, widgets=True)
            rest.save(rest_in)
            argv = [a for a in sys.argv[1:]]
            argv = [rest_in if a == args.input else a for a in argv]
            if "-o" in argv:
                argv[argv.index("-o") + 1] = rest_out
            elif "--output" in argv:
                argv[argv.index("--output") + 1] = rest_out
            else:
                argv += ["-o", rest_out]
            # the issuer's pages can be where the form asks for its signature
            # - Transfer In's "Member's Signature" is on Kawartha's page - and
            # the pages behind them are not a form that asks for none
            signs = R.asks_for_signature(whole)
            r = subprocess.run([sys.executable, os.path.abspath(__file__)] + argv
                               + ["--no-house", "--kept", str(k)]
                               + (["--kept-signs"] if signs else []),
                               capture_output=True, text=True, encoding="utf-8")
            sys.stdout.write(r.stdout)
            # (it exits non-zero whenever its checks found anything; only one
            # that never got to them has nothing to put the form back with)
            if r.returncode and ("checks:" not in r.stdout or not os.path.exists(rest_out)):
                sys.stderr.write(r.stderr)
                sys.exit(r.returncode)
            # the form itself, cut back to its issuer's pages, keeps their
            # fields: copied into a new file they came through as none
            final = fitz.open(args.input)
            final.delete_pages(k, len(theirs_) - 1)
            final.insert_pdf(fitz.open(rest_out), widgets=True)
            final.save(args.output, garbage=3, deflate=True)
            was = sum(1 for p in whole for _w in p.widgets())
            now = sum(1 for p in fitz.open(args.output) for _w in p.widgets())
            if now != was:
                say(f"FIELD_LOST: {was} field(s) in the form, {now} once its own pages "
                    f"were put back in front of the rebuild")
            if args.png:
                # The rebuild's proof showed only the pages it rebuilt, so
                # the kept pages were missing from it. They go in front,
                # the form and what was saved side by side.
                title = os.path.splitext(os.path.basename(args.input))[0]
                saved = fitz.open(args.output)
                panels = []
                for n in range(k):
                    one = None if args.proof_scroll else os.path.join(
                        args.png, f"{title} [kept] p{n + 1}_proof.png")
                    panels.append(R.save_proof(
                        whole[n].get_pixmap(dpi=args.png_dpi),
                        saved[n].get_pixmap(dpi=args.png_dpi), one,
                        f"{os.path.basename(args.input)}   page {n + 1} of "
                        f"{whole.page_count}   kept exactly as printed - the issuer's "
                        f"own page   (form left, saved right)", dpi=args.png_dpi))
                scroll = os.path.join(args.png, f"{title} [rebuilt] all_proof.png")
                if args.proof_scroll and os.path.exists(scroll):
                    from PIL import Image
                    R.join_down(panels + [Image.open(scroll).convert("RGB")], scroll)
            say(f"THIRD_PARTY_PAGES_KEPT: page(s) 1-{k} of the form are the issuer's own "
                f"and are kept exactly as printed; only page(s) {k + 1}-{len(theirs_)} were rebuilt")
            return

    doc = fitz.open(args.input)
    # read before anything touches the form: by the end of the rebuild its
    # calculation list is no longer there to read
    calc_order = calc_names(doc)
    fonts = R.resolve_fonts({})
    globals()["ARIAL"] = fonts["regular"][2]
    globals()["MERGE_NOTES"] = bool(getattr(args, "merge_fields", False))
    out = fitz.open()
    sheet = KeepShape(out, fonts, args.size, doc[0].rect)
    title = os.path.splitext(os.path.basename(args.input))[0]

    # A form someone else puts out - a marketing board, the CRA - is not
    # Libro's to re-brand, and stamping a Libro logo and title on one is
    # worse than leaving it alone. Same test the restyle uses.
    theirs, why = R.third_party(doc[0], 100.0, R.house_marks(R.HOUSE_MARKS),
                                R.load_issuers(R.ISSUERS))
    if theirs and args.skip_third_party:
        say(f"THIRD_PARTY_FORM: {why} - no Libro header added")
    elif theirs:
        say(f"THIRD_PARTY_FORM: {why} - a Libro header was added anyway; "
            f"pass --skip-third-party to leave it alone")

    # the house header, as the restyle puts it on
    house = not (theirs and args.skip_third_party) and not args.no_house
    logo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "libro_logo.png")
    logo_right = 36.0
    if house and os.path.exists(logo):
        pix = fitz.Pixmap(logo)
        logo_right = 36 + 63 * pix.width / pix.height
        sheet.page.insert_image(fitz.Rect(36, sheet.y, logo_right, sheet.y + 63),
                                filename=logo)
        sheet.placed += 1
        # the house logo stands in for whatever the form had at its head
        for info in top_images(doc[0]):
            sheet.handled.add(image_key(doc[0], info))
            sheet.image_log["replaced by the house logo"] += 1

    def set_letterhead(page):
        """Put the letterhead a page of the form opens with at the top of the
        sheet in hand, where it was, and keep rows off it."""
        for info in top_images(page):
            key = image_key(page, info)
            if key in sheet.handled or not info.get("xref"):
                continue
            try:
                sheet.reserve_image(image_of(doc, info["xref"]), fitz.Rect(info["bbox"]))
            except Exception as e:
                sheet.lost_images.append(f"page {page.number + 1} letterhead: {e}")
                sheet.handled.add(key)
                continue
            sheet.handled.add(key)
            sheet.image_log["set at the head of its sheet"] += 1

    # A form someone else issued keeps its own letterhead on the first sheet.
    if not house:
        set_letterhead(doc[0])
    alias, bold_path, bold_font = fonts["bold"]
    title = R.house_title(args.input)
    if house:
        # The title is the file's name, and some of them are long - "FHSA
        # Designation of SH, Ben with disclaimer (use until ON legislation
        # update)". Set at 14pt and hung off the right margin it runs back
        # across the logo. It breaks over lines and never starts left of the
        # logo; it stays at 14pt, the client's title size - the logo is taller
        # than three lines of it, so the header takes no more room.
        room = doc[0].rect.width - 36 - (logo_right + 14)
        # A letter carries the logo alone (house_title gives it no title).
        at, lines = 14.0, [title] if title else []
        if title and bold_font.text_length(title, at) > room:
            lines, line = [], ""
            for word in title.split():
                trial = (line + " " + word).strip()
                if bold_font.text_length(trial, at) > room and line:
                    lines.append(line)
                    line = word
                else:
                    line = trial
            if line:
                lines.append(line)
        for k, piece in enumerate(lines):
            sheet.page.insert_text(
                fitz.Point(doc[0].rect.width - 36 - bold_font.text_length(piece, at),
                           sheet.y + 20 + k * at * 1.2),
                piece, fontname=alias, fontfile=bold_path, fontsize=at)
        sheet.said.update(re.findall(r"[^\s_]+", title))
        sheet.y += max(74.0, 26 + len(lines) * at * 1.2)
    else:
        sheet.y += 4

    def shaded_bar(d):
        """A section bar is a fill you can SEE. A form lays white rectangles
        over its page to mask or to box things off - one has forty of them -
        and painting those the house grey shades every line on the sheet."""
        if not d.get("fill"):
            return False
        r = fitz.Rect(d["rect"])
        if not (6 < r.height < 30 and r.width > 60):
            return False
        return 0.05 < sum(d["fill"]) / len(d["fill"]) < 0.95

    # Each page's own bars. Gathered into one list, a bar from page 1 shaded
    # whatever row sat at its height on every other page: RRIF's terms came
    # out striped grey where its first page has its section headings.
    bars_of, fills_of = {}, {}
    for page in doc:
        drawn = R.printed(page).get_drawings()
        bars_of[page.number] = []
        for d in drawn:
            if shaded_bar(d):
                r = fitz.Rect(d["rect"])
                BAR_FILL[id(r)] = tuple(d["fill"])
                bars_of[page.number].append(r)
        # every fill you can see, whatever its shape, for shading table cells
        fills_of[page.number] = [(fitz.Rect(d["rect"]), tuple(d["fill"])) for d in drawn
                                 if d.get("fill") and d["type"] in ("f", "fs")
                                 and 0.05 < sum(d["fill"]) / len(d["fill"]) < 0.95
                                 and d["rect"].width > 6 and d["rect"].height > 6]
    if house:
        # Without the house header there is nothing for it to close off:
        # drawn anyway it ran across the top margin, through Milk's own logo.
        sheet.page.draw_line(fitz.Point(36, sheet.y - 8),
                             fitz.Point(doc[0].rect.width - 36, sheet.y - 8),
                             color=(0, 0, 0), width=1.0)
    # the house header, which the look checks leave out: it is new by design
    head_band = fitz.Rect(0, 0, doc[0].rect.width, sheet.y - 6)

    # what the rebuild draws for itself, and so must not also carry over:
    # the blank under a field, the box round a tick, the grid of a table and
    # the section bars. Carrying those as well is what put two rules under
    # "Branch:".
    zones = {page.number: [fitz.Rect(b) for b in bars_of[page.number]] for page in doc}
    for page in doc:
        for w in page.widgets():
            zones[page.number].append(fitz.Rect(w.rect) + (-3, -3, 3, 3))
    sheet.source = 0

    table = R.load_relabels(args.relabel_table) if args.relabel else {}
    declared_doc = R.declared_pages(doc)
    # A form that asks for no signature anywhere gets the client's standard
    # block added at the end of its fill-in part - before its terms, or after
    # its last row - unless it is a declaration, someone else's, or listed
    # never to get one. Room is kept for it as the form is laid out.
    adding = bool(args.signing and (house or args.no_house)
                  and R.signing_listed(args.input) and not R.signing_excluded(args.input)
                  and not R.is_declaration(doc) and not args.kept_signs
                  and not R.asks_for_signature(fitz.open("pdf", doc.tobytes()))
                  and not any(R.signing_areas(p)[0] for p in fitz.open("pdf", doc.tobytes())))
    block_need = sheet.size * (3.4 * (R.STANDARD_MEMBERS + 1) + 1.2) + 4
    adding_spot = None
    adding_page = R.block_spot(fitz.open("pdf", doc.tobytes()))[0] if adding else None
    # a text box put in place of a merge tag never takes a name in use
    TAKEN.update(w.field_name for p in doc for w in p.widgets())
    notes, before, dropped_feet = [], [], []
    foot_boxes, footers = footer_lines(doc), {}   # form page -> footer lines
    # Read every page once up front: which tables it ruled, what its rows
    # are, and whether it begins another document. The last of those has to
    # be known for the NEXT page as well as this one, so it cannot be worked
    # out lazily inside the loop.
    marks = [letterhead(p) for p in doc]
    some = 0 < sum(marks) < len(marks)      # a logo on some pages, not all
    # A logo on every page of the form is its letterhead, and every rebuilt
    # sheet gets it: the house logo on a Libro form, the form's own otherwise.
    if doc.page_count > 1 and all(marks):
        if house and os.path.exists(logo):
            sheet.heads = [(fitz.Pixmap(logo), fitz.Rect(36, sheet.top - 4, logo_right,
                                                          sheet.top + 59))]
        else:
            first = top_images(doc[0])
            sheet.heads = [(image_of(doc, i["xref"]), fitz.Rect(i["bbox"]))
                           for i in first if i.get("xref")]
        for p in doc:
            if p.number == 0:
                continue
            for info in top_images(p):
                sheet.handled.add(image_key(p, info))
                sheet.image_log["repeated on every sheet"] += 1
    read, seen_title = [], ""
    for page in doc:
        # (a lattice of nothing but fields is their boxes, not a table)
        grids = field_boxes(page, form_grids(page))[0]
        rows = align_stacks(merge_prose(unfold_columns(rows_with_fields(page, grids), page,
                                                       foot_boxes.get(page.number, ())),
                                        page_box(page).x1))
        # A field the form set inside a ruled cell - uprights either side of
        # it, no line of its own under it - is written in the cell. The rebuild
        # draws a line under every field; in a cell that line reads as one more
        # row of the table, which the AFT form was sent back for.
        shown = R.printed(page)
        verts = R.upright_edges(shown, visible_only=True)
        lines_here = R.flat_rules(shown)
        typed_words = shown.get_text("words")
        for row in rows:
            for i in row:
                if (i["what"] != "field" or i.get("kind") in ("CheckBox", "RadioButton")
                        or i.get("boxed")):
                    continue
                r = fitz.Rect(i["rect"])
                mid = (r.y0 + r.y1) / 2
                spans = [vx for vx, vy0, vy1 in verts if vy0 - 2 <= mid <= vy1 + 2]
                left = [vx for vx in spans if vx <= r.x0 + 3]
                right = [vx for vx in spans if vx >= r.x1 - 3]
                if not (left and right):
                    continue
                side_l, side_r = max(left), min(right)
                # the field's own line stops short of the cell's sides; the
                # cell's bottom edge, which a field can sit right on, runs
                # from one side to the other and is not a line to write on
                ruled = any(abs(y - r.y1) <= 4 and x0 < r.x1 and r.x0 < x1
                            and (x0 > side_l + 3 or x1 < side_r - 3)
                            for y, x0, x1, _, _ in lines_here)
                # or typed: LIRA Fed's "NAME: ______" inside its identification
                # box is a line of underscores, and read as a field in a cell
                # it came back with nothing to write on
                # ("ADDRESS:______" is typed as one word with its label)
                ruled = ruled or any(re.search(r"_{4,}", w[4])
                                     and abs(w[3] - r.y1) <= 6 and w[0] < r.x1 and r.x0 < w[2]
                                     for w in typed_words)
                if not ruled:
                    i["in_cell"] = True
        read.append({"grids": grids, "rows": rows,
                     "opens": starts_document(page, rows, seen_title, some)})
        seen_title = top_title(page, rows) or seen_title

    for pno, n_rows, box in UNFOLDED:
        zones[pno].append(box + (-4, -4, 4, 4))
        inside = [d for d in R.printed(doc[pno]).get_drawings()
                  if d["items"] and fitz.Rect(d["rect"]).intersects(box)]
        notes.append(("columns_unfolded",
                      f"page {pno + 1} of the form: {n_rows} row(s) set in two columns "
                      f"read as one, left column then right, and set across the page"
                      + (f"; {len(inside)} rule(s) drawn among them left out"
                         if inside else "")))
    # The letterhead's lines again at the top of a later page - Payroll's
    # "LIBRO CREDIT UNION / Payroll / Deduction Authorization Form / 217 York
    # St." - are the house header's to carry, as they are on the first page.
    # Read as rows of that page they came back halfway down the second sheet.
    band = doc[0].rect.height * 0.16
    said_ = lambda r: " ".join(" ".join(i["text"].split()) for i in r if i["what"] == "text")
    head_rows = {said_(r) for r in read[0]["rows"]
                 if r and all(i["what"] == "text" and i["rect"].y1 <= band for i in r)}
    head_rows.discard("")
    if head_rows:
        for n in range(1, len(read)):
            rows_ = read[n]["rows"]
            k = 0
            while (k < len(rows_) and rows_[k] and said_(rows_[k]) in head_rows
                   and all(i["rect"].y1 <= band for i in rows_[k])):
                k += 1
            # (two lines or more: one line alone - "LIBRO CREDIT UNION" over
            #  RRIF's Declaration of Trust - is the start of a document's own
            #  title, which the client wants kept on its new page)
            if k >= 2:
                read[n]["rows"] = rows_[k:]
                notes.append(("letterhead_repeat_dropped",
                              f"page {n + 1} of the form: {k} line(s) of the letterhead "
                              f"again - the header carries them"))
    running = running_block(doc, read)
    running_fields = Counter()
    if running:
        k, on = running
        block = read[on[0]]["rows"][:k]
        # or the letterhead's own lines, set at the top of every page in the
        # band the house header takes on the first: Payroll's "LIBRO CREDIT
        # UNION / Payroll / Deduction Authorization Form / 217 York St." came
        # back halfway down the second sheet
        top_band = doc[on[0]].rect.height * 0.16
        letterhead_text = (k <= 5 and all(i["what"] == "text" and i["rect"].y1 <= top_band
                                          for r in block for i in r))
        if (not any(i["what"] == "field" for r in block for i in r)
                and (letterhead_text
                     or (k <= 3 and all(i["what"] != "text" or i["size"] >= 11.5 or i["bold"]
                                        for r in block for i in r)))):
            # the form's title again at the head of each page: the house
            # header on the first sheet carries it, as it does page 1's
            for n in on:
                read[n]["rows"] = read[n]["rows"][k:]
            notes.append(("running_title_dropped",
                          " / ".join(i["text"] for r in block for i in r if i["what"] == "text")
                          + f" - the form's title at the head of pages "
                          f"{', '.join(str(n + 1) for n in on)}; the header carries it"))
            running = None
    if running:
        k, on = running
        block = read[on[0]]["rows"][:k]
        if table:
            relabel_rows(block, table, notes, on[0], None, declared_doc[on[0]])
        sheet.running = [(block, page_box(doc[on[0]]).x1)]
        sheet.running_on = set(on)
        # the first page carries the block too, under its title, where it
        # stays; a sheet that page runs onto opens with the block as well
        said = {" ".join(i["text"].split()) for r in block for i in r if i["what"] == "text"}
        also = {" ".join(i["text"].split()) for r in read[0]["rows"] for i in r
                if i["what"] == "text"}
        if said and len(said & also) >= 0.6 * len(said):
            sheet.running_on.add(0)
        for n in on:
            rows_here = read[n]["rows"]
            box = fitz.Rect()
            for r in rows_here[:k]:
                for i in r:
                    box |= i["rect"]
            # the block's own lines and boxes go with it, not with the rows
            zones[n].append(fitz.Rect(0, box.y0 - 3, doc[n].rect.width, box.y1 + 3))
            read[n]["rows"] = rows_here[k:]
        running_fields = Counter((i["name"], i["kind"]) for r in block for i in r
                                 if i["what"] == "field")
        notes.append(("running_block",
                      f"{k} row(s) the form repeats at the top of pages "
                      f"{', '.join(str(n + 1) for n in on)} - set once at the top of "
                      f"each sheet those pages go onto"))

    # A sentence the form carried over from the foot of one page to the head
    # of the next is one sentence. Each page is read on its own, so it came
    # out broken where the old page ended - "...will be considered to have"
    # and then, a gap later, "been amended to conform". The rebuild sets its
    # own page breaks, so the old one goes.
    def in_foot(row, n):
        return (bool(page_numbers(row, doc[n])) and len(page_numbers(row, doc[n])) == len(row)
                or all(any(fitz.Point((i["rect"].x0 + i["rect"].x1) / 2,
                                      (i["rect"].y0 + i["rect"].y1) / 2) in b + (-2, -2, 2, 2)
                           for b, _ in foot_boxes.get(n, ())) for i in row))
    for n in range(doc.page_count - 1):
        if read[n + 1]["opens"]:
            continue
        ups = [r for r in read[n]["rows"] if not in_foot(r, n)]
        downs = [r for r in read[n + 1]["rows"] if not in_foot(r, n + 1)]
        if not ups or not downs:
            continue
        a_row, b_row = ups[-1], downs[0]
        if not (len(a_row) == 1 == len(b_row)
                and a_row[0]["what"] == "text" == b_row[0]["what"]):
            continue
        a, b = a_row[0], b_row[0]
        head = b["text"].lstrip()[:1]
        hanging = (LIST_MARK.match(a["text"].lstrip())
                   and a["rect"].x0 < b["rect"].x0 <= a["rect"].x0 + 40)
        if (head.isalpha() and head.islower()
                and a["text"].rstrip()[-1:] not in ".:;!?"
                and abs(a["size"] - b["size"]) < 0.6
                and a["bold"] == b["bold"] and a["italic"] == b["italic"]
                and (abs(a["rect"].x0 - b["rect"].x0) <= 2.5 or hanging)
                and not LIST_MARK.match(b["text"].lstrip())):
            a_row[0] = dict(a, text=a["text"].rstrip() + " " + b["text"].lstrip(),
                            runs=join_runs(a, b),
                            rect=fitz.Rect(a["rect"].x0, a["rect"].y0,
                                           max(a["rect"].x1, b["rect"].x1), a["rect"].y1))
            read[n + 1]["rows"] = [r for r in read[n + 1]["rows"] if r is not b_row]
            notes.append(("joined_across_pages",
                          f"pages {n + 1}-{n + 2} of the form: {a['text'].split()[-1]!r} / "
                          f"{b['text'].split()[0]!r} - one sentence the old page break cut"))

    last_base, started, typed = None, False, 0
    for page in doc:
        right_edge = page_box(page).x1
        sheet.source = page.number
        here = read[page.number]
        bars = bars_of[page.number]
        if started and here["opens"]:
            sheet.new_page()
            last_base = None
            # another document starts here, and it starts with its letterhead
            if not sheet.heads:
                set_letterhead(page)
        # whether what follows this page of the form will start a fresh sheet
        # anyway - either because another document begins there, or because
        # there is nothing after it. If so, a line or two left over here has
        # nothing to gain by spilling onto a page of its own.
        closes = (page.number == doc.page_count - 1
                  or read[page.number + 1]["opens"])

        grids = here["grids"]
        for g in grids:
            zones[page.number].append(g["box"] + (-4, -4, 4, 4))
        laid = split_beside_grids(here["rows"], grids)
        # A page of the form that is the last before a fresh sheet - the end
        # of the form, or the page before another document starts - and that
        # would spill a few rows onto a sheet of their own, closes up the
        # space between its rows instead. "The acknowledgement should fit on
        # the first page": four lines were going to a sheet by themselves,
        # ahead of a Security Agreement that starts on its own sheet anyway.
        # Only the gaps give way, and never below 30% of what the form left.
        # The member signing areas on this page of the form, found the way the
        # signing pass will find them once the page is rebuilt, and the room
        # the client's standard block will need there. The rebuild keeps the
        # rows of an area together on one sheet and leaves that room after
        # them; the signing pass then draws the block into it. Without the
        # room the block has nowhere to go on a page the rebuild has filled.
        signing_at = []
        found_here = (R.signing_areas(page)[0]
                      if args.signing and (house or args.no_house) and not R.is_declaration(doc)
                      and R.signing_listed(args.input) else [])
        # Several areas close together on the form - "Owner Signature" rows
        # split by "(if two or more signatures are required)" - are read as
        # one once rebuilt, and room kept for the pieces fits nothing: PAD
        # gained a page for room its block could not use. Those are left for
        # the signing pass to report, with no room kept.
        crowded = any(a is not b and abs(a["box"].y0 - b["box"].y1) < 120
                      for a in found_here for b in found_here)
        if crowded:
            notes.append(("signing_area_left",
                          f"page {page.number + 1} of the form: its signing lines "
                          f"come in pieces with notes between them - no room kept"))
        # and the rows a first run found the block had no room after, read off
        # the rebuilt page by the signing pass itself - whatever the form's own
        # page let this reading see
        hinted = [{"box": fitz.Rect(0, h[1], page.rect.width, h[2]),
                   "pieces": [fitz.Rect(0, h[1], page.rect.width, h[2])],
                   "members": h[3]}
                  for h in json.loads(args.signing_room or "[]") if h[0] == page.number]
        if hinted:
            found_here = [a for a in found_here
                          if not any(a["box"].intersects(h["box"]) for h in hinted)]
        if True:
            for area in ([] if crowded else found_here) + hinted:
                # the rows that ARE the area: ones that touch its own captions
                # or lines, not merely ones near it - the "Quota No." row under
                # an area is its own thing, and splitting it from its captions
                # to make room left its captions 40pt adrift
                pieces = [p + (-2, -2, 2, 2) for p in area["pieces"]]
                idx = [k for k, r in enumerate(laid)
                       if any(fitz.Rect(i["rect"]).intersects(p)
                              for i in r for p in pieces)]
                if idx:
                    # the block is drawn from the area's first line, which sits
                    # a row below where the area's rows begin
                    # and two lines of air under it (see R.standardize_signing):
                    # with less, the staff row sat on the "Quota No. / Address"
                    # row under it on the quota forms
                    need = (sheet.size * (3.4 * (R.member_rows(area) + 1) + 1.2) + 4
                            + sheet.measure(laid[min(idx)], right_edge))
                    has = sum(sheet.measure(laid[k], right_edge)
                              for k in range(min(idx), max(idx) + 1))
                    signing_at.append({"first": min(idx), "last": max(idx),
                                       "need": need, "air": 2.0 * sheet.size,
                                       "extra": max(0.0, need - has),
                                       "start": None, "done": False})

        def close_signing(a):
            """Leave the room the standard block needs after an area's rows."""
            a["done"] = True
            if a["start"] is None:
                return
            page_no, y0 = a["start"]
            if sheet.doc.page_count == page_no and sheet.y - y0 < a["need"]:
                # straight onto the cursor: a gap opened with `gap` can be
                # given back to a later row, and this one is not spare
                sheet.y = y0 + a["need"]
                sheet.spare = 0.0

        squeeze = 1.0
        if closes and len(laid) > 1:
            content = (sum(sheet.measure(r, right_edge) for r in laid)
                       + sum(a["extra"] for a in signing_at)
                       # and the block to be added there, which closing up the
                       # gaps can make room for: Bill Payment Trace went to two
                       # sheets for a block alone on the second
                       + (block_need if adding and page.number == adding_page else 0.0))
            gaps = 0.0
            for a, b in zip(laid, laid[1:]):
                said = [i for i in b if i["what"] == "text"]
                step = (min(i["base"] for i in b) - min(i["base"] for i in a)
                        - (said[0]["size"] if said else 9))
                gaps += min(18.0, max(0.0, step))
            left = sheet.rect.height - sheet.bottom - sheet.y - 6
            if gaps > 0 and content + gaps > left and content + 0.3 * gaps <= left:
                squeeze = max(0.3, (left - content) / gaps)
                notes.append(("gaps_closed_up",
                              f"page {page.number + 1} of the form set with its row "
                              f"gaps at {squeeze:.0%} so it ends on its own sheet"))
        if args.merge_fields:
            merge_tags_to_fields(laid, TAKEN, notes, ADDED)
        if table:
            relabel_rows(laid, table, notes, page.number, before, declared_doc[page.number])
        # every line the form typed for someone to write on, so a rule that
        # goes missing is counted rather than silently lost: the word census
        # cannot see one, because a rule has no words in it
        typed += sum(1 for row in laid for i in row if i["what"] == "rule")
        n = 0
        while n < len(laid):
            row = laid[n]
            for a in signing_at:
                if a["start"] is None and not a["done"] and n >= a["first"]:
                    sheet.room(a["need"] + 8, clear=False)
                    # the air under the block only where the sheet has it:
                    # asked for regardless, Egg's signing area went to a new page
                    if sheet.y + a["need"] + a["air"] + 8 <= sheet.rect.height - sheet.bottom:
                        a["need"] += a["air"]
                    a["start"] = (sheet.doc.page_count, sheet.y)
                if a["start"] is not None and not a["done"] and n > a["last"]:
                    close_signing(a)
            texts = [i for i in row if i["what"] == "text"]
            has_field = any(i["what"] == "field" for i in row)
            # (a title is at the head of the page: Wire Instructions sets its
            # whole body at 12pt, and every row of it went as "the title")
            # (and only where a header carries it: the pages behind an issuer's
            # own get none, and Transfer In's wire page lost its title)
            if (house and not started and texts and texts[0]["size"] >= 12 and not has_field
                    and all(i["rect"].y1 <= page.rect.height * 0.16 for i in row)):
                n += 1
                continue                    # the form's own title; the header carries it
            feet = page_numbers(row, page)
            if feet:
                dropped_feet += [i["text"].strip() for i in feet]
                row = [i for i in row if i not in feet]
                laid[n] = row
                if not row:
                    n += 1
                    continue
                texts = [i for i in row if i["what"] == "text"]
                has_field = any(i["what"] == "field" for i in row)
            # The form's footer - its code and revision date - belongs at the
            # foot of every sheet, not in the flow: set as a row it came out
            # halfway down a page ("EST 107 (06/15)" between two witness
            # blocks). It is taken out here and set at the foot afterwards.
            boxes = foot_boxes.get(page.number, ())
            footing = [i for i in row if i["what"] == "text" and any(
                fitz.Point((i["rect"].x0 + i["rect"].x1) / 2, (i["rect"].y0 + i["rect"].y1) / 2)
                in b + (-2, -2, 2, 2) for b, _ in boxes)]
            if footing:
                for i in footing:
                    size = next(sz for b, sz in boxes if fitz.Point(
                        (i["rect"].x0 + i["rect"].x1) / 2,
                        (i["rect"].y0 + i["rect"].y1) / 2) in b + (-2, -2, 2, 2))
                    here = footers.setdefault(page.number, [])
                    if not any(t == i["text"] for t, _, _, _, _ in here):
                        here.append((i["text"], i["rect"].x0, size,
                                     (i["bold"], i["italic"]), i))
                row = [i for i in row if i not in footing]
                laid[n] = row
                if not row:
                    n += 1
                    continue
                texts = [i for i in row if i["what"] == "text"]
                has_field = any(i["what"] == "field" for i in row)
            if not started:
                # the rule and any mark above the first row belonged to the
                # title block, which the header has replaced
                cut = min(i["rect"].y0 for i in row) - 2
                zones[page.number].append(fitz.Rect(0, 0, page.rect.width, cut))
            started = True
            base = min(i["base"] for i in row)
            # the block a form that asks for no signature gets goes before its
            # terms or its office-use part - which can be a table's first row,
            # so this comes before tables are set (ABM POS Trace's admin box)
            if (adding and adding_spot is None and texts
                    and R.TERMS_HEAD.search(texts[0]["text"]) and len(texts[0]["text"]) < 60):
                sheet.room(block_need + 8, clear=False)
                adding_spot = (sheet.doc.page_count - 1, sheet.y + sheet.size)
                sheet.y += block_need
                sheet.spare = 0.0
            # everything still to come on the last page of the form: if it is
            # only a line or two, it closes up and leans into the bottom
            # margin rather than opening a sheet of its own
            tail = (sum(sheet.measure(laid[k], right_edge)
                        for k in range(n, len(laid)))
                    if closes and len(laid) - n <= 4 else 10 ** 6)
            if last_base is not None:
                # keep the space the form left between rows, in proportion
                gap = max(0.0, base - last_base) - (texts[0]["size"] if texts else 9)
                want = min(18.0, max(0.0, gap)) * squeeze
                # close up only where the gap would push the rest onto a sheet
                # of its own: Turkey's last signing row was squashed against
                # the one above with half the page still empty below it
                left = sheet.rect.height - sheet.bottom - sheet.y
                # (counting the gaps of every row still to come, not just this one)
                sheet.gap(0.0 if tail <= 40 and tail + want * (len(laid) - n) > left else want)
            last_base = base

            for i in row:
                if i.get("drawn"):
                    zones[page.number].append(i["rect"] + (-3, -2, 3, 6))

            # In a table is inside its top border, measured by the middle of
            # the row's words: a signature row set right on the line that
            # opens a table has its baseline a point above that line, and
            # taken by the baseline it was pulled into the table's first
            # cell - "Authorized Signature:" on top of "F - For Use by
            # Relinquishing Institution Only".
            def middle(r):
                ys = ([(i["rect"].y0 + i["rect"].y1) / 2 for i in r if i["what"] == "text"]
                      or [(i["rect"].y0 + i["rect"].y1) / 2 for i in r])
                return sum(ys) / len(ys)

            here = next((g for g in grids
                         if g["box"].y0 - 2 <= min(i["base"] for i in row) <= g["box"].y1 + 2
                         and middle(row) >= g["box"].y0 - 0.5
                         and g["box"].x0 - 6 <= min(i["rect"].x0 for i in row)
                         <= g["box"].x1 + 6), None)
            if here is not None:
                k, inside = n, []
                while k < len(laid):
                    y = min(i["base"] for i in laid[k])
                    if not (here["box"].y0 - 2 <= y <= here["box"].y1 + 2
                            and middle(laid[k]) >= here["box"].y0 - 0.5
                            # (and inside it sideways: what stood beside the
                            #  grid was set after it - see split_beside_grids)
                            and here["box"].x0 - 6 <= min(i["rect"].x0 for i in laid[k])
                            <= here["box"].x1 + 6):
                        break
                    inside += laid[k]
                    k += 1
                # A shaded panel with no uprights inside its border - ABM's
                # "ADMIN OFFICE USE ONLY" box, labels with a line to write on
                # after each - is not a table. Read band by band it came back
                # as one, label cells over empty cells. Its rows are set as
                # rows, kept together, in a box of its own fill and border.
                gb = here["box"]
                # (drawn uprights only - dark strips or strokes; the edges of
                #  the grey fills between label and blank are not lines)
                def dark_(d):
                    c = d.get("fill") if d["type"] == "f" else d.get("color")
                    return c is not None and max(c) < 0.5
                inner_ups = [d for d in R.printed(page).get_drawings()
                             if dark_(d) and d["rect"].width <= 3 and d["rect"].height >= 8
                             and gb.x0 + 6 < d["rect"].x0 < gb.x1 - 6
                             and d["rect"].y0 < gb.y1 - 2 and d["rect"].y1 > gb.y0 + 2]
                # (the box's own border is not a line to write on: Federal
                #  RLIF's top edge, broken by its heading, came back as one)
                panel_rows = [[i for i in r if not (i["what"] == "rule" and (
                    abs(i["rect"].y1 - gb.y0) <= 4 or abs(i["rect"].y1 - gb.y1) <= 4))]
                              for r in laid[n:k]]
                typed -= sum(len(r) for r in laid[n:k]) - sum(len(r) for r in panel_rows)
                panel_rows = [r for r in panel_rows if r]
                if (not inner_ups and panel_rows
                        and all(len(r) <= 3 and all(i.get("kind") not in ("CheckBox", "RadioButton")
                                                    for i in r) for r in panel_rows)):
                    # (shaded as the rest of the rebuild shades: a fill 95% white
                    #  or lighter - ABM's - counts as white, as tables' do)
                    shade_ = Counter(tuple(round(c, 2) for c in f) for r_, f in fills_of[page.number]
                                     if gb.contains(r_)).most_common(1)
                    need_ = sum(max(sheet.measure(r, right_edge) + sheet.size * 0.3, sheet.size * 2.8)
                                for r in panel_rows) + sheet.size * 2
                    sheet.room(need_ + 8, clear=False)
                    page_no_, top_ = sheet.doc.page_count, sheet.y + sheet.size * 0.4
                    sheet.y += sheet.size * 0.6
                    for j_, r in enumerate(panel_rows):
                        y_was = sheet.y
                        sheet.place(r, min(right_edge, gb.x1 - 4))
                        # rows as far apart as the form set them
                        if j_ + 1 < len(panel_rows):
                            step_ = (min(i["base"] for i in panel_rows[j_ + 1])
                                     - min(i["base"] for i in r))
                            sheet.y = max(sheet.y + sheet.size * 0.3,
                                          y_was + min(step_, sheet.size * 3.2))
                    if sheet.doc.page_count == page_no_:
                        box_ = fitz.Rect(gb.x0, top_, gb.x1, sheet.y + sheet.size * 0.2)
                        if shade_:
                            sheet.page.draw_rect(box_, color=None, fill=shade_[0][0],
                                                 width=0, overlay=False)
                        sheet.page.draw_rect(box_, color=(0, 0, 0), width=1.6)
                        sheet.y = box_.y1 + sheet.size * 0.4
                    notes.append(("panel_set_as_rows",
                                  f"page {page.number + 1} of the form: a shaded box of "
                                  f"{len(panel_rows)} row(s) with no columns, set as rows"))
                    last_base = gb.y1
                    n = k
                    continue
                # every item belongs to exactly one band, or a field that
                # sits on a band edge is built twice under the same name
                bands = [(top, foot, cuts, []) for top, foot, cuts in here["bands"]]
                for i in inside:
                    # the band the item stands in; only an item that stands
                    # in none - a hair over an edge - goes to the nearest. By
                    # nearest middle alone, section E's signature row, set at
                    # the foot of its tall band, went to the short band of
                    # section F below it
                    holds = [k for k in range(len(bands))
                             if bands[k][0] <= i["base"] <= bands[k][1]]
                    pick = (holds[0] if len(holds) == 1 else
                            min(range(len(bands)),
                                key=lambda k: abs((bands[k][0] + bands[k][1]) / 2 - i["base"])
                                if bands[k][0] - 4 <= i["base"] <= bands[k][1] + 4
                                else 10 ** 6))
                    lo, hi, _, _ = bands[pick]
                    if lo - 4 <= i["base"] <= hi + 4:
                        bands[pick][3].append(i)
                sheet.grid(here, bands, right_edge, bars=bars,
                           fills=fills_of[page.number])
                last_base = here["box"].y1
                n = k
                continue

            grid = [row]
            if is_table_row(row):
                k = n + 1
                while k < len(laid) and is_table_row(laid[k]):
                    grid.append(laid[k])
                    k += 1
            if len(grid) > 1:
                # a table is rows, plural. One line of labels with a blank
                # after each is a line of the form, not a grid, and boxing it
                # up puts a border round something that never had one
                box = fitz.Rect(grid[0][0]["rect"])
                for r in grid:
                    for i in r:
                        box |= i["rect"]
                zones[page.number].append(fitz.Rect(box.x0 - 8, box.y0 - 8,
                                                    page.rect.width, box.y1 + 8))
                sheet.room(sheet.size * 2.6 + 4, clear=False)
                # a small table goes on whole: ABM's admin box left its last
                # row, "Response:", alone at the top of the next sheet
                whole_ = sum(sheet.measure(r, right_edge) * 1.25 + 6 for r in grid)
                if whole_ <= 0.4 * (sheet.rect.height - sheet.top - sheet.bottom):
                    sheet.room(whole_ + 8, clear=False)
                sheet.table(grid, right_edge)
                last_base = max(i["base"] for i in grid[-1])
                n = k
                continue

            under = laid[n + 1] if n + 1 < len(laid) else None
            if (is_rule_row(row) and under is not None
                    and under and all(i["what"] == "text" for i in under)
                    and all(len(i["text"]) < 90 for i in under)
                    and len(under) <= slot_count(row)):
                sheet.place_rules(row, under, right_edge)
                n += 1
                continue

            bar = next((b for b in bars
                        if texts and b.y0 - 3 <= texts[0]["rect"].y0
                        and texts[0]["rect"].y1 <= b.y1 + 3), None)
            # A heading goes on the page its section starts on, not at the
            # foot of the one before. A bar makes one; so does a short bold
            # line with nothing to fill in - "6. INSURANCE" is a heading
            # whether or not the form shaded it.
            if is_heading(row, bars):
                # How much the whole section needs: this heading and every
                # row after it until the next heading. If all of it would fit
                # on a page of its own but not in what is left of this one,
                # the section moves rather than being cut in half - which is
                # how a section came to leave one tick box behind and carry
                # the rest over.
                usable = sheet.rect.height - sheet.top - sheet.bottom
                def est(r):
                    # a row and the gap after it; a table's rows are padded
                    # top and bottom, which a plain row's measure leaves out
                    # and a section that "fitted" left its last row behind
                    h = sheet.measure(r, right_edge)
                    y = min(i["base"] for i in r)
                    if any(g["box"].y0 - 2 <= y <= g["box"].y1 + 2 for g in grids):
                        h = h * 1.25 + 6.0
                    return h
                def gap_before(k):
                    # the gap the layout will open above row k: the form's
                    # own, capped and closed up as the main loop does it
                    prev = [i for i in laid[k - 1] if i["what"] == "text"]
                    said = [i for i in laid[k] if i["what"] == "text"]
                    step = (min(i["base"] for i in laid[k]) - min(i["base"] for i in laid[k - 1])
                            - (said[0]["size"] if said else 9))
                    return min(18.0, max(0.0, step)) * squeeze
                whole, k = est(row), n + 1
                bare = whole
                while k < len(laid) and not is_heading(laid[k], bars, strong_only=True):
                    whole += est(laid[k]) + gap_before(k)
                    bare += est(laid[k])
                    if whole > usable:
                        break
                    k += 1
                # The last of a form's page before a fresh sheet closes up its
                # gaps and may lean into the bottom margin (see `tail`), so a
                # section that ends it is judged the same way - measured with
                # its gaps, Egg's ACKNOWLEDGEMENT moved to a sheet of its own.
                tail_fits = (k >= len(laid) and closes
                             and bare <= sheet.rect.height - sheet.bottom / 2 - sheet.y)
                # A long section - most of a sheet - moves to keep it whole only
                # from the lower part of a sheet: from higher up, moving it
                # leaves most of the sheet empty - Federal RLIF's first sheet
                # held only its title once the preamble under it moved on. A
                # shorter one always moves (Est115's "This declaration ...").
                left_here = sheet.rect.height - sheet.bottom - sheet.y
                if tail_fits:
                    pass
                elif whole <= usable and (whole <= 0.6 * usable or left_here < 0.45 * usable):
                    sheet.room(whole + 8, clear=False)
                else:
                    follows = sum(sheet.measure(laid[j], right_edge)
                                  for j in range(n + 1, min(n + 3, len(laid))))
                    sheet.room(min(sheet.rect.height / 3,
                                   sheet.measure(row, right_edge) + follows + 8), clear=False)
            if bar is not None:
                sheet.shade(row, right_edge, was=bar)
            sheet.place(row, right_edge, last=tail <= 40)
            n += 1
        for a in signing_at:              # an area that ran to the foot of the page
            if not a["done"]:
                close_signing(a)
        last_base = None                    # a new page starts its own run
    if adding and adding_spot is None:
        sheet.room(block_need + 8, clear=False)
        adding_spot = (sheet.doc.page_count - 1, sheet.y + sheet.size)
        sheet.y += block_need
    # Each sheet carries the footer of the form page its rows came from, at
    # the foot, in the fine-print size - as the form printed it on every page.
    if footers:
        last = None
        for k, pg in enumerate(out):
            src = sheet.origin.get(k, last)
            last = src
            lines = footers.get(src, [])
            y = pg.rect.height - sheet.bottom / 2 - 2 - 9 * (len(lines) - 1)
            for text, x0, size, (bold, italic), item in lines:
                face = sheet.face(bold, italic)
                at = min(size, 8.0)
                width = face[2].text_length(text, at)
                x = (max(36.0, x0) if x0 < pg.rect.width / 2
                     else pg.rect.width - 36.0 - width)
                # each word in its own weight: "EST120 (01/26)" is bold only
                # as far as the form number
                was, sheet.page = sheet.page, pg
                sheet.__dict__.get("_run_at", {}).pop(id(item), None)
                sheet.put(item, x, y, text, face, size=at)
                sheet.page = was
                sheet.said.update(re.findall(r"[^\s_]+", text))
                y += 9
        notes.append(("footer_on_every_sheet",
                      "; ".join(dict.fromkeys(t for v in footers.values() for t, _, _, _, _ in v))
                      + " - set at the foot of each sheet instead of in the flow"))
    # bring the form's own rules and boxes across, each following its row
    art = sum(sheet.carry_art(page, zones[page.number]) for page in doc)
    # Arial shares a glyph between the hyphen and the soft hyphen, and between
    # the space and the no-break space; without this every hyphen and space in
    # the rebuild copies and searches as the wrong character
    # The member signing area, brought to the client's standard - the same
    # pass the restyle runs, on the pages as they were rebuilt. The words it
    # takes out and puts in are handed to the word check, so a caption that
    # went missing any other way still shows up.
    signing_issues, signing_boxes = [], {}
    if args.signing and R.signing_listed(args.input):
        declared = R.is_declaration(doc)
        if declared:
            notes.append(("signing_area_left", "a declaration - its signing and "
                                               "witnessing are left as the form has them"))
        for pg in out:
            done, said_notes = R.standardize_signing(pg, fonts, sheet.size,
                                                     # Libro's own pages behind an issuer's
                                                     # (Milk) are Libro's to sign, header or not
                                                     skip=(not house and not args.no_house)
                                                     or declared)
            for rec in done:
                SIGNING_DROPPED.extend(rec.get("dropped", []))
                signing_boxes.setdefault(pg.number, []).append(
                    R.signing_extent(rec, pg.rect.width) + (0, -6, 0, 6))
                for t in rec["was"]:
                    sheet.said.subtract(re.findall(r"[^\s_]+", t))
                for t in rec["made_text"]:
                    sheet.said.update(re.findall(r"[^\s_]+", t))
            for kind, detail in said_notes:
                if kind.isupper():
                    signing_issues.append((kind, f"page {pg.number + 1}: {detail}"))
                else:
                    notes.append((kind, f"page {pg.number + 1}: {detail}"))
        if adding_spot is not None:
            pg = out[adding_spot[0]]
            y_ = adding_spot[1]
            # (set from the text's own left edge, not the sheet's margin)
            lefts_ = [w[0] for w in pg.get_text("words") if w[0] >= 30]
            x0_ = min(lefts_) if lefts_ else 36.0
            area_ = {"box": fitz.Rect(x0_, y_, pg.rect.width - 36, y_ + 1),
                     "members": 0, "rules": [], "pieces": [], "text": [], "frag_text": []}
            rec_, why_ = R.standard_signing(pg, area_, fonts, sheet.size)
            if rec_ is None:
                signing_issues.append(("SIGNING_AREA_NO_ROOM",
                                       f"page {pg.number + 1}: a block to add, the form asking "
                                       f"for no signature: it {why_}"))
            else:
                signing_boxes.setdefault(pg.number, []).append(
                    R.signing_extent(rec_, pg.rect.width) + (0, -6, 0, 6))
                sheet.said.update(w for t in rec_["made_text"] for w in re.findall(r"[^\s_]+", t))
                notes.append(("signing_area_added",
                              f"page {pg.number + 1}: the form asks for no signature; the "
                              f"standard block was added at y={y_:.0f}"))
        sheet.said = +sheet.said        # drop the words taken out entirely
    # An area the block had no room in: map it back to the rows of the form
    # it was laid out from, and lay the form out once more with the room kept
    # there. The reading of the form's own page can miss an area the rebuilt
    # page shows plainly - a single "Holder's Signature" line, captions the
    # form set in pieces - so the signing pass's own reading decides.
    if R.NO_ROOM_AREAS and args.signing_room is None:
        hints = {}
        for pno, box, members in R.NO_ROOM_AREAS:
            for orig_y, new_y, page_no, src in sheet.moved:
                if page_no == pno and box.y0 - 14 <= new_y <= box.y1 + 2:
                    h = hints.setdefault((src, round(box.y0)), [src, orig_y, orig_y, members])
                    h[1], h[2] = min(h[1], orig_y), max(h[2], orig_y)
        if hints:
            import subprocess
            retry = [h[:1] + [h[1] - 1, h[2] + 1] + h[3:] for h in hints.values()]
            say(f"signing block had no room in {len(retry)} area(s); laying the form "
                f"out again with room kept there")
            # (the child writes UTF-8 whatever the console's code page: on
            # Windows it wrote cp1252, RLIF's footer "© Concentra" failed to
            # decode, and the whole rebuild died with nothing written)
            r = subprocess.run([sys.executable, os.path.abspath(__file__)] + sys.argv[1:]
                               + ["--signing-room", json.dumps(retry)],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace",
                               env=dict(os.environ, PYTHONIOENCODING="utf-8"))
            for out, text in ((sys.stdout, r.stdout), (sys.stderr, r.stderr)):
                if text:
                    enc = out.encoding or "utf-8"
                    out.write(text.encode(enc, "replace").decode(enc))
            return r.returncode
    bad = R.fix_tounicode(out, fonts)
    # PyMuPDF marks a file "append only" (SigFlags 3) when it adds a
    # signature field, as if one had been signed. Acrobat then treats the
    # whole form as signed and won't let anyone edit it - and nothing here
    # is signed: the form's own empty signature boxes were only put back.
    # The file gets the flags the form had.
    sig_was = doc.xref_get_key(doc.pdf_catalog(), "AcroForm/SigFlags")
    if out.xref_get_key(out.pdf_catalog(), "AcroForm")[0] != "null":
        if sig_was[0] == "null":
            out.xref_set_key(out.pdf_catalog(), "AcroForm/SigFlags", "null")
        else:
            out.xref_set_key(out.pdf_catalog(), "AcroForm/SigFlags", sig_was[1])
    # every field the pages carry is on the form's field list (see
    # R.list_all_fields - a page copy once emptied the list)
    for detail in R.radios_to_checkboxes(out):
        notes.append(("radio_to_checkbox", detail))
    unlisted = R.list_all_fields(out)
    if unlisted:
        signing_issues.append(("FIELDS_UNLISTED", f"{unlisted} field(s) were missing from the "
                                                  f"form's field list - put back"))
    out.save(args.output)
    # the calculation list is only settled when the file is written, so the
    # form's own order goes back into the saved file
    with fitz.open(args.output) as saved:
        if keep_calc_order(calc_order, saved):
            saved.saveIncr()
    was = sum(1 for page in doc for _ in page.widgets())
    print(f"line art carried over: {art} shape(s)")
    print(f"rebuilt onto {out.page_count} page(s): {args.output}")
    print(f"fields: {was} in the original, {len(sheet.fields)} in the rebuild")
    for issue, detail in bad:
        say(f"{issue}: {detail}")
    for name in sheet.swapped:
        say(f"RADIO_AS_CHECKBOX: {name} - the group no longer picks one only")
    for detail in sheet.dropped:
        say(f"ART_LEFT_OUT: {detail}")
    for kind, detail in notes:
        say(f"{kind.upper()}: {detail}")
    if sheet.drew["rule"] != typed:
        say(f"BLANK_LOST: the form types {typed} rule(s) with no field behind "
            f"them; {sheet.drew['rule']} came through")
    if dropped_feet:
        say("PAGE_NUMBER_DROPPED: the form's own page numbers "
            f"({', '.join(dict.fromkeys(dropped_feet))}) no longer match, "
            "because the rebuild re-paginates")
    for note in dict.fromkeys(SPELT):
        say(f"LIGATURE_SPELT_OUT: {note}")
    if MARKS:
        kinds = Counter(f"U+{ord(a):04X} -> {b}" for a, b in MARKS)
        say("SYMBOL_REDRAWN: " + ", ".join(f"{k} x{n}" for k, n in kinds.items()))
    if HIDDEN:
        say(f"left out, drawn in white on white by the form: {HIDDEN}")
    if UNDER_LOGO or LOGO_DROPPED:
        say(f"LOGO_PLACEHOLDER_DROPPED: the template's logo field and what it covered - "
            f"fields {[n for n, _ in LOGO_DROPPED]}, text {UNDER_LOGO} - the header sets "
            f"Libro's logo instead")
    for kind, detail in NOTES_OFF:
        say(f"{kind.upper()}: {detail}")
    was = R.fields_expected(doc)    # radio groups as the check boxes they became
    was.update(ADDED)          # text boxes put in place of merge tags, on purpose
    was.subtract(Counter(SIGNING_DROPPED))   # signers past the second, on purpose
    was.subtract(Counter(LOGO_DROPPED))      # a template's logo field, on purpose
    was = +was
    if running:
        # the form's copies of a running block are gone, and one is set on
        # each sheet instead - same names, so a value typed once fills all
        for key, n in running_fields.items():
            was[key] += n * (sheet.running_placed - len(running[1]))
        say(f"running block set on {sheet.running_placed} sheet(s); "
                          f"the form had it on {len(running[1])} page(s)")
    wrote = fitz.Rect()
    for page in doc:
        wrote |= page_box(page)
    found = verify(fitz.open(args.output), sheet.said, was,
                   fitz.Rect(wrote.x0 - 1, 20.0, wrote.x1 + 6, 0))
    found += signing_issues
    # The check above holds the file to what the rebuild drew - and a row the
    # rebuild never drew is in neither. Transfer In's wire page came back as a
    # logo and a signing block, "checks: clean". So each page of the FORM is
    # held to the file too: most of its words have to be in it somewhere.
    # (most, not all - its title, page numbers and relabelled words go by design)
    word = lambda t: {w.casefold() for w in re.findall(r"[A-Za-z]{3,}", t)}
    with fitz.open(args.output) as saved:
        have = set().union(*(word(p.get_text()) for p in saved))
    for page in doc:
        own = word(R.printed(page).get_text())
        if len(own) >= 20 and len(own & have) < 0.6 * len(own):
            found.append(("TEXT_NOT_CARRIED",
                          f"page {page.number + 1} of the form: only {len(own & have)} of its "
                          f"{len(own)} words are anywhere in the rebuild - e.g. "
                          + ", ".join(sorted(own - have)[:12])))
    found += R.verify_field_behaviour(doc, fitz.open(args.output))
    # Does it still look like the form? The rebuild lays every line out again,
    # and lost run-in bold headings, italic notes, shaded table heads and
    # centring on the way - none of which any check above can see. The
    # header and the standard signing areas are new by design and left out.
    with fitz.open(args.output) as saved:
        after_skip = {0: [head_band]}
        for pno, boxes in signing_boxes.items():
            after_skip.setdefault(pno, []).extend(boxes)
        found += [(issue, f"page {page}: {detail}" if page else detail)
                  for page, issue, detail in looks.compare(doc, saved, None, after_skip)]
    # Every image the form carried is placed one way or another, or said to be
    # lost. Nothing drops out without a word - the rebuild once shipped nine
    # forms missing a logo and no check noticed.
    had = sum(len(p.get_image_info()) for p in doc)
    if had or sheet.placed:
        say("images: " + (", ".join(f"{n} {how}" for how, n in sheet.image_log.items())
                          or "none of the form's") + f" ({had} in the form)")
    for what in sheet.lost_images:
        found.append(("IMAGE_LOST", what))
    accounted = sum(sheet.image_log.values()) + len(sheet.lost_images)
    if accounted < had:
        found.append(("IMAGE_LOST", f"{had - accounted} of the form's {had} image(s) "
                                    f"were never placed"))
    shown = sum(len(p.get_image_info()) for p in fitz.open(args.output))
    if shown < sheet.placed:
        found.append(("IMAGE_LOST", f"{sheet.placed - shown} image(s) set on the "
                                    f"rebuilt pages are not in the saved file"))
    with fitz.open(args.output) as saved:
        sig_now = saved.xref_get_key(saved.pdf_catalog(), "AcroForm/SigFlags")
    # (no flags at all and flags of 0 are the same file: unsigned, editable -
    # a rebuild with no fields has no AcroForm to carry the form's 0)
    flags = lambda v: int(v[1]) if v[0] == "int" else 0
    if flags(sig_now) != flags(sig_was):
        found.append(("FILE_LOCKED", f"signature flags {sig_now[1] or 'none'}, the form had "
                                     f"{sig_was[1] or 'none'} - an 'append only' file is "
                                     f"read as signed and can't be edited"))
    for issue, detail in found:
        say(f"{issue}: {detail}")
    if args.png:
        made = proofs(doc, args, sheet, before)
        print(f"proofs: {made} page(s) in "
              + ("one file" if args.proof_scroll else f"{made} image(s)")
              + f", in {args.png}")
    print("checks: clean" if not found else f"checks: {len(found)} problem(s)")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
