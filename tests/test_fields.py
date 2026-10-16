"""Radio buttons become pick-one check boxes, the field list stays whole -
and the checks that say so still speak.

    python tests/test_fields.py restyle_pdf.py

Builds a form with a radio group and two plain boxes, with its AcroForm
written into the catalog (as 45 of the 76 final forms have it - the
field-list check once passed every one of those without looking), and asks:
  - the group comes out one check box field over its two boxes, each box
    exporting the option it had, and both readers list it once
  - fields_expected, worked out from the original, agrees with the result
  - sabotage: a box gone is a box the comparison misses
  - sabotage: a field taken off the list is put back, and counted
"""
import importlib.util
import os
import sys
from collections import Counter

import pymupdf as fitz
import pypdf

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("r", sys.argv[1])
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)
ok = True


def check(what, cond):
    global ok
    print(("  ok   " if cond else "  FAIL ") + what)
    ok = ok and cond


def build(path):
    doc = fitz.open()
    page = doc.new_page()
    xs = []
    for i, name in enumerate(["G", "G", "Plain", "Other"]):
        w = fitz.Widget()
        w.field_type = fitz.PDF_WIDGET_TYPE_CHECKBOX
        w.rect = fitz.Rect(72 + 40 * i, 100, 84 + 40 * i, 112)
        w.field_name = name
        xs.append(page.add_widget(w).xref)
    # the first two made one radio group by hand: a parent with the radio
    # flags, each box its own on-state, A and B
    px = doc.get_new_xref()
    doc.update_object(px, f"<</T (G) /FT /Btn /Ff {(1 << 15) | (1 << 14)} /V /Off "
                          f"/Kids [{xs[0]} 0 R {xs[1]} 0 R]>>")
    for x, on in zip(xs[:2], "AB"):
        kept = [ln for ln in doc.xref_object(x).splitlines() if not ln.lstrip().startswith(("/T ", "/FT", "/Ff", "/V "))]
        doc.update_object(x, "\n".join(kept))
        doc.xref_set_key(x, "Parent", f"{px} 0 R")
        doc.xref_set_key(x, "AP/N", doc.xref_get_key(x, "AP/N")[1].replace("/Yes ", f"/{on} "))
    # the AcroForm written into the catalog rather than as an object of its
    # own (PyMuPDF writes a new form's that way), listing the group, not its boxes
    cat = doc.pdf_catalog()
    kind, body = doc.xref_get_key(cat, "AcroForm")
    if kind == "xref":
        body = doc.xref_object(int(body.split()[0]), compressed=True)
    body = body.replace(f"{xs[0]} 0 R {xs[1]} 0 R", f"{px} 0 R")
    doc.xref_set_key(cat, "AcroForm", body)
    doc.save(path)


src = os.path.join(HERE, "fields_in.pdf")
out = os.path.join(HERE, "fields_out.pdf")
build(src)
orig = fitz.open(src)
check("the test form has a radio group", any(w.field_type == fitz.PDF_WIDGET_TYPE_RADIOBUTTON
                                             for p in orig for w in p.widgets()))
check("and its AcroForm is in the catalog", orig.xref_get_key(orig.pdf_catalog(), "AcroForm")[0] == "dict")

doc = fitz.open(src)
said = R.radios_to_checkboxes(doc)
unlisted = R.list_all_fields(doc)
doc.save(out)
print("  ", said, "| unlisted put back:", unlisted)
res = fitz.open(out)
res_pages = list(res)               # a widget needs its page alive to read its states
ws = [w for p in res_pages for w in p.widgets()]
g = [w for w in ws if w.field_name == "G"]
check("no radio button left", not any(w.field_type == fitz.PDF_WIDGET_TYPE_RADIOBUTTON for w in ws))
check("G is check boxes, two of them", len(g) == 2 and all(w.field_type == fitz.PDF_WIDGET_TYPE_CHECKBOX for w in g))
check("each box exports the option it had", sorted(w.on_state() for w in g) == ["A", "B"])
names = set(pypdf.PdfReader(out).get_fields())
check("pypdf lists G once, as a field", "G" in names and {"Plain", "Other"} <= names)
got = Counter((w.field_name, w.field_type_string) for p in res for w in p.widgets())
check("fields_expected agrees with the result", R.fields_expected(orig) == got)

# sabotage: a box lost in the conversion
sab = fitz.open(out)
page = sab[0]
page.delete_widget([w for w in page.widgets() if w.field_name == "G"][0])
lost = R.fields_expected(orig) - Counter((w.field_name, w.field_type_string) for p in sab for w in p.widgets())
check("sabotage: a lost box shows as missing", lost == Counter({("G", "CheckBox"): 1}))

# sabotage: a field off the list, on a catalog AcroForm
sab = fitz.open(out)
listed, write = R.field_list(sab)
plain = [w.xref for p in sab for w in p.widgets() if w.field_name == "Plain"][0]
write([x for x in listed if x != plain])
check("sabotage: a field off the list is put back and counted", R.list_all_fields(sab) == 1
      and plain in R.field_list(sab)[0])

print("all field checks behave" if ok else "FAILED")
sys.exit(0 if ok else 1)
