"""Sabotage tests for the line-art check: take a rule away, add a box that
was never there, and check both are reported.

    python tests/test_line_art.py restyle_pdf.py [a form.pdf]
"""
import sys, os, importlib.util
import pymupdf as fitz

spec = importlib.util.spec_from_file_location("r", sys.argv[1])
r = importlib.util.module_from_spec(spec); spec.loader.exec_module(r)
src_path = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
    "input forms", "RRIF Designation of Successr Annuitant.pdf")
here = os.path.dirname(os.path.abspath(__file__))
src = fitz.open(src_path)

def saved(page, name):
    out = os.path.join(here, name)
    page.parent.save(out)
    return fitz.open(out)[0]

bad = 0
doc = fitz.open(); doc.insert_pdf(src, from_page=0, to_page=0)
page = doc[0]
seg = max((s for s in r.rule_segments(r.printed(page)) if s.width > 100),
          key=lambda s: s.width)
page.add_redact_annot(seg + (-1, -1, 1, 1))
page.apply_redactions(graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED)
got = r.verify_line_art(src[0], saved(page, "out_art_lost.pdf"))
ok = any(i[0] == "LINE_ART_LOST" for i in got)
bad += not ok
print(f"  {'ok  ' if ok else 'FAIL'} a ruled line rubbed out -> {got or 'nothing reported'}")

doc2 = fitz.open(); doc2.insert_pdf(src, from_page=0, to_page=0)
page2 = doc2[0]
page2.draw_rect(fitz.Rect(70, 700, 300, 720), color=(0, 0, 0), width=0.8)
got = r.verify_line_art(src[0], saved(page2, "out_art_gained.pdf"))
ok = any(i[0] == "LINE_ART_GAINED" for i in got)
bad += not ok
print(f"  {'ok  ' if ok else 'FAIL'} a box drawn that wasn't there -> {got or 'nothing reported'}")

print("FAILURES:", bad) if bad else print("the line-art check reports both")
sys.exit(1 if bad else 0)
