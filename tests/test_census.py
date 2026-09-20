"""Sabotage tests for the glyph census: break the restyle on purpose and check
the census reports it. Runs the ORIGINAL and the FIXED script side by side."""
import sys, os, argparse, importlib.util
import pymupdf as fitz

HERE = os.path.dirname(os.path.abspath(__file__))
ORIG, NEW = sys.argv[1], sys.argv[2]

def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def make_form(path, ocr=False, widget=False):
    doc = fitz.open()
    for pg in range(2):                       # two pages: census must line up per page
        p = doc.new_page()
        y = 80
        for line in [f"APPLICATION FOR SERVICES  page {pg+1}",
                     "Applicant Name:   Jane Q. Public",
                     "Mailing Address:  1234 Some Street, Some Town",
                     "Pre-authorized debit - see e-mail agreement; also clause 4",
                     "Total amount owing in full on or before the due date",
                     "I certify that the information above is true and complete."]:
            p.insert_text((72, y), line, fontname="tiro", fontsize=12,
                          render_mode=3 if ocr else 0)
            y += 28
        if widget and pg == 0:
            w = fitz.Widget(); w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
            w.field_name = "acct"; w.field_value = "PREFILLED 12345"
            w.rect = fitz.Rect(72, 260, 300, 280); w.text_font = "Helv"; w.text_fontsize = 11
            p.add_widget(w)
    doc.save(path)

real_insert = fitz.Page.insert_text
def sabotage(mode):
    def patched(self, point, text, *a, **kw):
        if mode == "skip_short" and text.startswith("Applicant"):
            return 0                                             # 26 glyphs, fewer than the spaces redrawn
        if mode == "skip" and text.startswith("Total"):
            return 0                                             # erased, never redrawn
        if mode == "dup" and text.startswith("Applicant"):
            real_insert(self, point, text, *a, **kw)             # drawn twice
        if mode == "swap" and text.startswith("Total"):
            text = text.replace("o", "0")                        # same count, wrong text
        return real_insert(self, point, text, *a, **kw)
    fitz.Page.insert_text = patched

ARGS = dict(size=10.0, best_fit=False, min_size=6.0, right_margin=None, quantise=0.5,
            dry_run=False, png=None, png_dpi=110, pad=0.5, centre_tol=12.0,
            min_indent=24.0, width_tolerance=1.0, skip_conforming=False,
            target_font="Arial", size_tolerance=0.2, gap_tolerance=3.0, no_recover=False,
            reflow=False, min_leading=1.1, page_margin=None, min_fill=0.5, fine_size=8.0, fine_floor=5.0, header=False, logo_size=63.0, logo_top=18.0, title_size=14.0, title_width=220.0,
            header_gap=8.0, min_bottom_margin=18.0, old_header_zone=100.0, old_title_min_size=12.0)

cases = [("clean run (no sabotage)",        dict(),              None,   "nothing"),
         ("a line erased, never redrawn",   dict(),              "skip", "TEXT_LOST"),
         ("a short line erased (26 glyphs)", dict(),             "skip_short", "TEXT_LOST"),
         ("a line drawn twice",             dict(),              "dup",  "TEXT_GAINED"),
         ("'o' swapped for '0' (same count)", dict(),            "swap", "TEXT_LOST+TEXT_GAINED"),
         ("invisible OCR layer made visible", dict(ocr=True),    None,   "TEXT_GAINED"),
         ("pre-filled field (left alone)",  dict(widget=True),   None,   "nothing")]

details = []
mods = {"original": load(ORIG, "orig"), "fixed": load(NEW, "fixed")}
print(f"{'case':36} {'should report':22} {'original script':28} fixed script")
for label, form_kw, mode, want in cases:
    src = os.path.join(HERE, "in.pdf"); make_form(src, **form_kw)
    got = {}
    for name, m in mods.items():
        sabotage(mode)
        try:
            fonts = m.resolve_fonts()
            rows, _ = m.process_file(src, os.path.join(HERE, f"out_{name}.pdf"), fonts,
                                     argparse.Namespace(**ARGS))
        finally:
            fitz.Page.insert_text = real_insert
        hits = sorted({f"{r[2]}@p{r[1]}" for r in rows if r[2] in ("TEXT_LOST", "TEXT_GAINED")})
        got[name] = ", ".join(hits) or "nothing"
    print(f"{label:36} {want:22} {got['original']:28} {got['fixed']}")
    details.extend(f"    {label}: {r[2]} p{r[1]}: {r[3]}" for r in rows
                   if r[2] in ("TEXT_LOST", "TEXT_GAINED") and r[1] == 1)
print()
print("fixed script, page 1 messages:")
for d in details: print(d)
