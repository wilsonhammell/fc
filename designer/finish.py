"""Put Tab back in reading order on the designer's output.

    python designer/finish.py

Run after build.mjs, before make_handover.py. The named forms go into the
designer with their fields in reading order (apply_fields.tab_in_reading_order),
but its export moves some fields - most of the DB ones - to the end of their
page's list, so Tab jumped from the bottom of ATM & POS Trace back up to the
member's name. Each page of designer/out/*/*.pdf is put back in reading
order, and saved incrementally: only the pages' field lists change, and the
designer's requirements, scripts and attachment stay byte for byte as it
wrote them.
"""
import glob
import json
import os
import sys

import fitz

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import apply_fields  # noqa: E402

OUT = os.path.join(HERE, "out")
# a batch's own output: python designer/finish.py designer/out_<batch>
if len(sys.argv) > 1:
    OUT = os.path.abspath(sys.argv[1])
# the designer's system fields keep the order it wrote them in - that order
# is also how they are drawn, and DNA_REQ comes last so it covers the form
SYSTEM = {e["identifier"] for e in json.load(open(
    os.path.join(os.path.dirname(os.path.dirname(HERE)), "form_requirements", "public", "systemFields.json"),
    encoding="utf-8"))}


def main():
    pdfs = sorted(glob.glob(os.path.join(OUT, "*", "*.pdf")))
    for path in pdfs:
        doc = fitz.open(path)
        before = [[a[0] for a in page.annot_xrefs()] for page in doc]
        apply_fields.tab_in_reading_order(doc, keep_order=SYSTEM)
        after = [[a[0] for a in page.annot_xrefs()] for page in doc]
        moved = sum(a != b for a, b in zip(before, after))
        doc.saveIncr()
        doc.close()
        print(f"   {os.path.basename(path)}: {moved} page(s) put back in reading order")
    print(f"{len(pdfs)} designer output(s) finished")
    return 0 if pdfs else 1


if __name__ == "__main__":
    sys.exit(main())
