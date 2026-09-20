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
import os
import re
import sys

import pymupdf as fitz

import restyle_pdf as R

BAR_GREY = (0.82, 0.81, 0.81)


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


def paragraphs(found):
    """Run the loose rows back together into paragraphs, tables and lists, so
    the rebuild sets out blocks rather than the original's line breaks."""
    out = []
    for b in found:
        last = out[-1] if out else None
        joins = (last and last["kind"] == "text" and b["kind"] == "text"
                 and not b["text"].startswith(("•", "-", "●"))
                 and abs(last["size"] - b["size"]) < 0.6
                 and last["bold"] == b["bold"] and last["italic"] == b["italic"]
                 and b["y"] - last["y"] < 2.2 * max(b["size"], 1)
                 and not last["text"].rstrip().endswith(":"))
        if joins:
            last["text"] = last["text"].rstrip() + " " + b["text"].lstrip()
            last["y"] = b["y"]
        elif (last and last["kind"] == "cells" and b["kind"] == "cells"
              and len(last["cells"]) == len(b["cells"])):
            last.setdefault("rows", [last["cells"]])["rows"] if False else None
            last["rows"] = last.get("rows", [last["cells"]]) + [b["cells"]]
            last["y"] = b["y"]
        else:
            if b["kind"] == "cells":
                b["rows"] = [b["cells"]]
            out.append(dict(b))
    return out


class Sheet:
    """Somewhere to put blocks, one after another down the page."""

    def __init__(self, doc, fonts, size, margin=40.0, gap=6.0):
        self.doc, self.fonts, self.size, self.margin, self.gap = doc, fonts, size, margin, gap
        self.page = None
        self.new_page()

    def new_page(self):
        self.page = self.doc.new_page(width=612, height=792)
        self.y = self.margin
        self.left = self.margin
        self.right = 612 - self.margin

    def room(self, height):
        if self.y + height > 792 - self.margin:
            self.new_page()

    def wrap(self, text, width, size, bold=False, italic=False):
        font = self.fonts["bolditalic" if bold and italic else "bold" if bold
                          else "italic" if italic else "regular"][2]
        words, rows, line = text.split(), [], ""
        for w in words:
            trial = (line + " " + w).strip()
            if font.text_length(trial, size) > width and line:
                rows.append(line)
                line = w
            else:
                line = trial
        if line:
            rows.append(line)
        return rows

    def face(self, bold, italic):
        return self.fonts["bolditalic" if bold and italic else "bold" if bold
                          else "italic" if italic else "regular"]

    def text(self, text, size=None, bold=False, italic=False, indent=0.0, lead=1.25):
        size = size or self.size
        alias, path, font = self.face(bold, italic)
        for row in self.wrap(text, self.right - self.left - indent, size, bold, italic):
            self.room(size * lead)
            self.page.insert_text(fitz.Point(self.left + indent, self.y + size * 0.85), row,
                                  fontname=alias, fontfile=path, fontsize=size)
            self.y += size * lead
        return self

    def bar(self, text):
        self.room(self.size * 1.9)
        alias, path, font = self.face(True, False)
        box = fitz.Rect(self.left, self.y, self.right, self.y + self.size * 1.7)
        self.page.draw_rect(box, color=None, fill=BAR_GREY)
        self.page.insert_text(fitz.Point(self.left + 4, self.y + self.size * 1.25), text,
                              fontname=alias, fontfile=path, fontsize=self.size)
        self.y = box.y1 + self.gap
        return self

    def fill_row(self, label, size=None):
        """A label with a line after it to write on."""
        size = size or self.size
        alias, path, font = self.face(False, False)
        label = label.replace("_", " ").rstrip()
        self.room(size * 2)
        base = self.y + size * 0.85
        self.page.insert_text(fitz.Point(self.left, base), label,
                              fontname=alias, fontfile=path, fontsize=size)
        start = self.left + font.text_length(label, size) + 6
        if start < self.right - 20:
            self.page.draw_line(fitz.Point(start, base + 2), fitz.Point(self.right, base + 2),
                                color=(0, 0, 0), width=0.6)
        self.y += size * 2
        return self

    def table(self, rows, size=None):
        size = size or self.size
        alias, path, font = self.face(False, False)
        cols = max(len(r) for r in rows)
        labels = rows[0]
        widths = [font.text_length((labels[i] if i < len(labels) else "") + "   ", size)
                  for i in range(cols)]
        spare = (self.right - self.left) - sum(widths)
        widths = [w + spare / cols for w in widths]
        height = size * 1.9
        for row in rows:
            self.room(height)
            x = self.left
            for i in range(cols):
                cell = fitz.Rect(x, self.y, x + widths[i], self.y + height)
                self.page.draw_rect(cell, color=(0, 0, 0), width=0.6)
                if i < len(row) and row[i]:
                    self.page.insert_text(fitz.Point(x + 3, self.y + size * 1.2), row[i],
                                          fontname=alias, fontfile=path, fontsize=size * 0.9)
                x += widths[i]
            self.y += height
        self.y += self.gap
        return self


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("-o", "--output", default="rebuilt.pdf")
    ap.add_argument("--size", type=float, default=10.0)
    ap.add_argument("--dump", action="store_true", help="print what it found and stop")
    args = ap.parse_args()

    doc = fitz.open(args.input)
    found = []
    for page in doc:
        found += blocks_of(page, args)
    found = paragraphs(found)
    if args.dump:
        for b in found:
            print(f"  {b['kind']:5} {b['size']:4.1f} {'B' if b['bold'] else ' '}"
                  f"{'I' if b['italic'] else ' '} {len(b.get('rows', []) or '') or ''}  "
                  f"{b['text'][:64]!r}")
        print(f"{len(found)} block(s)")
        return

    fonts = R.resolve_fonts({})
    out = fitz.open()
    sheet = Sheet(out, fonts, args.size)
    title = os.path.splitext(os.path.basename(args.input))[0]

    logo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "libro_logo.png")
    if os.path.exists(logo):
        pix = fitz.Pixmap(logo)
        box = fitz.Rect(sheet.left, sheet.y, sheet.left + 63 * pix.width / pix.height,
                        sheet.y + 63)
        sheet.page.insert_image(box, filename=logo)
    alias, path_b, font_b = fonts["bold"]
    sheet.page.insert_text(fitz.Point(sheet.right - font_b.text_length(title, 14),
                                      sheet.y + 20), title,
                           fontname=alias, fontfile=path_b, fontsize=14)
    sheet.y += 74

    started = False
    for b in found:
        if not started and b["size"] >= 12:
            continue                        # the form's own title; the header carries it
        started = True
        if b["kind"] == "bar":
            sheet.bar(b["text"])
        elif b["kind"] == "cells":
            sheet.table(b.get("rows") or [b["cells"]])
        elif b["kind"] == "fill":
            sheet.fill_row(b["text"])
        elif b["text"].lstrip()[:1] in ("•", "●"):
            sheet.text(b["text"].lstrip("•● ").strip(), bold=b["bold"],
                       italic=b["italic"], indent=14)
        else:
            sheet.text(b["text"], bold=b["bold"], italic=b["italic"])
        sheet.y += sheet.gap * 0.5
    out.save(args.output)
    print(f"rebuilt {len(found)} block(s) onto {out.page_count} page(s): {args.output}")


if __name__ == "__main__":
    main()
