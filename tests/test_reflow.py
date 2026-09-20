"""Reflow + census: a Times paragraph too wide for Arial 10pt gets re-wrapped;
the census must stay clean, and must catch a word dropped during the re-wrap."""
import sys, os, argparse, importlib.util
import pymupdf as fitz
HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("r", sys.argv[1]); r = importlib.util.module_from_spec(spec); spec.loader.exec_module(r)

src = os.path.join(HERE, "reflow_in.pdf")
d = fitz.open(); p = d.new_page()
text = ("The Borrower shall cause, and hereby authorizes and directs, the insurer to pay any "
        "proceeds directly to Libro; the Borrower will deliver a proof of loss within thirty days "
        "and will co-operate with the insurer, the adjuster and Libro in every respect.")
tw = fitz.TextWriter(p.rect)
tw.fill_textbox(fitz.Rect(72, 100, 540, 200), text, font=fitz.Font("tiro"), fontsize=10, lineheight=1.9)
tw.write_text(p)
p.insert_text((72, 260), "Signature: ", fontname="tiro", fontsize=10)
d.save(src)

ns = argparse.Namespace(size=10.0, best_fit=True, min_size=6.0, right_margin=None, quantise=0.5,
    dry_run=False, png=None, png_dpi=110, pad=0.5, centre_tol=12.0, min_indent=24.0,
    width_tolerance=1.0, skip_conforming=False, target_font="Arial", size_tolerance=0.2,
    gap_tolerance=3.0, no_recover=False, reflow=True, min_leading=1.1, page_margin=None, min_fill=0.5, fine_size=8.0, fine_floor=5.0, header=False,
    logo_size=63.0, logo_top=18.0, title_size=14.0, title_width=220.0, header_gap=8.0,
    min_bottom_margin=18.0, old_header_zone=100.0, old_title_min_size=12.0)
fonts = r.resolve_fonts()

def run(label):
    rows, size = r.process_file(src, os.path.join(HERE, "reflow_out.pdf"), fonts, ns)
    got = [f"{x[2]}: {x[3][:90]}" for x in rows if x[2] in ("reflowed", "TEXT_LOST", "TEXT_GAINED", "reflow_blocked")]
    print(f"{label}: size {size}pt"); [print("   ", g) for g in got]

run("clean re-wrap")
real = fitz.Page.insert_text
def drop_word(self, point, text, *a, **kw):
    return 0 if text == "adjuster" else real(self, point, text, *a, **kw)
fitz.Page.insert_text = drop_word
try:
    run("re-wrap that drops 'adjuster'")
finally:
    fitz.Page.insert_text = real
