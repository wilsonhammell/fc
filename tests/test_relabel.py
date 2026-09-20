"""Relabel + census: only whole labels change; body text never does, and if
it did the census would say so."""
import sys, os, argparse, importlib.util
import pymupdf as fitz
HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("r", sys.argv[1]); r = importlib.util.module_from_spec(spec); spec.loader.exec_module(r)

src = os.path.join(HERE, "relabel_in.pdf")
d = fitz.open(); p = d.new_page()
p.insert_text((72, 100), "The Borrower signs below in front of a witness who is not a family member.", fontname="tiro", fontsize=10)
p.draw_line((72, 200), (250, 200)); p.draw_line((320, 200), (540, 200))
p.insert_text((72, 212), "Witness", fontname="tiro", fontsize=10)
p.insert_text((320, 212), "Signature of Producer", fontname="tiro", fontsize=10)
p.insert_text((72, 260), "Name of Lender:", fontname="tiro", fontsize=10)
d.save(src)

ns = argparse.Namespace(size=10.0, best_fit=True, min_size=6.0, right_margin=None, quantise=0.5,
    dry_run=False, png=None, png_dpi=110, pad=0.5, centre_tol=12.0, min_indent=24.0,
    width_tolerance=1.0, skip_conforming=False, target_font="Arial", size_tolerance=0.2,
    gap_tolerance=3.0, no_recover=False, reflow=True, min_leading=1.1, page_margin=None,
    min_fill=0.5, fine_size=8.0, fine_floor=5.0, header=False, logo_size=63.0, logo_top=18.0, title_size=14.0,
    title_width=220.0, header_gap=8.0, min_bottom_margin=18.0, old_header_zone=100.0,
    old_title_min_size=12.0)
fonts = r.resolve_fonts()
table = r.load_relabels(sys.argv[2])
out = os.path.join(HERE, "relabel_out.pdf")

def run(label):
    rows, size = r.process_file(src, out, fonts, ns, None, table)
    text = fitz.open(out)[0].get_text()
    print(f"{label}:")
    for x in rows:
        if x[2] in ("relabelled", "relabel_for_human", "relabel_review", "TEXT_LOST", "TEXT_GAINED"):
            print(f"    {x[2]}: {x[3][:90]}")
    print(f"    body sentence intact: {'in front of a witness who is not a family member' in text}")

run("clean relabel")
real = fitz.Page.insert_text
def reword_body_too(self, point, text, *a, **kw):
    return real(self, point, text.replace("a witness", "an Authorizing Staff Member"), *a, **kw)
fitz.Page.insert_text = reword_body_too
try:
    run("sabotage: the body sentence gets reworded as well")
finally:
    fitz.Page.insert_text = real
