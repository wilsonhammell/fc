#!/usr/bin/env python3
"""Name a form's fields, and place the ones it lacks, from its mapping.

    python apply_fields.py "field naming/RRSP Application" [...]

The second half of field naming. field_context.py numbered every field and
every blank on the form; mapping.json, written from that by hand, says what
each one is:

    {"items": {"F3": {"name": "PRIMARY_MEMBER_NUMBER", "kind": "db",
                      "label": "Member Number", "desc": "the member's number"},
               "B30": {"name": "POS_TRACER_NUMBER", "kind": "entry", ...}},
     "drop": ["F11"],
     "skip": {"B1": "the spouse account box, moved here as F18"},
     "add": [{"page": 0, "rect": [186, 328, 237, 339], "name": "LOAN_BALANCE",
              "kind": "candidate", "label": "...", "desc": "...", "type": "text"}],
     "notes": ["what a person has to settle that no field can"]}

"add" places a field where nothing was found to put one - a value the form
prints as sample text, a box the layout never drew.

kind is one of
    db         a name in dbFields.json - the filler loads it
    special    the designer's signer and joint fields (Member1Name, JointMembers)
    candidate  a value the DB probably holds that dbFields.json does not list yet
    entry      typed by the member or staff; nothing loads it

A field already on the form is renamed (and rebuilt where its type has to
change - a signer name is a dropdown); a blank gets a new field of the type
asked for, Arial (Helvetica) 10 like the designer's default. Writes, beside
the mapping:

    <form>.pdf       the form with its fields named
    fields.csv       one row per field: page, name, kind, the label beside it,
                     what goes there
    named pN.png     each page with each field's name over it

and refuses nothing silently: an item the mapping leaves out, a "db" name not
in the list, a plain name that IS in the list, are all said, and in fields.csv.
"""
import argparse
import csv
import json
import os
import re
import sys

import fitz

HERE = os.path.dirname(os.path.abspath(__file__))
DB = {d["identifier"] for d in json.load(open(os.path.join(HERE, "dbFields.json"), encoding="utf-8"))}
FORMS = os.path.join(HERE, "final selection at 10pt")

SPECIAL = re.compile(r"^(Member[1-6](Name|Signature|SignDate)|JointMember[1-5](Name|Number)|JointMembers)$")
KINDS = ("db", "special", "candidate", "entry")
# what a field name may be made of: the filler matches it exactly, and the
# designer's export values allow no more
NAME_OK = re.compile(r"^[A-Za-z0-9_]+$")

TYPES = {"text": fitz.PDF_WIDGET_TYPE_TEXT, "checkbox": fitz.PDF_WIDGET_TYPE_CHECKBOX,
         "dropdown": fitz.PDF_WIDGET_TYPE_COMBOBOX}
HAS = {fitz.PDF_WIDGET_TYPE_TEXT: "text", fitz.PDF_WIDGET_TYPE_CHECKBOX: "checkbox",
       fitz.PDF_WIDGET_TYPE_COMBOBOX: "dropdown", fitz.PDF_WIDGET_TYPE_RADIOBUTTON: "radio",
       fitz.PDF_WIDGET_TYPE_SIGNATURE: "signature", fitz.PDF_WIDGET_TYPE_LISTBOX: "listbox",
       fitz.PDF_WIDGET_TYPE_BUTTON: "button"}

COLOURS = {"db": (0.1, 0.55, 0.2), "special": (0.0, 0.5, 0.55),
           "candidate": (0.9, 0.45, 0.0), "entry": (0.35, 0.35, 0.35)}


def type_for(name, asked):
    """What a field has to be: the designer fixes the type of its own names
    (a signer's name is a dropdown it fills at run time, the rest text)."""
    if re.match(r"^Member\d+Name$", name):
        return "dropdown"
    if SPECIAL.match(name):
        return "text"
    return asked


def problems(item_id, m):
    out = []
    name, kind = m.get("name", ""), m.get("kind")
    if kind not in KINDS:
        out.append(f"{item_id}: kind {kind!r} is not one of {', '.join(KINDS)}")
    if not NAME_OK.match(name or ""):
        out.append(f"{item_id}: name {name!r} has characters the filler can't match")
    if kind == "db" and name not in DB:
        out.append(f"{item_id}: {name} is marked db but is not in dbFields.json")
    if kind == "special" and not SPECIAL.match(name):
        out.append(f"{item_id}: {name} is marked special but is no signer or joint name")
    if kind in ("candidate", "entry") and name in DB:
        out.append(f"{item_id}: {name} is marked {kind} but IS in dbFields.json")
    return out


def add_field(page, rect, name, kind_of):
    w = fitz.Widget()
    w.field_type = TYPES[kind_of]
    w.rect = fitz.Rect(rect)
    w.field_name = name
    if kind_of != "checkbox":
        w.text_font, w.text_fontsize = "Helv", 10
    if kind_of == "dropdown":
        # filled from DNA when the form opens; the designer gives it no options
        w.choice_values = [" "]
    w.border_color, w.border_width = (0.6, 0.6, 0.6), 0.5
    page.add_widget(w)


def merge_repeats(path):
    """A name placed more than once becomes ONE field with a widget per
    placement, the shape Acrobat and the designer both use: one value, filled
    once, shown everywhere. Written as placed, each copy was a field of its
    own that merely shared the name, which the PDF rules don't allow. The
    parent carries the name, type and font; each widget keeps its own place."""
    doc = fitz.open(path)
    cat = doc.pdf_catalog()
    groups = {}
    for page in doc:
        for w in page.widgets():
            groups.setdefault(w.field_name, []).append(w.xref)
    repeated = {n: xs for n, xs in groups.items() if len(xs) > 1}
    if not repeated:
        return 0
    fields_ref = doc.xref_get_key(cat, "AcroForm/Fields")
    held = doc.xref_object(int(fields_ref[1].split()[0])) if fields_ref[0] == "xref" else fields_ref[1]
    refs = [int(x) for x in re.findall(r"(\d+) 0 R", held)]
    for name, xrefs in repeated.items():
        first = xrefs[0]
        parent = doc.get_new_xref()
        ft = doc.xref_get_key(first, "FT")[1]
        da = doc.xref_get_key(first, "DA")
        ff = doc.xref_get_key(first, "Ff")
        body = f"<</T({name})/FT{ft}/Kids[{' '.join(f'{x} 0 R' for x in xrefs)}]"
        if da[0] == "string":
            body += f"/DA({da[1]})"
        if ff[0] != "null":
            body += f"/Ff {ff[1]}"
        doc.update_object(parent, body + ">>")
        for x in xrefs:
            # the field's own entries come off the widget altogether - set to
            # null they stay in it as nulls, which pypdf refuses to read
            kept = [ln for ln in doc.xref_object(x).splitlines()
                    if not re.match(r"\s*/(T|FT|DA|Ff|V|Parent)\b", ln)]
            doc.update_object(x, "\n".join(kept))
            doc.xref_set_key(x, "Parent", f"{parent} 0 R")
        refs = [r for r in refs if r not in xrefs] + [parent]
    doc.xref_set_key(cat, "AcroForm/Fields", "[" + " ".join(f"{r} 0 R" for r in refs) + "]")
    doc.save(path + ".tmp", garbage=3, deflate=True)
    doc.close()
    os.replace(path + ".tmp", path)
    return len(repeated)


def apply(folder):
    stem = os.path.basename(os.path.normpath(folder))
    ctx = json.load(open(os.path.join(folder, "context.json"), encoding="utf-8"))
    mapping = json.load(open(os.path.join(folder, "mapping.json"), encoding="utf-8"))
    named, drop = mapping.get("items", {}), set(mapping.get("drop", []))
    # blanks looked at and judged not to be a place for a value, with why
    skip = mapping.get("skip", {})
    extra, notes = mapping.get("add", []), mapping.get("notes", [])
    doc = fitz.open(os.path.join(FORMS, ctx["form"]))
    said, rows = [], []
    by_id = {i["id"]: i for i in ctx["items"]}

    for i in ctx["items"]:
        if i["id"] not in named and i["id"] not in drop and i["id"] not in skip:
            if i["existing"] is not None:
                said.append(f"{i['id']}: field {i['existing']!r} is in no mapping - left as it was")
            else:
                said.append(f"{i['id']}: {i['type']} blank at page {i['page'] + 1} "
                            f"{[round(v) for v in i['rect']]} is in no mapping - no field placed")
    for k in list(named) + list(drop) + list(skip):
        if k not in by_id:
            said.append(f"{k}: in the mapping but not on the form")
    for k, m in named.items():
        said += problems(k, m)
    for n, m in enumerate(extra, 1):
        said += problems(f"add {n}", m)

    # existing fields first, matched to their items by name and rect
    for page in doc:
        for w in list(page.widgets()):
            item = next((i for i in ctx["items"] if i["existing"] == w.field_name
                         and i["page"] == page.number
                         and fitz.Rect(i["rect"]).intersects(w.rect)), None)
            if item is None:
                continue
            if item["id"] in drop:
                page.delete_widget(w)
                continue
            m = named.get(item["id"])
            if not m:
                continue
            want = type_for(m["name"], HAS.get(w.field_type, "text"))
            rect = fitz.Rect(m.get("rect") or w.rect)
            if HAS.get(w.field_type) != want or m.get("rect"):
                page.delete_widget(w)
                add_field(page, rect, m["name"], want)
            else:
                w.field_name = m["name"]
                w.update()
    for k, m in named.items():
        i = by_id.get(k)
        if i is None or i["existing"] is not None:
            continue
        asked = m.get("type") or ("checkbox" if i["type"] == "tick" else "text")
        add_field(doc[i["page"]], m.get("rect") or i["rect"], m["name"], type_for(m["name"], asked))

    for m in extra:
        add_field(doc[m["page"]], m["rect"], m["name"], type_for(m["name"], m.get("type", "text")))

    out_pdf = os.path.join(folder, f"{stem}.pdf")
    doc.save(out_pdf, garbage=3, deflate=True)
    doc.close()
    shared = merge_repeats(out_pdf)

    for k, m in named.items():
        i = by_id.get(k, {})
        rows.append({"id": k, "page": i.get("page", -1) + 1, "name": m["name"], "kind": m["kind"],
                     "label on form": m.get("label", ""), "what goes there": m.get("desc", ""),
                     "was": i.get("existing") or f"(new, on a {i.get('type')})"})
    for n, m in enumerate(extra, 1):
        rows.append({"id": f"A{n}", "page": m["page"] + 1, "name": m["name"], "kind": m["kind"],
                     "label on form": m.get("label", ""), "what goes there": m.get("desc", ""),
                     "was": "(new, placed by hand)"})
    for k, why in skip.items():
        i = by_id.get(k, {})
        rows.append({"id": k, "page": i.get("page", -1) + 1, "name": "", "kind": "not a field",
                     "label on form": "", "what goes there": why,
                     "was": f"(a {i.get('type')} blank)"})
    where = {f"A{n}": m["rect"] for n, m in enumerate(extra, 1)}
    rows.sort(key=lambda r: (r["page"], (by_id.get(r["id"], {}).get("rect")
                                         or where.get(r["id"]) or [0, 0])[1]))
    with open(os.path.join(folder, "fields.csv"), "w", newline="", encoding="utf-8-sig") as fh:
        wr = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["id"])
        wr.writeheader()
        wr.writerows(rows)
        for n in notes:
            wr.writerow({"id": "NOTE", "what goes there": n})
        for s in said:
            wr.writerow({"id": "CHECK", "name": s})

    # the proof: each field's name over it, coloured by kind
    saved = fitz.open(out_pdf)
    kind_of = {m["name"]: m["kind"] for m in list(named.values()) + extra}
    for page in saved:
        marks = [(fitz.Rect(w.rect), w.field_name) for w in page.widgets()]
        for r, n in marks:
            colour = COLOURS.get(kind_of.get(n), (0.8, 0.0, 0.0))
            page.draw_rect(r, color=colour, width=0.9)
            # inside the box where it fits - stacked boxes 13pt apart, as in a
            # letter's address block, put every name over the box above it -
            # and over a tick box, which has no room in it
            if r.height >= 9 and r.width >= 30:
                page.insert_text(fitz.Point(r.x0 + 1.5, r.y0 + 6.5), n, fontsize=5.5, color=colour)
            else:
                page.insert_text(fitz.Point(r.x0 + 1, r.y0 - 1.5), n, fontsize=5.5, color=colour)
        page.get_pixmap(dpi=110, annots=False).save(os.path.join(folder, f"named p{page.number + 1}.png"))

    counts = {k: sum(1 for m in list(named.values()) + extra if m["kind"] == k) for k in KINDS}
    print(f"{stem}: " + ", ".join(f"{n} {k}" for k, n in counts.items()) + f", {len(drop)} dropped"
          + (f"; {shared} name(s) shown in more than one place, written as one field each"
             if shared else ""))
    for n in notes:
        print("   NOTE  " + n)
    for s in said:
        print("   CHECK " + s)
    return rows, said


def gather(root):
    """One list across every form named so far, for the people who build the
    datafeed: each candidate value once, with what it is and every form and
    label it turns up under - and every DB match a person still has to
    confirm. Read back from each form's fields.csv, so it is the same list
    the forms say."""
    cands, checks = {}, []
    for stem in sorted(os.listdir(root)):
        path = os.path.join(root, stem, "fields.csv")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                if r.get("kind") == "candidate":
                    c = cands.setdefault(r["name"], {"desc": r["what goes there"],
                                                     "labels": [], "forms": []})
                    if r["label on form"] and r["label on form"] not in c["labels"]:
                        c["labels"].append(r["label on form"])
                    if stem not in c["forms"]:
                        c["forms"].append(stem)
                elif r.get("kind") == "db" and "CHECK" in r.get("what goes there", ""):
                    checks.append((stem, r["name"], r["label on form"], r["what goes there"]))
                elif r.get("id") == "NOTE":
                    checks.append((stem, "", "", r["what goes there"]))
    with open(os.path.join(root, "DB candidates.csv"), "w", newline="", encoding="utf-8-sig") as fh:
        wr = csv.writer(fh)
        wr.writerow(["proposed name", "what it is", "labels on the forms", "forms"])
        for n, c in sorted(cands.items()):
            wr.writerow([n, c["desc"], " | ".join(c["labels"]), " | ".join(c["forms"])])
    with open(os.path.join(root, "to confirm.csv"), "w", newline="", encoding="utf-8-sig") as fh:
        wr = csv.writer(fh)
        wr.writerow(["form", "field", "label on form", "question"])
        wr.writerows(checks)
    print(f"{len(cands)} candidate value(s) across the forms -> DB candidates.csv; "
          f"{len(checks)} thing(s) to confirm -> to confirm.csv")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folders", nargs="+")
    args = ap.parse_args()
    bad = 0
    for f in args.folders:
        _, said = apply(f)
        bad += len(said)
    gather(os.path.dirname(os.path.normpath(args.folders[0])))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
