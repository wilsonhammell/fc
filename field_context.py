#!/usr/bin/env python3
"""What each place a value goes on a form is, read off the page around it.

    python field_context.py "final selection at 10pt/RRSP Application.pdf" [-o "field naming"]

The first half of naming a form's fields from the DB list. Every field the
form already has, and every blank it prints with no field on it - a ruled
line, a run of typed underscores, a tick box - is given an id and the words
around it: the label in front of it, the caption under it, the words over it,
and the heading of the section it sits in. That is what a person reads to know
what goes there, and the same "Name" means a different value under "Spouse"
than under "Applicant".

Writes, per form, under the output folder:

    <form>/context.json      every item, its rect and its words
    <form>/context pN.png    each page with the items boxed and numbered

Nothing here names anything. The mapping is written from these by hand and
applied by apply_fields.py.
"""
import argparse
import json
import os
import re

import fitz

import restyle_pdf as R

HERE = os.path.dirname(os.path.abspath(__file__))

# a tick box typed as a character rather than drawn or made a field
TICK_CHARS = "☐□❑❒■◻▢"


def lines_of(page):
    """The page's text as lines: (rect, text, bold, size), in reading order."""
    out = []
    for b in page.get_text("dict")["blocks"]:
        if b.get("type"):
            continue
        for line in b["lines"]:
            spans = [s for s in line["spans"] if s["text"].strip()]
            if not spans:
                continue
            text = " ".join(" ".join(s["text"].split()) for s in spans)
            bold = all(s["flags"] & 16 or "Bold" in s["font"] for s in spans)
            size = max(s["size"] for s in spans)
            out.append((fitz.Rect(line["bbox"]), text, bold, size))
    return sorted(out, key=lambda l: (round(l[0].y0), l[0].x0))


def phrase(words, rect, side):
    """The run of words beside a rect on its line, read outwards from it until
    a gap wider than a tab: the label in front of a blank, or the words after a
    tick box."""
    mid = (rect.y0 + rect.y1) / 2
    row = [w for w in words if abs((w[1] + w[3]) / 2 - mid) <= 5]
    if side == "left":
        row = sorted((w for w in row if w[2] <= rect.x0 + 2), key=lambda w: -w[2])
        edge, got = rect.x0, []
        for w in row:
            # the first gap can be any width - a column of labels set well
            # clear of its boxes ("PAN number used ...:" 70pt off) is still
            # their label - and after that a tab ends the phrase
            if (got and edge - w[2] > 18) or len(got) >= 10:
                break
            got.insert(0, w[4])
            edge = w[0]
    else:
        row = sorted((w for w in row if w[0] >= rect.x1 - 2), key=lambda w: w[0])
        edge, got = rect.x1, []
        for w in row:
            if w[0] - edge > 18 or len(got) >= 10:
                break
            got.append(w[4])
            edge = w[2]
    return " ".join(got)


def band(words, rect, above):
    """The words just over or just under a rect, across its width: a caption
    printed under a line, or a column head over a box."""
    if above:
        hit = [w for w in words if rect.y0 - 13 <= w[3] <= rect.y0 + 1
               and w[0] < rect.x1 and w[2] > rect.x0]
    else:
        hit = [w for w in words if rect.y1 - 1 <= w[1] <= rect.y1 + 11
               and w[0] < rect.x1 and w[2] > rect.x0]
    return " ".join(w[4] for w in sorted(hit, key=lambda w: (round(w[1]), w[0])))


def heading(lines, rect):
    """The section an item sits in: the nearest bold or larger line above it.
    A heading set beside the item on its own row ("Spouse:" in front of a
    name line) is the label, not the section, so only lines wholly above
    count."""
    for r, text, bold, size in reversed(lines):
        if r.y1 <= rect.y0 - 1 and (bold or size >= 11) and len(text) > 2:
            return text
    return ""


def blanks(page, shown, taken):
    """The places the form prints for a value with no field on them yet."""
    found = []
    width = page.rect.width

    # a blank is taken when a field sits ON it - a box over the line - not
    # when one is merely near: Ln Renewal prints the member's name in fields
    # just under each signature line, and the lines read as taken
    def covered(r):
        return any((r & (t + (-2, -2, 2, 2))).get_area() > 0.3 * max(r.get_area(), 1)
                   for t in taken)

    words = shown.get_text("words")
    uprights = R.upright_edges(shown)
    for y, x0, x1, _w, _c in R.flat_rules(shown):
        # a rule across the whole page is a section's border, not a blank
        if x1 - x0 < 24 or x1 - x0 > width * 0.8:
            continue
        # nor is one under words: that is the words underlined ("5 business
        # days" on Ln Renewal), and a blank has nothing written on it yet
        on = sum(max(0.0, min(w[2], x1) - max(w[0], x0)) for w in words
                 if y - 14 <= w[3] <= y + 2)
        if on > 0.5 * (x1 - x0):
            continue
        # nor a table's own lines: a rule that meets an upright at both ends
        # is the edge of a cell
        ends = [vx for vx, vy0, vy1 in uprights if vy0 - 2 <= y <= vy1 + 2]
        if (any(abs(vx - x0) <= 3 for vx in ends)
                and any(abs(vx - x1) <= 3 for vx in ends)):
            continue
        r = fitz.Rect(x0, y - 13, x1, y)
        if not covered(r):
            found.append(("line", r))
    for w in shown.get_text("words"):
        for m in re.finditer(r"_{4,}", w[4]):
            # where in the word the run sits, by its share of the characters
            n = max(1, len(w[4]))
            a = w[0] + (w[2] - w[0]) * m.start() / n
            b = w[0] + (w[2] - w[0]) * m.end() / n
            r = fitz.Rect(a, w[1], b, w[3])
            if not covered(r):
                found.append(("typed", r))
        if w[4] and w[4][0] in TICK_CHARS:
            r = fitz.Rect(w[0], w[1], w[0] + (w[3] - w[1]) * 0.9, w[3])
            if not covered(r):
                found.append(("tick", r))
    # small drawn squares are tick boxes too
    for d in shown.get_drawings():
        r = fitz.Rect(d["rect"])
        if 5 <= r.width <= 15 and abs(r.width - r.height) <= 1.5 and not d.get("fill"):
            if not covered(r):
                found.append(("tick", r))
    # one blank per place: a line under typed underscores is the same blank
    out = []
    for kind, r in found:
        if not any(abs(r.y1 - o.y1) < 4 and r.x0 < o.x1 and o.x0 < r.x1 for _, o in out):
            out.append((kind, r))
    return out


# Fields and blanks are numbered apart, F1.. and B1.., each in page order,
# so a change in how blanks are found never renumbers the fields a mapping
# already names.
def read_form(path):
    doc = fitz.open(path)
    items = []
    for page in doc:
        shown = R.printed(page)
        words = shown.get_text("words")
        lines = lines_of(shown)
        widgets = list(page.widgets())
        taken = [fitz.Rect(w.rect) for w in widgets]

        def context(r):
            return {"left": phrase(words, r, "left"), "right": phrase(words, r, "right"),
                    "above": band(words, r, True), "below": band(words, r, False),
                    "section": heading(lines, r)}

        for w in widgets:
            r = fitz.Rect(w.rect)
            items.append({"id": f"F{sum(1 for i in items if i['id'][0] == 'F') + 1}",
                          "page": page.number,
                          "existing": w.field_name, "type": w.field_type_string,
                          "hidden": bool(w._annot.flags & 2),
                          "rect": [round(v, 1) for v in r], **context(r)})
        found = blanks(page, shown, taken)
        # A label in a column of labels whose neighbours all have a box at the
        # same place, and it has none and no line either: ATM Trace's POS
        # "Tracer number for the transaction:" was simply left without one.
        # Offered where the box above or below it sits, at its own height.
        boxes = taken + [r for _, r in found]
        for r, text, bold, size in lines:
            if bold or size >= 11 or len(text) > 80:
                continue
            mid = (r.y0 + r.y1) / 2
            if any(abs((b.y0 + b.y1) / 2 - mid) < 6 and b.x0 > r.x0 for b in boxes):
                continue
            if any(abs((w[1] + w[3]) / 2 - mid) < 4 and w[0] > r.x1 + 6 for w in words):
                continue
            near = [b for b in boxes if 6 <= abs((b.y0 + b.y1) / 2 - mid) <= 32
                    and b.x0 > r.x1 and b.width > 40
                    and any(abs(l[0].x0 - r.x0) <= 3
                            and abs((l[0].y0 + l[0].y1) / 2 - (b.y0 + b.y1) / 2) < 6
                            for l in lines)]
            if near:
                b = min(near, key=lambda b: abs((b.y0 + b.y1) / 2 - mid))
                h = b.height
                found.append(("gap", fitz.Rect(b.x0, mid - h / 2, b.x1, mid + h / 2)))
        for kind, r in found:
            items.append({"id": f"B{sum(1 for i in items if i['id'][0] == 'B') + 1}",
                          "page": page.number,
                          "existing": None, "type": kind,
                          "rect": [round(v, 1) for v in r], **context(r)})
    return doc, items


def proof(doc, items, folder, stem):
    """Each page with every item boxed and numbered: blue for a field the form
    has, orange for a blank with none."""
    made = []
    for page in doc:
        pix_doc = fitz.open()
        # (widgets=False: copying them empties the source's field list)
        pix_doc.insert_pdf(doc, from_page=page.number, to_page=page.number, widgets=False)
        pg = pix_doc[0]
        for i in items:
            if i["page"] != page.number:
                continue
            r = fitz.Rect(i["rect"])
            colour = (0.1, 0.3, 0.9) if i["existing"] is not None else (0.95, 0.5, 0.0)
            pg.draw_rect(r, color=colour, width=0.8)
            pg.insert_text(fitz.Point(r.x0, r.y0 - 1), i["id"], fontsize=6, color=colour)
        out = os.path.join(folder, f"context p{page.number + 1}.png")
        pg.get_pixmap(dpi=110, annots=False).save(out)
        made.append(out)
    return made


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("forms", nargs="+")
    ap.add_argument("-o", "--output", default=os.path.join(HERE, "field naming"))
    args = ap.parse_args()
    for path in args.forms:
        stem = os.path.splitext(os.path.basename(path))[0].strip()
        folder = os.path.join(args.output, stem)
        os.makedirs(folder, exist_ok=True)
        doc, items = read_form(path)
        with open(os.path.join(folder, "context.json"), "w", encoding="utf-8") as fh:
            json.dump({"form": os.path.basename(path), "items": items}, fh,
                      indent=1, ensure_ascii=False)
        proof(doc, items, folder, stem)
        have = sum(1 for i in items if i["existing"] is not None)
        print(f"{stem}: {have} field(s), {len(items) - have} blank(s) with none")


if __name__ == "__main__":
    main()
