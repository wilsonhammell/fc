"""Check that moving a form's furniture puts every shape back.

Pushes everything below a line down the page, for several lines and every
form given, and checks the line-art census finds the same shapes afterwards.

    python tests/test_push.py restyle_pdf.py "input forms"
"""
import sys, os, importlib.util
import pymupdf as fitz

spec = importlib.util.spec_from_file_location("r", sys.argv[1])
r = importlib.util.module_from_spec(spec); spec.loader.exec_module(r)
folder = sys.argv[2] if len(sys.argv) > 2 else "input forms"
here = os.path.dirname(os.path.abspath(__file__))
tmp = os.path.join(here, "out_push.pdf")

bad = done = refused = shapes = 0
for name in sorted(os.listdir(folder)):
    if not name.lower().endswith(".pdf"):
        continue
    src = fitz.open(os.path.join(folder, name))
    for y in (250, 350, 450, 550, 650):
        doc = fitz.open()
        doc.insert_pdf(src, from_page=0, to_page=0)
        moved = r.push_down(doc[0], y, 12)
        if moved < 0:                      # a path lies across the line
            refused += 1
            doc.close()
            continue
        doc.save(tmp)
        got = r.verify_line_art(src[0], fitz.open(tmp)[0])
        done += 1
        shapes += moved
        if got:
            bad += 1
            print(f"  FAIL {name[:40]} at y={y}: {got[0][0]} {got[0][1][:50]}")
        doc.close()
    src.close()
print(f"  {done} push(es), {shapes} shapes moved, {refused} refused")
print("FAILURES:", bad) if bad else print("every shape came back, the same shape")
sys.exit(1 if bad else 0)
