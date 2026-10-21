"""The finish every named form gets - fields stretched along their lines, Tab
in reading order, opening at 100%, unused default fonts dropped - does what
it says and no more.

    python tests/test_finish.py apply_fields.py

  - a field on a line drawn in two pieces takes the whole line, from the
    caption to its end (ATM & POS Trace's Name)
  - two fields on one line stop at the caption between them; a field in a
    cell stops at the cell's wall
  - a field on a border across the page, and a tall box, stay as they are
  - fields made bottom-up tab top-down, and the page says /Tabs /W
  - the form opens at 100%, but an opening script is left alone
  - a default font no field uses goes, one a field's DA names stays - with
    the AcroForm in the catalog and as an object of its own
  - a length limit on a name placed twice ends up on the field, not the
    widgets
"""
import importlib.util
import os
import sys

import pymupdf as fitz
import pypdf

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.abspath(sys.argv[1])))
spec = importlib.util.spec_from_file_location("A", sys.argv[1])
A = importlib.util.module_from_spec(spec)
spec.loader.exec_module(A)
ok = True


def check(what, cond):
    global ok
    print(("  ok   " if cond else "  FAIL ") + what)
    ok = ok and cond


def field(page, name, rect):
    w = fitz.Widget()
    w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    w.rect = fitz.Rect(rect)
    w.field_name = name
    w.text_font, w.text_fontsize = "Helv", 10
    page.add_widget(w)


def rect_of(doc, name):
    return next(fitz.Rect(w.rect) for p in doc for w in p.widgets() if w.field_name == name)


# -- stretching along lines
doc = fitz.open()
page = doc.new_page()
page.insert_text((72, 100), "Name:", fontsize=10)
page.draw_line((104, 102), (150, 102), width=0.5)       # the line in two pieces,
page.draw_line((150, 102), (500, 102), width=0.5)       # broken where the field began
field(page, "Name", (150, 88, 300, 102))
page.insert_text((72, 140), "Date:", fontsize=10)
page.draw_line((102, 142), (500, 142), width=0.5)
page.insert_text((300, 140), "Time:", fontsize=10)
field(page, "Date", (130, 128, 180, 142))
field(page, "Time", (340, 128, 380, 142))
page.draw_rect((72, 160, 260, 180), width=0.5)          # a cell, its right wall at 260
page.draw_line((72, 180), (400, 180), width=0.5)        # its bottom carried on past it
field(page, "Cell", (80, 166, 150, 180))
page.draw_line((30, 222), (582, 222), width=0.5)        # a border across the page
field(page, "OnBorder", (100, 208, 200, 222))
page.draw_line((72, 300), (500, 300), width=0.5)
field(page, "Tall", (100, 250, 200, 300))
grown = A.fit_to_lines(doc)
r = rect_of(doc, "Name")
check(f"a line in two pieces is one: Name runs {r.x0:.0f}-{r.x1:.0f}, from the caption to the end",
      r.x0 <= 106 and r.x1 >= 499)
d, t = rect_of(doc, "Date"), rect_of(doc, "Time")
check(f"fields either side of a caption stop at it: Date to {d.x1:.0f}, Time from {t.x0:.0f}",
      d.x1 < 300 and d.x0 <= 104 and t.x0 > 320 and t.x1 >= 499)
c = rect_of(doc, "Cell")
check(f"a field in a cell stops at its wall: to {c.x1:.0f}", 200 < c.x1 <= 260)
check("a border across the page stretches nothing", rect_of(doc, "OnBorder") == fitz.Rect(100, 208, 200, 222))
check("a tall box is no line field", rect_of(doc, "Tall") == fitz.Rect(100, 250, 200, 300))
check(f"{grown} grown, the four that should be", grown == 4)

# -- Tab, made bottom-up
doc = fitz.open()
page = doc.new_page()
for name, y in [("Third", 300), ("Second", 200), ("FirstRight", 100), ("FirstLeft", 100)]:
    field(page, name, (72 if name != "FirstRight" else 300, y, 200 if name != "FirstRight" else 400, y + 14))
A.tab_in_reading_order(doc)
got = [w.field_name for w in doc[0].widgets()]
check(f"Tab goes as the page reads: {got}", got == ["FirstLeft", "FirstRight", "Second", "Third"])
check("and the page tabs in that order", doc.xref_get_key(doc[0].xref, "Tabs")[1] == "/W")

# -- the designer's system fields keep their order, after the form's: DNA_REQ
# is drawn last so it covers the rest, and sorted with the others it went under
doc = fitz.open()
page = doc.new_page()
field(page, "DNA_REQ", (20, 20, 590, 770))      # the whole page, made first
field(page, "Below", (72, 300, 200, 314))
field(page, "Above", (72, 100, 200, 114))
field(page, "VALID_ID", (0, 0, 10, 10))
A.tab_in_reading_order(doc, keep_order={"DNA_REQ", "VALID_ID"})
got = [w.field_name for w in doc[0].widgets()]
check(f"system fields keep their order, after the form's: {got}", got == ["Above", "Below", "DNA_REQ", "VALID_ID"])

# -- opening view
check("opens at 100%", A.open_at_full_size(doc)
      and doc.xref_get_key(doc.pdf_catalog(), "OpenAction")[1].endswith("/XYZ null null 1]"))
doc.xref_set_key(doc.pdf_catalog(), "OpenAction", "<</S/JavaScript/JS(app.alert(1))>>")
check("an opening script is left alone", not A.open_at_full_size(doc)
      and "/JavaScript" in doc.xref_get_key(doc.pdf_catalog(), "OpenAction")[1])


# -- default fonts, AcroForm in the catalog and as an object of its own
def with_fonts(indirect):
    doc = fitz.open()
    page = doc.new_page()
    field(page, "A", (72, 72, 200, 86))
    field(page, "B", (72, 100, 200, 114))
    used, unused = doc.get_new_xref(), doc.get_new_xref()
    doc.update_object(used, "<</Type/Font/Subtype/Type1/BaseFont/Courier>>")
    doc.update_object(unused, "<</Type/Font/Subtype/Type1/BaseFont/Symbol>>")
    b = next(w for w in page.widgets() if w.field_name == "B")
    doc.xref_set_key(b.xref, "DA", "(/Cour 10 Tf 0 g)")
    cat = doc.pdf_catalog()
    kind, body = doc.xref_get_key(cat, "AcroForm")
    if kind == "xref":
        body = doc.xref_object(int(body.split()[0]), compressed=True)
    fonts = f"/DR<</Font<</Helv 0 0 R/Cour {used} 0 R/Symb {unused} 0 R>>>>"
    body = body.replace("/DR", "/OldDR") if "/DR" in body else body
    body = body[:-2] + fonts + ">>"
    # (Helv needs a real object for the pattern to read)
    helv = doc.get_new_xref()
    doc.update_object(helv, "<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>")
    body = body.replace("/Helv 0 0 R", f"/Helv {helv} 0 R")
    if indirect:
        ax = doc.get_new_xref()
        doc.update_object(ax, body)
        doc.xref_set_key(cat, "AcroForm", f"{ax} 0 R")
    else:
        doc.xref_set_key(cat, "AcroForm", body)
    return doc


for indirect in (False, True):
    doc = with_fonts(indirect)
    gone = A.prune_default_fonts(doc)
    left = doc.xref_get_key(doc.pdf_catalog(), "AcroForm/DR/Font")[1]
    where = "an object of its own" if indirect else "in the catalog"
    check(f"AcroForm {where}: Symb dropped, Helv and Cour kept ({left})",
          gone == ["Symb"] and "/Helv" in left and "/Cour" in left and "/Symb" not in left)
    path = os.path.join(HERE, "finish_out.pdf")
    doc.save(path)
    check(f"AcroForm {where}: pypdf still reads both fields",
          set((pypdf.PdfReader(path).get_fields() or {}).keys()) == {"A", "B"})

# -- Helv and ZaDb listed where the fields' text names them and /DR doesn't
def font_list(doc):
    kind, val = doc.xref_get_key(doc.pdf_catalog(), "AcroForm/DR/Font")
    if kind == "xref":
        val = doc.xref_object(int(val.split()[0]), compressed=True)
    return val


doc = fitz.open()
page = doc.new_page()
field(page, "A", (72, 72, 200, 86))
cat = doc.pdf_catalog()
kind, body = doc.xref_get_key(cat, "AcroForm")
if kind == "xref":
    doc.update_object(int(body.split()[0]), "<</Fields[" + " ".join(
        f"{w.xref} 0 R" for w in page.widgets()) + "]>>")
else:
    doc.xref_set_key(cat, "AcroForm", "<</Fields[" + " ".join(f"{w.xref} 0 R" for w in page.widgets()) + "]>>")
added = A.add_default_fonts(doc)
check(f"a form with no /DR gets Helv and ZaDb ({font_list(doc)})",
      added == ["Helv", "ZaDb"] and "/Helv" in font_list(doc) and "/ZaDb" in font_list(doc))
check("and a second pass adds nothing", A.add_default_fonts(doc) == [])

for indirect in (False, True):
    doc = with_fonts(indirect)
    added = A.add_default_fonts(doc)
    where = "an object of its own" if indirect else "in the catalog"
    check(f"AcroForm {where}, Helv already listed: only ZaDb added, Cour kept ({font_list(doc)})",
          added == ["ZaDb"] and "/Cour" in font_list(doc) and "/ZaDb" in font_list(doc))

# -- a length limit on a repeated name
doc = fitz.open()
page = doc.new_page()
field(page, "Acct", (72, 72, 200, 86))
field(page, "Acct", (72, 300, 200, 314))
A.set_lengths(doc, {"Acct": 12})
path = os.path.join(HERE, "finish_out.pdf")
doc.save(path)
A.merge_repeats(path)
doc = fitz.open(path)
kids = [w.xref for w in doc[0].widgets()]
parent = int(doc.xref_get_key(kids[0], "Parent")[1].split()[0])
check("a repeated name's length limit is on the field",
      doc.xref_get_key(parent, "MaxLen")[1] == "12"
      and all(doc.xref_get_key(k, "MaxLen")[0] == "null" for k in kids))
doc.close()
os.remove(path)

print("the finish behaves" if ok else "THE FINISH IS WRONG")
sys.exit(0 if ok else 1)
