#!/usr/bin/env python3
"""Proofs for forms whose printed page changed after the restyle.

    python final_proofs.py

The restyle's proofs (final selection proofs/) show each original beside its
restyled page. A few forms' printed pages changed later, in field naming:
author notes erased, sample values that sat under fields taken off, a
run-on caption moved back and its empty page dropped. For those - every form
whose mapping erases or moves printed text - the proof is redone from the
original and the named form as it now prints, captioned with what changed
after the restyle, and replaces the old one in final selection proofs/ and
forms for review/proofs/, where make_handover.py takes it from.
"""
import json
import os
import sys

import fitz

import restyle_pdf as R

HERE = os.path.dirname(os.path.abspath(__file__))
NAMING = os.path.join(HERE, "field naming")
ORIGINALS = os.path.join(HERE, "final selection input")
PROOF_DIRS = [os.path.join(HERE, "final selection proofs"), os.path.join(HERE, "forms for review", "proofs")]
DPI = 110


def changed_after(m):
    said = []
    if m.get("erase"):
        said.append("notes erased: " + ", ".join(f"'{t}'" for t in m["erase"]))
    if m.get("erase_under_fields"):
        said.append("sample values that printed under fields taken off")
    for mv in m.get("move_text", []):
        said.append(f"'{mv['text']}' moved from page {mv['from_page']} to page {mv['to_page']}")
    if m.get("drop_pages"):
        said.append(f"empty page(s) {', '.join(map(str, m['drop_pages']))} dropped")
    return said


def blank_like(pix):
    doc = fitz.open()
    page = doc.new_page(width=pix.width * 72 / DPI, height=pix.height * 72 / DPI)
    return page.get_pixmap(dpi=DPI)


def main():
    made = []
    for form in sorted(os.listdir(NAMING)):
        mp = os.path.join(NAMING, form, "mapping.json")
        named = os.path.join(NAMING, form, f"{form}.pdf")
        if not (os.path.exists(mp) and os.path.exists(named)):
            continue
        m = json.load(open(mp, encoding="utf-8"))
        said = changed_after(m)
        if not said:
            continue
        ctx = json.load(open(os.path.join(NAMING, form, "context.json"), encoding="utf-8"))
        original = fitz.open(os.path.join(ORIGINALS, ctx["form"]))
        final = fitz.open(named)
        pages = []
        for i in range(max(original.page_count, final.page_count)):
            a = original[i].get_pixmap(dpi=DPI) if i < original.page_count else None
            # fields hidden: the page as it prints
            b = final[i].get_pixmap(dpi=DPI, annots=False) if i < final.page_count else None
            a = a or blank_like(b)
            b = b or blank_like(a)
            pages.append(R.save_proof(
                a, b, None,
                f"{ctx['form']}   page {i + 1}   (original left, final right)   "
                f"changed after the restyle - see below", dpi=DPI,
                legend=said if i == 0 else ()))
        stem = os.path.splitext(ctx["form"])[0]
        for d in PROOF_DIRS:
            if not os.path.isdir(d):
                continue
            for old in os.listdir(d):
                if old.startswith(stem + " [") or old.startswith(stem + "  ["):
                    os.remove(os.path.join(d, old))
            R.join_down(pages, os.path.join(d, f"{stem} [final 10pt].png"))
        made.append((form, said))
    # forms taken as is ("dont restructure"): their copy beside the final, and
    # the restyled copy's proof gone, since the form no longer comes from it
    as_is = os.path.join(HERE, "field naming as is")
    sup = json.load(open(os.path.join(as_is, "supersedes.json"), encoding="utf-8")) \
        if os.path.exists(os.path.join(as_is, "supersedes.json")) else {}
    for form, old in sup.items():
        ctx_p = os.path.join(as_is, form, "context.json")
        named = os.path.join(as_is, form, f"{form}.pdf")
        if not (os.path.exists(ctx_p) and os.path.exists(named)):
            continue
        source = fitz.open(json.load(open(ctx_p, encoding="utf-8"))["source"])
        final = fitz.open(named)
        pages = [R.save_proof(source[i].get_pixmap(dpi=DPI), final[i].get_pixmap(dpi=DPI, annots=False), None,
                              f"{form}.pdf   page {i + 1}   (as given left, final right)   taken as is", dpi=DPI,
                              legend=["taken as is (dont restructure): nothing on the page moved or restyled - "
                                      "fields named and coded only"] if i == 0 else ())
                 for i in range(final.page_count)]
        stems = {form} | ({os.path.splitext(json.load(open(os.path.join(NAMING, old, "context.json"),
                                                         encoding="utf-8"))["form"])[0]} if old else set())
        for d in PROOF_DIRS:
            if not os.path.isdir(d):
                continue
            for f in os.listdir(d):
                if any(f.startswith(s + " [") or f.startswith(s + "  [") for s in stems):
                    os.remove(os.path.join(d, f))
            R.join_down(pages, os.path.join(d, f"{form} [as is].png"))
        made.append((form, ["taken as is"]))
    for form, said in made:
        print(f"{form}: {'; '.join(said)}")
    print(f"{len(made)} proof(s) redone")
    return 0


if __name__ == "__main__":
    sys.exit(main())
