"""Does the output still look like the form it came from?

Every other check asks whether something is *there*: each glyph, each word,
each field, each rule. None of them asks whether it still looks the way it
did, and that is where the client's review found what they missed - a
sentence come out bold, a grey header row gone white, radio buttons walked
to the end of their sentence, a line broken in the middle with room to spare,
headings that were centred set flush left, an underline dropped.

So this matches the original's words to the output's, in reading order, and
for every word that is in both asks:

  STYLE_CHANGED      bold, italic or underline is not what it was
  COLOUR_CHANGED     the text is a different colour
  FILL_CHANGED       the shading behind it is gone, new, or a different shade
  FIELD_MOVED        a field no longer sits beside the words it sat beside
  NEW_LINE_BREAK     a line now breaks where it didn't, with room for the
                     next word
  ALIGNMENT_BROKEN   lines that shared a left edge, or a row, no longer do
  CENTRING_LOST      a line that was centred in its box no longer is
  RULES_CHANGED      more or fewer ruled lines, boxes or shaded panels

Words that were reworded, added or taken out are not in both and are not
compared - the census, the word check and the relabel report cover them.
Matching across the whole document rather than page by page is what lets
this read a rebuild, which re-paginates.
"""

import difflib
import re
from collections import Counter

import pymupdf as fitz

# restyle_pdf, for printed() and rule_segments(). The restyle hands over its
# own when it calls in, since it runs as __main__ and importing it again
# would load a second copy; run any other way, it is imported on first use.
R = None


def _restyle():
    global R
    if R is None:
        import restyle_pdf
        R = restyle_pdf
    return R

BOLD = re.compile(r"bold|black|heavy|demi", re.I)
WORD = re.compile(r"[^\W_]+")


def bold_name(font):
    return bool(BOLD.search(font or ""))


def italic_name(font):
    """Italic by name. Not every face says "Italic": Source Sans calls its
    italic "-It" and its bold italic "-BoldIt"."""
    tail = re.split(r"[-,]", font or "")[-1].lower()
    return "italic" in tail or "oblique" in tail or tail.endswith("it")


def key_of(text):
    """What a word is compared as: its letters and digits, in lower case.
    Punctuation drifts in a rebuild ("Debit :" set as "Debit:") without the
    word changing."""
    return "".join(WORD.findall(text)).lower()


def near(a, b, tol):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def shade(colour):
    """A fill as compared: None for white or near it, else its colour to two
    places. White panels laid on grey ones are how a form clears a cell, so
    white counts as no shading at all."""
    if colour is None or min(colour) >= 0.95:
        return None
    return tuple(round(c, 2) for c in colour)


NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)"
TM = re.compile(r"((?:%s\s+){6})Tm\b" % NUM)


def slanted_starts(page):
    """Where the page sets text through a slanted text matrix, as (x, y)
    starts in page coordinates, along with every other start.

    That is how Word draws italic in a font that has none: the regular face,
    sheared. Neither the font name nor the extractor says so - the only
    trace of it is the matrix - so a form whose italic notes came out
    upright passed every check. Returns (slanted, upright), each
    {baseline: [x, ...]}; both empty if the page places text some other way."""
    slanted, upright = {}, {}
    try:
        stream = page.read_contents().decode("latin-1")
    except Exception:
        return slanted, upright
    h = page.rect.height
    for m in TM.finditer(stream):
        a, b, c, d, e, f = (float(v) for v in m.group(1).split())
        into = slanted if abs(c) > 0.05 * max(abs(d), 1e-6) and abs(b) < 1e-6 else upright
        into.setdefault(round(h - f), []).append(e)
    return slanted, upright


def is_slanted(origin, slanted, upright):
    """Was the glyph at `origin` set under a slanted matrix? Each glyph
    belongs to the nearest start at or before it on its baseline."""
    y = round(origin[1])
    best, slant = None, False
    for dy in (-1, 0, 1):
        for into, flag in ((slanted, True), (upright, False)):
            for x in into.get(y + dy, ()):
                if x <= origin[0] + 0.5 and (best is None or x > best):
                    best, slant = x, flag
    return slant


def read(doc, skip=None):
    """Every word the document prints, in reading order, with how it looks.

    `skip` is {page number: [rects]} of places to leave out - the parts a
    tool replaces on purpose, like the header band."""
    _restyle()
    skip = skip or {}
    words, fields, pages = [], [], []
    for page in doc:
        p = R.printed(page)
        pno = page.number
        # each glyph's look, from the text trace: font, colour, and whether
        # it is stroked - faux bold, a regular face drawn with an outline
        # Faux bold is the same glyphs drawn twice, filled and then stroked:
        # a glyph is bold if any stroke lands on it, not by a vote between
        # its two copies.
        glyphs, stroked = {}, set()
        slanted, upright = slanted_starts(p)
        trace = p.get_texttrace()
        filled = {tuple(round(v, 1) for v in run["bbox"])
                  for run in trace if run.get("type") == 0}
        for run in trace:
            if run.get("type") == 1:
                stroked.update((round(c[2][0], 1), round(c[2][1], 1))
                               for c in run.get("chars", ()))
        for run in trace:
            if run.get("type") not in (0, 1):
                continue
            if run.get("type") == 1 and tuple(round(v, 1) for v in run["bbox"]) in filled:
                continue                     # the outline of a filled copy
            for c in run.get("chars", ()):
                if c[0] <= 32:
                    continue
                look = (bold_name(run["font"]) or bool(run.get("flags", 0) & 16)
                        or run.get("type") == 1
                        or (round(c[2][0], 1), round(c[2][1], 1)) in stroked,
                        italic_name(run["font"]) or bool(run.get("flags", 0) & 2)
                        or is_slanted(c[2], slanted, upright),
                        tuple(round(v, 2) for v in (run.get("color") or (0, 0, 0))))
                r = fitz.Rect(c[3])
                cx, cy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
                glyphs.setdefault(int(cy // 6), []).append((cx, cy, look, c[2][1]))
        draws = p.get_drawings()
        fills = [(d.get("seqno", 0), fitz.Rect(d["rect"]), d["fill"]) for d in draws
                 if d.get("fill") is not None and d["type"] in ("f", "fs")
                 and d["rect"].width >= 6 and d["rect"].height >= 6]
        segs = segments(p, draws)
        flat = [s for s in segs if s.height < 2.5 and s.width >= 4]
        walls = [s for s in segs if s.width < 2.5 and s.height >= 6]
        pages.append({"rect": page.rect, "upright": walls,
                      "art": art_counts(draws, segs)})
        lines = {}
        for x0, y0, x1, y1, text, b, l, _ in p.get_text("words", sort=True):
            key = key_of(text)
            r = fitz.Rect(x0, y0, x1, y1)
            if not key or any(r.intersects(z) for z in skip.get(pno, ())):
                continue
            looks, bases = Counter(), []
            for k in range(int(y0 // 6) - 1, int(y1 // 6) + 2):
                for cx, cy, look, base in glyphs.get(k, ()):
                    if x0 - 0.5 <= cx <= x1 + 0.5 and y0 - 0.5 <= cy <= y1 + 0.5:
                        looks[look] += 1
                        bases.append(base)
            bold, italic, colour = (looks.most_common(1)[0][0] if looks
                                    else (False, False, (0, 0, 0)))
            mid = fitz.Point((x0 + x1) / 2, (y0 + y1) / 2)
            under = [f for f in fills if f[1].contains(mid)]
            fill = shade(max(under, key=lambda f: f[0])[2]) if under else None
            w = {"page": pno, "rect": r, "text": text, "key": key, "bold": bold,
                 "italic": italic, "colour": colour, "fill": fill,
                 "line": (pno, b, l),
                 "base": max(bases) if bases else y1 - 0.2 * (y1 - y0)}
            words.append(w)
            lines.setdefault((b, l), []).append(w)
        for ws in lines.values():
            box = fitz.Rect(ws[0]["rect"])
            for w in ws[1:]:
                box |= w["rect"]
            for k, w in enumerate(ws):
                w["first"], w["last"] = k == 0, k == len(ws) - 1
                w["linebox"] = box
                # underlined: a rule from the baseline to a little under it,
                # that belongs to the word's line - not a cell's border
                # running on past the line's ends. Measured from the baseline,
                # not the word's box: shrunk type has a shallower box, and
                # measured from that a rule under it came and went.
                r, base = w["rect"], w["base"]
                drop = max(2.5, 0.35 * r.height)
                w["under"] = "_" not in w["text"] and any(
                    base - 0.5 <= (s.y0 + s.y1) / 2 <= base + drop
                    and s.x0 >= box.x0 - 3 and s.x1 <= box.x1 + 3
                    and min(s.x1, r.x1) - max(s.x0, r.x0) >= 0.6 * r.width
                    for s in flat)
        for wd in page.widgets():
            fields.append({"page": pno, "rect": fitz.Rect(wd.rect),
                           "name": wd.field_name or "", "type": wd.field_type_string})
    return words, fields, pages


def segments(page, draws):
    """Every ruled line and box edge, however it was drawn: stroked lines,
    rectangle edges, and slivers of fill - which Word uses for table borders,
    upright ones included."""
    out = list(R.rule_segments(page))
    for d in draws:
        r = fitz.Rect(d["rect"])
        if d["type"] in ("f", "fs") and d.get("fill") is not None \
                and min(d["fill"]) < 0.99 and r.width < 4 and r.height >= 4:
            out.append(r)
    return out


def art_counts(draws, segs):
    """How many shaded panels the page has, counted as a reader sees them:
    a form often lays a shaded row as two overlapping panels.

    Ruled lines are not counted here. A rebuild draws a rule for every typed
    blank, so their number changes by design, and the restyle's own
    line-art check already accounts for every one it touches."""
    out = Counter()
    panels = []
    for d in draws:
        r = fitz.Rect(d["rect"])
        if d.get("fill") is not None and d["type"] in ("f", "fs") \
                and shade(d["fill"]) and r.width >= 20 and r.height >= 6:
            if not any((r & q).get_area() >= 0.6 * min(r.get_area(), q.get_area())
                       for q in panels):
                panels.append(r)
    out["shaded panel"] = len(panels)
    return out


def match(was, now):
    """{index in was: index in now} for the words the two have in common, in
    order. Blocks that moved are found by a second pass over what the first
    left over, so a footer the rebuild set at the foot of the last sheet is
    still compared."""
    pairs = {}
    a, b = [w["key"] for w in was], [w["key"] for w in now]
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            pairs[blk.a + k] = blk.b + k
    left_a = [i for i in range(len(a)) if i not in pairs]
    used = set(pairs.values())
    left_b = [j for j in range(len(b)) if j not in used]
    sm = difflib.SequenceMatcher(None, [a[i] for i in left_a],
                                 [b[j] for j in left_b], autojunk=False)
    for blk in sm.get_matching_blocks():
        if blk.size >= 4:                  # a run, not a stray common word
            for k in range(blk.size):
                pairs[left_a[blk.a + k]] = left_b[blk.b + k]
    return pairs


def runs(hits, was):
    """Group flagged original word indices into runs of neighbours, so a
    sentence come out bold is one finding, not twelve."""
    out = []
    for i in sorted(hits):
        if out and i - out[-1][-1] <= 2 and was[i]["page"] == was[out[-1][-1]]["page"]:
            out[-1].append(i)
        else:
            out.append([i])
    return out


def quote(ws, limit=60):
    text = " ".join(w["text"] for w in ws)
    return repr(text if len(text) <= limit else text[:limit - 3] + "...")


def where(w):
    """The page a finding is reported on: always the output's, which is the
    one the proofs show."""
    return w["page"] + 1


def style_word(w):
    bits = [n for n, on in (("bold", w["bold"]), ("italic", w["italic"]),
                            ("underlined", w["under"])) if on]
    return " ".join(bits) or "regular"


def check_style(was, now, pairs):
    """Weight, slant and underline, word by word."""
    found = []
    by_change = {}
    for i, j in pairs.items():
        a, b = was[i], now[j]
        if (a["bold"], a["italic"], a["under"]) != (b["bold"], b["italic"], b["under"]):
            by_change.setdefault((style_word(a), style_word(b)), set()).add(i)
    for (old, new), hits in by_change.items():
        for run in runs(hits, was):
            ws = [was[i] for i in run]
            found.append((where(now[pairs[run[0]]]), "STYLE_CHANGED",
                          f"{quote(ws)} was {old}, now {new}"))
    return found


def check_colour(was, now, pairs):
    found, hits = [], {}
    for i, j in pairs.items():
        a, b = inked(was[i]["colour"]), inked(now[j]["colour"])
        if max(abs(x - y) for x, y in zip(a, b)) > 0.12:
            hits.setdefault((a, b), set()).add(i)
    for (a, b), idx in hits.items():
        for run in runs(idx, was):
            ws = [was[i] for i in run]
            found.append((where(now[pairs[run[0]]]), "COLOUR_CHANGED",
                          f"{quote(ws)} was {colour_name(a)}, now {colour_name(b)}"))
    return found


def inked(c):
    """A colour as a reader sees it: Word's "automatic" text is often a grey
    of 0.1 or so, which prints as black and is black."""
    if max(c) - min(c) < 0.05 and max(c) < 0.25:
        return (0.0, 0.0, 0.0)
    return c


def colour_name(c):
    if c is None:
        return "unshaded"
    if max(c) - min(c) < 0.05:
        return "black" if c[0] < 0.1 else f"grey {c[0]:.2f}"
    return "#%02x%02x%02x" % tuple(int(round(v * 255)) for v in c)


def check_fill(was, now, pairs):
    found, hits = [], {}
    for i, j in pairs.items():
        a, b = was[i]["fill"], now[j]["fill"]
        if a == b or (a and b and max(abs(x - y) for x, y in zip(a, b)) <= 0.08):
            continue
        hits.setdefault((a, b), set()).add(i)
    for (a, b), idx in hits.items():
        for run in runs(idx, was):
            ws = [was[i] for i in run]
            what = ("lost its shading" if b is None else
                    "is shaded now" if a is None else "is shaded differently")
            found.append((where(now[pairs[run[0]]]), "FILL_CHANGED",
                          f"{quote(ws)} {what}: was {colour_name(a)}, "
                          f"now {colour_name(b)}"))
    return found


def anchors(fields, words):
    """For each field, the words beside it on its row: the nearest to its left
    and to its right, as indices into `words`. Fields are told apart by name
    and by their order among fields of that name - radio buttons share one."""
    rows = {}
    for k, w in enumerate(words):
        rows.setdefault(w["page"], []).append(k)
    seen, out = Counter(), {}
    for f in sorted(fields, key=lambda f: (f["page"], f["rect"].y0, f["rect"].x0)):
        name = (f["name"], seen[f["name"]])
        seen[f["name"]] += 1
        r, cy = f["rect"], (f["rect"].y0 + f["rect"].y1) / 2
        cx = (r.x0 + r.x1) / 2
        left = right = None
        for k in rows.get(f["page"], ()):
            w = words[k]["rect"]
            if not (w.y0 - 2 <= cy <= w.y1 + 2):
                continue
            # by centres: a form's own box glyph sits under its tick box and
            # makes the word after it start there
            if (w.x0 + w.x1) / 2 < cx and (left is None or w.x1 > words[left]["rect"].x1):
                left = k
            if (w.x0 + w.x1) / 2 > cx and (right is None or w.x0 < words[right]["rect"].x0):
                right = k
        out[name] = (left, right, f)
    return out


TICKS = ("CheckBox", "RadioButton")


def check_fields(was, now, pairs, was_fields, now_fields):
    """A tick box or radio button kept beside its words: the one between
    "Yes" and "No" is still between them, the one after "Debit" still after
    it. Text and signature fields are left out - a rebuild sets those on a
    line of their own above their caption by design, and the signing
    standard moves them on purpose."""
    found = []
    old = anchors([f for f in was_fields if f["type"] in TICKS], was)
    new = anchors([f for f in now_fields if f["type"] in TICKS], now)
    for name, (left, right, f) in old.items():
        if name not in new:
            continue                         # FIELD_LOST is the field check's
        nleft, nright, g = new[name]
        moved = []
        if left is not None and left in pairs and pairs[left] != nleft:
            moved.append("after " + repr(was[left]["text"]))
        if right is not None and right in pairs and pairs[right] != nright:
            moved.append("before " + repr(was[right]["text"]))
        if moved:
            now_at = []
            if nleft is not None:
                now_at.append("after " + repr(now[nleft]["text"]))
            if nright is not None:
                now_at.append("before " + repr(now[nright]["text"]))
            found.append((g["page"] + 1, "FIELD_MOVED",
                          f"{f['type'].lower()} {name[0]!r} sat {' and '.join(moved)}; "
                          f"now {' and '.join(now_at) or 'on a row of its own'}"))
    return found


def paragraph_right(now, j):
    """How far the lines of word j's paragraph reach: the furthest right any
    of its other lines gets, not counting its last. Lines are the paragraph's
    while they keep its left edge and its leading."""
    me = now[j]["linebox"]
    page = now[j]["page"]
    boxes = sorted({id(w["linebox"]): w["linebox"] for w in now
                    if w["page"] == page and abs(w["linebox"].x0 - me.x0) <= 2}.values(),
                   key=lambda b: b.y0)
    k = next(i for i, b in enumerate(boxes) if b is me)
    para = [me]
    lead = me.height * 1.8
    for step in (-1, 1):
        i = k
        while 0 <= i + step < len(boxes) and abs(boxes[i + step].y0 - boxes[i].y0) <= lead:
            i += step
            para.append(boxes[i])
    para.sort(key=lambda b: b.y0)
    others = [b.x1 for b in para[:-1] if b is not me]
    return max(others) if others else None


def check_breaks(was, now, pairs, pages):
    """A line that now ends early: two words that shared a line in the form
    are split across two, and the first line had room for the second word.
    Room is measured against the furthest its paragraph's other lines reach,
    or the box's right-hand side where the line sits in one."""
    found = []
    for i in range(len(was) - 1):
        a, b = was[i], was[i + 1]
        if a["line"] != b["line"] or i not in pairs or i + 1 not in pairs:
            continue
        j, k = pairs[i], pairs[i + 1]
        if k != j + 1 or not now[j]["last"] or not now[k]["first"]:
            continue
        if now[j]["page"] != now[k]["page"]:
            continue                        # a page break is the pager's call
        if abs(now[j]["rect"].y1 - now[k]["rect"].y1) <= 2:
            continue                        # still one row, read as two lines
        line = now[j]["linebox"]
        reach = paragraph_right(now, j)
        walls = [s.x0 for s in pages[now[j]["page"]]["upright"]
                 if s.x0 >= line.x1 - 1 and s.y0 <= line.y0 + 1 and s.y1 >= line.y1 - 1]
        if walls:
            wall = min(walls) - 3
            reach = wall if reach is None else min(max(reach, line.x1), wall)
        if reach is None:
            continue
        room = reach - line.x1
        need = now[k]["rect"].width + now[k]["rect"].height * 0.3
        if room > need + max(6.0, now[k]["rect"].height * 0.6):
            ws = [now[x] for x in range(max(0, j - 4), min(len(now), k + 4))]
            found.append((now[j]["page"] + 1, "NEW_LINE_BREAK",
                          f"breaks after {now[j]['text']!r} in {quote(ws)} with "
                          f"{room:.0f}pt to spare; {now[k]['text']!r} needs "
                          f"{need:.0f}pt"))
    return found


def line_starts(words, pairs=None):
    """{line: first word index} for every line, in order."""
    out = {}
    for k, w in enumerate(words):
        if w["first"]:
            out[w["line"]] = k
    return out


DRIFT = 5.0        # points: less than this is a rebuild evening out edges


def short(words, i):
    """A label, a caption, a line of an address - not a line of prose. Prose
    is re-wrapped and re-indented by design, and its lines' edges with it."""
    return sum(1 for w in words if w["line"] == words[i]["line"]) <= 6


def check_alignment(was, now, pairs):
    """Short lines that shared a left edge (a stacked label column, a return
    address) or a row (labels across a table) still do. Only lines that are
    still lines are held to it: one whose first word has been re-wrapped into
    the middle of a line is the reflow's business."""
    found = []
    starts = line_starts(was)
    kept = {}
    for line, i in starts.items():
        j = pairs.get(i)
        # a line led by a box glyph or a bullet starts where its symbol does,
        # and the rebuild draws those as shapes of their own
        if j is not None and now[j]["first"] and was[i]["text"][:1].isalnum() \
                and now[j]["text"][:1].isalnum():
            kept[line] = (i, j)
    order = sorted(kept.values(), key=lambda p: (was[p[0]]["page"],
                                                 was[p[0]]["linebox"].y0,
                                                 was[p[0]]["linebox"].x0))
    stacked, rowed = [], []
    for n, (i, j) in enumerate(order):
        a, na = was[i]["linebox"], now[j]["linebox"]
        for i2, j2 in order[n + 1:]:
            if was[i2]["page"] != was[i]["page"]:
                break
            b, nb = was[i2]["linebox"], now[j2]["linebox"]
            if b.y0 - a.y1 > 2.2 * a.height:
                break
            if now[j2]["page"] != now[j]["page"]:
                continue
            if not (short(was, i) and short(was, i2)):
                continue
            if abs(a.x0 - b.x0) <= 1 and b.y0 >= a.y1 - 1 and abs(na.x0 - nb.x0) > DRIFT:
                stacked.append((i, i2, na.x0, nb.x0))
            if abs(a.y1 - b.y1) <= 1 and abs(na.y1 - nb.y1) > DRIFT:
                rowed.append((i, i2, na.y1, nb.y1))
    for i, i2, x, x2 in stacked:
        found.append((now[pairs[i]]["page"] + 1, "ALIGNMENT_BROKEN",
                      f"{quote(line_of(was, i), 30)} and {quote(line_of(was, i2), 30)} "
                      f"started at the same left edge; now {abs(x - x2):.0f}pt apart"))
    for i, i2, y, y2 in rowed:
        found.append((now[pairs[i]]["page"] + 1, "ALIGNMENT_BROKEN",
                      f"{quote(line_of(was, i), 30)} and {quote(line_of(was, i2), 30)} "
                      f"shared a row; now {abs(y - y2):.0f}pt apart"))
    return found


def line_of(words, i):
    return [w for w in words if w["line"] == words[i]["line"]]


def box_around(line, page):
    """The room a line sits in, side to side: between the nearest upright
    rules either side of it, or the page's printed width if it has none."""
    ups = page["upright"]
    left = [s.x1 for s in ups if s.x1 <= line.x0 + 1
            and s.y0 <= line.y0 + 2 and s.y1 >= line.y1 - 2]
    right = [s.x0 for s in ups if s.x0 >= line.x1 - 1
             and s.y0 <= line.y0 + 2 and s.y1 >= line.y1 - 2]
    if left and right:
        return max(left), min(right)
    return None


def centred(line, page):
    box = box_around(line, page)
    if box is None:
        return None
    lo, hi = box
    if line.x0 - lo < 12:
        return False                       # set from the left side
    # a form pads a centred caption with spaces as often as not, and the
    # padding is not ink: allow a couple of spaces' worth either way
    return abs((line.x0 + line.x1) / 2 - (lo + hi) / 2) <= max(4.0, 0.02 * (hi - lo))


def check_centring(was, now, pairs, was_pages, now_pages):
    found = []
    for line, i in line_starts(was).items():
        ws = line_of(was, i)
        idx = [was.index(w) for w in ws]
        if not all(k in pairs for k in idx):
            continue                         # reworded: its width changed
        j0, j1 = pairs[idx[0]], pairs[idx[-1]]
        if not (now[j0]["first"] and now[j1]["last"] and now[j0]["line"] == now[j1]["line"]):
            continue
        if centred(ws[0]["linebox"], was_pages[ws[0]["page"]]) and \
                centred(now[j0]["linebox"], now_pages[now[j0]["page"]]) is False:
            found.append((now[j0]["page"] + 1, "CENTRING_LOST",
                          f"{quote(ws)} was centred in its box; now set from the left"))
    return found


def check_art(was_pages, now_pages, allowed=None):
    """More or fewer rules, boxes and shaded panels, over the document."""
    was, now = Counter(), Counter()
    for p in was_pages:
        was.update(p["art"])
    for p in now_pages:
        now.update(p["art"])
    if allowed:
        was.update(allowed)
    found = []
    for kind in sorted(set(was) | set(now)):
        if was[kind] != now[kind]:
            found.append(("", "RULES_CHANGED",
                          f"{plural(kind)}: {was[kind]} in the form, {now[kind]} now"))
    return found


SHOWN = 4


def gather(found):
    """One row per finding while there are a few, one row per page and kind
    when there are many: a page whose every heading lost its bold is one
    thing to look at, and forty rows of it bury the rest of the report."""
    by = {}
    for page, issue, detail in found:
        by.setdefault((page, issue), []).append(detail)
    out = []
    for (page, issue), details in by.items():
        if len(details) <= SHOWN:
            out += [(page, issue, d) for d in details]
        else:
            out.append((page, issue, f"{len(details)} on this page, among them: "
                        + "; ".join(details[:SHOWN - 1]) + " ..."))
    return out


def plural(kind):
    return kind.replace("row of", "rows of") if kind.startswith("row of") else kind + "s"


def compare(before, after, skip_before=None, skip_after=None, art=True,
            art_allowed=None):
    """Everything above, for two open documents. Returns [(page, issue,
    detail)], page 1-based on the output's side, or "" for the document."""
    was, was_fields, was_pages = read(before, skip_before)
    now, now_fields, now_pages = read(after, skip_after)
    pairs = match(was, now)
    found = (check_style(was, now, pairs) + check_colour(was, now, pairs)
             + check_fill(was, now, pairs)
             + check_fields(was, now, pairs, was_fields, now_fields)
             + check_breaks(was, now, pairs, now_pages)
             + check_alignment(was, now, pairs)
             + check_centring(was, now, pairs, was_pages, now_pages))
    if art:
        found += check_art(was_pages, now_pages, art_allowed)
    return gather(found)


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("before")
    ap.add_argument("after")
    args = ap.parse_args()
    with fitz.open(args.before) as a, fitz.open(args.after) as b:
        found = compare(a, b)
    for page, issue, detail in found:
        print(f"p{page}\t{issue}\t{detail}")
    print("looks: same" if not found else f"looks: {len(found)} difference(s)")
    return 1 if found else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
