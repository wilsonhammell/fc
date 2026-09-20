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
HIDDEN = []          # text the form draws in white on white


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


def rows_with_fields(page):
    """Each row of the form as an ordered run of things: text, then a field,
    then more text. "I, ___ of ___, in the Province of Ontario" is a row of
    five items, and keeping them in that order is what lets the rebuild put
    the blank back inside the sentence instead of at the end of the line."""
    p = R.printed(page)
    lines, _ = R.collect_lines(p, False, "Arial", 10.0, 0.6)
    items = []
    raw = {tuple(round(v, 1) for v in l["bbox"]): l
           for b in p.get_text("rawdict")["blocks"] if b.get("type") == 0
           for l in b["lines"]}
    for ln in lines:
        # A line can hold several labels set apart across the row - "Branch:",
        # "Owner Number:", "Contract Number:" - each with its own field. Split
        # where the gap between one letter and the next is wide enough to be a
        # tab stop, whether the extractor called them separate spans or ran
        # them together with spaces, so each label goes back beside its field.
        exact = raw.get(tuple(round(v, 1) for v in ln["bbox"]))
        chars = [c for sp in exact["spans"] for c in sp["chars"]] if exact else []
        sp0 = ln["spans"][0]
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
            rules = sum(1 for h in held if h["c"] == "_")
            if run and (c is None or spaces > 8 or rules >= 3
                        or c["bbox"][0] - run[-1]["bbox"][2] > 8):
                text = "".join(x["c"] for x in run).strip()
                if text:
                    box = fitz.Rect(run[0]["bbox"])
                    for x in run[1:]:
                        box |= fitz.Rect(x["bbox"])
                    items.append(dict(style, what="text", text=text, rect=box,
                                      base=run[0]["origin"][1]))
                run = []
            elif held:
                run += held
            held = []
            if c is not None:
                run.append(c)
    for w in page.widgets():
        r = fitz.Rect(w.rect)
        # a field sits astride the baseline of its own label, so its middle
        # lines up with the text far better than its bottom edge does
        items.append({"what": "field", "rect": r, "base": (r.y0 + r.y1) / 2 + 2,
                      "name": w.field_name, "type": w.field_type,
                      "kind": w.field_type_string, "size": 10.0,
                      "bold": False, "italic": False,
                      "flags": w.field_flags, "text": ""})
    # group into rows by baseline first, then read each row left to right:
    # sorting by baseline throughout puts a field a point higher than its own
    # sentence ahead of it, and the blank ends up before the words it sits in
    items.sort(key=lambda i: i["base"])
    rows, current, last = [], [], None
    for it in items:
        if last is not None and abs(it["base"] - last) > 5:
            rows.append(current)
            current = []
        current.append(it)
        last = it["base"]
    if current:
        rows.append(current)
    for row in rows:
        row.sort(key=lambda i: i["rect"].x0)
    return rows


def is_table_row(row):
    """A row that reads label, box, label, box ... is a table row: the
    beneficiary grids are built that way, and they have to come back as a
    grid rather than a line of labels with rules trailing after it."""
    kinds = [i["what"] for i in row]
    return (len(row) >= 6 and kinds[0] == "text"        # a cell starts with its label
            and kinds.count("field") >= 3 and kinds.count("text") >= 3
            and all(a != b for a, b in zip(kinds, kinds[1:])))


def merge_prose(rows):
    """Join rows that are a single run of text into paragraphs, so the
    rebuild wraps them to its own column instead of keeping the original's
    line breaks."""
    def bullet_row(row):
        return (len(row) == 2 and all(i["what"] == "text" for i in row)
                and row[0]["text"].strip() in ("•", "●", "-", "*"))

    out = []
    for row in rows:
        plain = len(row) == 1 and row[0]["what"] == "text"
        last = out[-1] if out else None
        if (plain and last and bullet_row(last)
                and 0 < row[0]["base"] - last[1]["base"] < 2.2 * row[0]["size"]
                and abs(last[1]["size"] - row[0]["size"]) < 0.6):
            # the rest of the bullet's sentence, which the form broke across
            # lines; it belongs to the bullet, indent and all
            joined = dict(last[1])
            joined["text"] = last[1]["text"].rstrip() + " " + row[0]["text"].lstrip()
            joined["base"] = row[0]["base"]
            out[-1] = [last[0], joined]
            continue
        if (plain and last and len(last) == 1 and last[0]["what"] == "text"
                and abs(last[0]["size"] - row[0]["size"]) < 0.6
                and last[0]["bold"] == row[0]["bold"]
                and last[0]["italic"] == row[0]["italic"]
                and 0 < row[0]["base"] - last[0]["base"] < 2.2 * row[0]["size"]
                and not last[0]["text"].rstrip().endswith(":")
                and row[0]["text"].lstrip()[:1] not in ("•", "●")):
            joined = dict(last[0])
            joined["text"] = last[0]["text"].rstrip() + " " + row[0]["text"].lstrip()
            joined["base"] = row[0]["base"]
            out[-1] = [joined]
        else:
            out.append(row)
    return out


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


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("-o", "--output", default="rebuilt.pdf")
    ap.add_argument("--size", type=float, default=10.0)
    ap.add_argument("--dump", action="store_true", help="print what it found and stop")
    args = ap.parse_args()

    doc = fitz.open(args.input)
    fonts = R.resolve_fonts({})
    out = fitz.open()
    sheet = Rebuild(out, fonts, args.size)
    title = os.path.splitext(os.path.basename(args.input))[0]

    logo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "libro_logo.png")
    if os.path.exists(logo):
        pix = fitz.Pixmap(logo)
        sheet.page.insert_image(
            fitz.Rect(sheet.left, sheet.y, sheet.left + 63 * pix.width / pix.height,
                      sheet.y + 63), filename=logo)
    alias, bold_path, bold_font = fonts["bold"]
    sheet.page.insert_text(
        fitz.Point(sheet.right - bold_font.text_length(title, 14), sheet.y + 20), title,
        fontname=alias, fontfile=bold_path, fontsize=14)
    sheet.y += 74

    bars = []
    for page in doc:
        p = R.printed(page)
        bars += [fitz.Rect(d["rect"]) for d in p.get_drawings()
                 if d.get("fill") and 6 < fitz.Rect(d["rect"]).height < 30
                 and fitz.Rect(d["rect"]).width > 60]
    rows = []
    for page in doc:
        rows += rows_with_fields(page)

    rows = merge_prose(rows)
    started = False
    pending = []
    skip_next = False
    for idx, row in enumerate(rows + [None]):
        if row is not None and is_table_row(row):
            pending.append(row)
            continue
        if pending:
            # One row of label-and-box is not a table, it's a line of labels:
            # "Branch:", "Owner Number:", "Contract Number:" came back boxed
            # in as if they were a grid. A table is rows, plural.
            if len(pending) > 1:
                sheet.table(pending)
            else:
                sheet.row(pending[0])
            pending = []
        if row is None:
            break
        texts = [i for i in row if i["what"] == "text"]
        has_field = any(i["what"] == "field" for i in row)
        joined = " ".join(i["text"] for i in texts).strip()
        if not started and texts and texts[0]["size"] >= 12 and not has_field:
            continue                        # the form's own title; the header carries it
        started = True
        if not joined and not has_field:
            continue
        if skip_next:
            skip_next = False
            continue
        if is_rule_row(row):
            following = rows[idx + 1] if idx + 1 < len(rows) else None
            if following is not None and is_caption_row(following, slot_count(row)):
                sheet.signature(row, following)
                skip_next = True
            else:
                sheet.signature(row)
            continue
        if joined.rstrip().endswith(":") and len(joined) < 40:
            sheet.keep(sheet.size * 4)          # "NOTE:" keeps its notes
        in_bar = texts and any(b.y0 - 3 <= texts[0]["rect"].y0
                               and texts[0]["rect"].y1 <= b.y1 + 3 for b in bars)
        if in_bar and not has_field:
            sheet.keep(sheet.size * 4)          # a heading keeps its first lines
            sheet.bar(joined)
        elif (len(texts) == 2 and not has_field
              and texts[0]["text"].strip() in ("•", "●", "-", "*")):
            # a bullet and its sentence are one thing: set as a hanging
            # paragraph, not as two items that wrap apart
            sheet.prose("•  " + texts[1]["text"], bold=texts[1]["bold"],
                        italic=texts[1]["italic"], indent=14)
        elif has_field or len(texts) > 1:
            sheet.row(row)
        else:
            bullet = joined.lstrip()[:1] in ("•", "●", "-")
            sheet.prose(joined.lstrip("•●- ") if bullet else joined,
                        bold=texts[0]["bold"], italic=texts[0]["italic"],
                        indent=14 if bullet else 0)
        sheet.y += 2
    out.save(args.output)
    was = sum(1 for page in doc for _ in page.widgets())
    print(f"rebuilt onto {out.page_count} page(s): {args.output}")
    print(f"fields: {was} in the original, {len(sheet.fields)} in the rebuild")


if __name__ == "__main__":
    main()
