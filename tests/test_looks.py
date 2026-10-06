"""Sabotage tests for the look checks: make a small form, spoil one thing
about how it looks in each copy, and check each spoiling is reported - and
that an untouched copy reports nothing.

    python tests/test_looks.py
"""
import sys, os
import pymupdf as fitz

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(here))
import looks

GREY = (0.8, 0.8, 0.8)
PROSE = ("The undersigned confirms that the funds will be held in trust and "
         "applied only as the agreement sets out, and that any change to these "
         "terms must be made in writing and signed by both parties before it "
         "takes effect.")


def make(path, spoil=None):
    """The form: a centred heading in a ruled box, a shaded table head, a
    bold run-in heading, an underlined note, a blue link, a slanted (Word's
    italic) note, a stacked pair of labels, a Yes/No tick box pair, and a
    paragraph of prose. `spoil` names the one thing to get wrong."""
    doc = fitz.open()
    page = doc.new_page()
    W = page.rect.width
    # a ruled box, the heading centred in it
    for x in (72, W - 72):
        page.draw_rect(fitz.Rect(x, 60, x + 0.5, 90), color=None, fill=(0, 0, 0))
    heading = "Member Identification"
    hw = fitz.get_text_length(heading, "helv", 10)
    hx = 80 if spoil == "centring" else (W - hw) / 2
    page.insert_text((hx, 79), heading, fontname="helv", fontsize=10)
    # a shaded table head
    if spoil != "fill":
        page.draw_rect(fitz.Rect(72, 100, W - 72, 116), color=None, fill=GREY)
    page.insert_text((80, 112), "Name      Phone      Email", fontsize=10)
    # a run-in bold heading
    font = "helv" if spoil == "bold" else "hebo"
    page.insert_text((72, 140), "Renewal:", fontname=font, fontsize=10)
    page.insert_text((72 + fitz.get_text_length("Renewal: ", font, 10), 140),
                     "the term renews unless you tell us otherwise.", fontsize=10)
    # an underlined note
    page.insert_text((72, 160), "Please note", fontsize=10)
    if spoil != "underline":
        page.draw_line((72, 161.5), (72 + fitz.get_text_length("Please note", "helv", 10), 161.5),
                       width=0.6)
    # a blue link
    page.insert_text((72, 180), "www.example.ca", fontsize=10,
                     color=(0, 0, 0) if spoil == "colour" else (0.12, 0.37, 0.62))
    # two stacked labels
    page.insert_text((72, 210), "Branch:", fontsize=10)
    page.insert_text((92 if spoil == "alignment" else 72, 226), "Transit:", fontsize=10)
    # Yes [ ] No [ ] - the box between "Yes" and "No"
    page.insert_text((72, 250), "Yes", fontsize=10)
    page.insert_text((110, 250), "No", fontsize=10)
    page.insert_text((140, 250), "if none apply", fontsize=10)
    w = fitz.Widget()
    w.field_type = fitz.PDF_WIDGET_TYPE_CHECKBOX
    w.field_name = "yes_box"
    w.rect = fitz.Rect(160, 241, 169, 250) if spoil == "field" else fitz.Rect(95, 241, 104, 250)
    page.add_widget(w)
    # prose, broken where it runs out of room - or early, with room to spare
    y, line = 290, ""
    words = PROSE.split()
    for k, word in enumerate(words):
        trial = (line + " " + word).strip()
        early = spoil == "break" and k == 9 and y == 290
        if fitz.get_text_length(trial, "helv", 10) > W - 144 or early:
            page.insert_text((72, y), line, fontsize=10)
            y, line = y + 13, word
        else:
            line = trial
    page.insert_text((72, y), line, fontsize=10)
    # a note set in Word's italic: the regular face under a slanted matrix
    page.insert_text((72, 400), "Income Tax Act", fontsize=10)
    doc.save(path)
    doc.close()
    if spoil != "italic":
        doc = fitz.open(path)
        page = doc[0]
        xref = page.get_contents()[-1]
        s = doc.xref_stream(xref).decode("latin-1")
        h = page.rect.height
        old = f"1 0 0 1 72 {h - 400:g} Tm"
        assert old in s, "the slanted note's matrix was not where expected"
        doc.update_stream(xref, s.replace(old, f"1 0 .33 1 72 {h - 400:g} Tm").encode("latin-1"))
        doc.saveIncr()
        doc.close()


CASES = [
    (None, set(), "an untouched copy"),
    ("bold", {"STYLE_CHANGED"}, "a run-in bold heading set regular"),
    ("italic", {"STYLE_CHANGED"}, "Word's slanted italic set upright"),
    ("underline", {"STYLE_CHANGED"}, "an underline dropped"),
    ("colour", {"COLOUR_CHANGED"}, "a blue link set black"),
    ("fill", {"FILL_CHANGED", "RULES_CHANGED"}, "a shaded table head left white"),
    ("field", {"FIELD_MOVED"}, "a tick box walked to the end of its line"),
    ("break", {"NEW_LINE_BREAK"}, "prose broken with room to spare"),
    ("alignment", {"ALIGNMENT_BROKEN"}, "stacked labels no longer lined up"),
    ("centring", {"CENTRING_LOST"}, "a centred heading set flush left"),
]

bad = 0
before = os.path.join(here, "looks_in.pdf")
make(before)
for spoil, want, what in CASES:
    after = os.path.join(here, f"looks_{spoil or 'same'}.pdf")
    make(after, spoil)
    with fitz.open(before) as a, fitz.open(after) as b:
        got = looks.compare(a, b)
    codes = {issue for _, issue, _ in got}
    ok = codes == want
    bad += not ok
    shown = "; ".join(f"{i}: {d}" for _, i, d in got) or "nothing reported"
    print(f"  {'ok  ' if ok else 'FAIL'} {what} -> {shown}")
print("all caught" if not bad else f"{bad} case(s) wrong")
sys.exit(1 if bad else 0)
