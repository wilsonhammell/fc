"""Gather everything for the final forms into one folder to hand over.

    python make_handover.py [-o handover]

Run after make_review_folder.py, apply_fields.py and the designer build
(designer/requirements.py, designer/build.mjs, then designer/finish.py). The
output folder is
emptied first and filled from:
    field naming/named forms/        the cleaned forms, fields named
    designer/out/<form>/             what the PDF Requirement Designer exports
                                     for each production form: the PDF after
                                     coding and the requirements workbook
    designer/form_matches.json       which row of Combined Form Requirements
                                     each form is - a form with none goes in
                                     "not in the form table", not in 1 and 2
    forms for review/proofs          each form beside its original
    field naming/<form>/             each form's field list and named pages
    field naming/*.csv, *.txt        the lists across all forms
Anything missing is said at the end - nothing is left out without a word.
"""
import argparse
import csv
import json
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NAMING = os.path.join(HERE, "field naming")
REVIEW = os.path.join(HERE, "forms for review")
DESIGNER = os.path.join(HERE, "designer")

README = """\
Libro forms - final selection
=============================

1 cleaned forms\\
    The cleaned forms: Arial 10pt, Libro header, signing areas, every field
    named from the DB list (dbFields.json). This is what goes into the PDF
    Requirement Designer ("Import Form").

2 designer output\\<form>\\
    What the designer's export gives for each production form, made by
    running the designer's own code on the cleaned form:
      <form>.pdf    the PDF after coding - signing buttons, system fields,
                    Config, Load / Save Validation, formats, the scripts
      <form>.xlsx   the requirements workbook, the testing template
    Config and the signing buttons come from the form's row of Combined Form
    Requirements. Left for the developer, as the procedure has it: the code
    for every field marked Yes* (Custom Save Validation) - its note in the
    Logic column says what the rule is - and the form hooks.
    Hypothecation Agreement is one cleaned form and three production forms
    (Bonds, Mutual Funds, Stocks), so it has three folders here, each with
    the shared pages 1-4 and its own signing page.
    Character limits (a Libro account number is 12 digits) are written as
    the PDF's MaxLen and hold in any viewer. The designer shows and edits
    them in its Format section from form_requirements commit ce0921b on; an
    older designer build keeps them but does not show them.

not in the form table\\
    Forms with no row in Combined Form Requirements: the workbook lists them
    under their old names as "Not Required" or "To be removed from CRM".
    Each has its cleaned form and a designer output with no Config and no
    signing buttons, and why.txt.

3 proofs\\
    Each form beside its original. The name in [brackets] says how it was
    reformatted: plain / condense / stubborn / rebuild.

4 field lists\\<form>\\
    fields.csv     one row per field: page, name, kind, the label beside it,
                   what goes there, what it was called before, and the export
                   value of a pick-one box
    named pN.png   each page with its fields boxed and named

designer settings.csv
    every field's Load / Save / Custom Save Validation, error message name,
    format, character limit and note, form by form - what the designer
    output carries
form table matches.csv
    which workbook row each form was matched to
reviewer comments.csv
    the sticky notes the subject experts left on the forms - requirements
    in their words ("Fields are conditional based on loan type"). They stay
    on the cleaned forms; the designer's export removes every comment, so
    they are not on the designer output
missing from DB.csv / .txt
    values the forms ask for that the DB list does not have yet, named as
    the old datafeed named them where it had them, and whether it did
typed now, old datafeed filled.csv
    values typed in on the forms that the old datafeed used to fill (the
    beneficiaries, executors, guarantors...), which the new DB lacks
to confirm.csv
    DB matches and notes a person should check
all fields.csv
    every field on every form, in one list
form mappings.txt, missing forms.txt
    which file is which production form; production forms with no file yet

Field kinds in fields.csv: db = a DB field; candidate = a value missing
from the DB (listed in missing from DB); special = a signer or staff field
the builder fills; entry = typed in by the member or staff.
"""


README_AS_IS = """Libro forms - special test forms, taken as is
=============================================

The ten forms given in "dont restructure": copies cleaned by hand, handed
over apart from the main handover. Nothing on their pages was moved or
restyled - every field is where and as big as its maker drew it, and every
page prints as given (checked: no box moved, pages pixel for pixel the same).
They were only named and coded:
  - every field named from the DB list, by the same rules as the main set,
    each taking the name and validation the reviewed copy of the same form
    has (Large Cash Deposit Form named fresh);
  - radio buttons made pick-one check boxes, each box where it was;
  - load and save validation, formats, character limits;
  - the designer's buttons, Config and requirements. On four forms the logo
    sits where the buttons go, so they are on the same line, just right of it.
What could not be done without moving things: a name the form gives two
boxes stays two fields (first name, surname), and a blank the form prints
with no field gets none.

1 cleaned forms\\        the named forms, before the designer
2 designer output\\<form>\\  the designer's export: the coded PDF and its
                          requirements workbook
3 proofs\\               each form as given beside the final
4 field lists\\<form>\\     fields.csv and each page with its fields named
designer settings.csv    every field's validation, format, limit and rule
missing from DB.csv / .txt, to confirm.csv, all fields.csv
                         as in the main handover, for these ten
"""


def fingerprint(path):
    """What a form is, without what changes every time it is written: its
    pages as drawn, its fields and everything set on them, the designer's
    scripts and its requirements - less the requirements' generatedAt and
    field ids, the file ID and the dates, which differ on every build. Two builds of an
    unchanged form share it; a byte hash never did, and every approved form
    read as rebuilt after each round."""
    import hashlib
    import fitz
    h = hashlib.sha1()
    doc = fitz.open(path)
    for page in doc:
        h.update(page.read_contents())
        # sorted: the fields' order on the page is Tab and drawing order,
        # which a reviewer does not judge, and its fixes touched every form
        for r in sorted(repr((w.field_name, w.field_type, [round(v, 1) for v in w.rect], w.field_flags,
                              w.field_value, w.on_state() if w.field_type in (2, 5) else "",
                              doc.xref_get_key(w.xref, "MaxLen")[1], doc.xref_get_key(w.xref, "F")[1],
                              w.script_format, w.script_stroke, w.script_calc, w.script_blur,
                              w.text_font, w.text_fontsize)) for w in page.widgets()):
            h.update(r.encode())
    names = doc.xref_get_key(doc.pdf_catalog(), "Names/JavaScript/Names")
    for ref in re.findall(r"(\d+) 0 R", names[1] if names[0] == "array" else ""):
        js = doc.xref_get_key(int(ref), "JS")
        if js[0] == "xref":
            h.update(doc.xref_stream(int(js[1].split()[0])) or b"")
        else:
            h.update(js[1].encode())
    for n in doc.embfile_names():
        try:
            req = json.loads(doc.embfile_get(n))
            req.pop("generatedAt", None)
            # each field and marker gets a fresh random id on every import
            for item in req.get("fields", []) + req.get("markers", []):
                if isinstance(item, dict):
                    item.pop("id", None)
            h.update(json.dumps(req, sort_keys=True).encode())
        except ValueError:
            h.update(doc.embfile_get(n))
    return h.hexdigest()


def snapshot(path):
    """A form as the reviewer sees it: per field, what the designer has set
    on it and where its boxes are; per page, its printed content's hash. The
    review station compares a verdict's snapshot with the current one."""
    import hashlib
    import fitz
    doc = fitz.open(path)
    fields = {}
    for n in doc.embfile_names():
        try:
            req = json.loads(doc.embfile_get(n))
        except ValueError:
            continue
        for f in req.get("fields", []):
            if f.get("systemPlacement") or not f.get("name"):
                continue
            s = fields.setdefault(f["name"].strip(), {
                "type": f.get("type"), "editable": f.get("editable"),
                "load": f.get("loadValidated"), "save": f.get("saveValidated"),
                "custom": f.get("customSaveValidated"), "note": f.get("note", ""),
                "label": f.get("customerFacingName", ""),
                "format": (f.get("format") or {}).get("category", "none"),
                "maxLength": f.get("maxLength", 0), "multiline": f.get("multiline"),
                "boxes": []})
            r = f.get("rect", {})
            s["boxes"].append([f.get("page", 0), round(r.get("x", 0)), round(r.get("y", 0)),
                               round(r.get("width", 0)), round(r.get("height", 0))])
    for s in fields.values():
        s["boxes"].sort()
    pages = [hashlib.sha1(p.read_contents()).hexdigest()[:12] for p in doc]
    return {"fields": fields, "pages": pages}


def copy_tree(src, dst, missing):
    if not os.path.isdir(src):
        missing.append(src)
        return 0
    shutil.copytree(src, dst)
    return len(os.listdir(dst))


def norm(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--output")
    ap.add_argument("--set", choices=["main", "as-is"], default="main",
                    help="as-is: the special test forms taken as is (dont restructure/), "
                         "handed over on their own")
    args = ap.parse_args()
    global NAMING, REVIEW
    if args.set == "as-is":
        NAMING = REVIEW = os.path.join(HERE, "field naming as is")
    sfx = "_as_is" if args.set == "as-is" else ""
    out = args.output or os.path.join(HERE, "handover" if args.set == "main" else "handover - special (as is)")
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(out)
    missing, said = [], []
    matches = json.load(open(os.path.join(DESIGNER, f"form_matches{sfx}.json"), encoding="utf-8"))
    unmatched_files = {m["file"] for m in matches if m["match"] != "exact"}
    named = os.path.join(NAMING, "named forms")

    # 1 the cleaned forms, the matched ones
    cleaned = os.path.join(out, "1 cleaned forms")
    os.makedirs(cleaned)
    n_clean = 0
    for f in sorted(os.listdir(named)):
        if f.endswith(".pdf") and norm(f[:-4]) not in {norm(u) for u in unmatched_files}:
            shutil.copy2(os.path.join(named, f), cleaned)
            n_clean += 1

    # 2 the designer output, per production form; and the forms with no row
    designed = os.path.join(out, "2 designer output")
    apart = os.path.join(out, "not in the form table")
    os.makedirs(designed)
    if any(m["match"] != "exact" for m in matches):
        os.makedirs(apart)
    n_design = n_apart = 0
    for m in matches:
        prod = m["production name"].strip()
        src = os.path.join(DESIGNER, f"out{sfx}", prod)
        if m["match"] == "exact":
            if not os.path.isdir(src):
                missing.append(f"designer output for {prod}")
                continue
            shutil.copytree(src, os.path.join(designed, prod))
            n_design += 1
            continue
        dst = os.path.join(apart, prod)
        os.makedirs(dst)
        pdf = next((f for f in os.listdir(named) if norm(f[:-4]) == norm(m["file"])), None)
        if pdf:
            shutil.copy2(os.path.join(named, pdf), os.path.join(dst, f"{prod} - cleaned.pdf"))
        else:
            missing.append(f"cleaned form for {prod}")
        if os.path.isdir(src):
            for f in os.listdir(src):
                base, ext = os.path.splitext(f)
                shutil.copy2(os.path.join(src, f), os.path.join(dst, f"{base} - designer output{ext}"))
        with open(os.path.join(dst, "why.txt"), "w", encoding="utf-8") as fh:
            fh.write(f"{prod}\n\nNo row for this form in Combined Form Requirements ({m['match']}"
                     f"{', ' + m['closest'] if m.get('closest') else ''}).\n"
                     "So nothing gives it a Config (form ID, cabinet, membership type, product "
                     "codes) or its signing buttons: the designer output here has neither. Its "
                     "fields are named and set like every other form's.\n")
        n_apart += 1

    n_proofs = copy_tree(os.path.join(REVIEW, "proofs"), os.path.join(out, "3 proofs"), missing)
    lists = os.path.join(out, "4 field lists")
    os.makedirs(lists)
    n_lists = 0
    sys.path.insert(0, HERE)
    import apply_fields
    # the forms taken as is stand in for the restyled copies they supersede
    for stem, folder in apply_fields.form_dirs(NAMING):
        dst = os.path.join(lists, stem)
        os.makedirs(dst)
        for f in sorted(os.listdir(folder)):
            if f == "fields.csv" or (f.startswith("named p") and f.endswith(".png")):
                shutil.copy2(os.path.join(folder, f), dst)
        n_lists += 1

    for src in (os.path.join(NAMING, f) for f in
                ("missing from DB.csv", "missing from DB.txt", "typed now, old datafeed filled.csv",
                 "to confirm.csv", "all fields.csv")):
        if os.path.isfile(src):
            shutil.copy2(src, out)
        else:
            missing.append(src)
    for f in ("form mappings.txt", "missing forms.txt") if args.set == "main" else ():
        src = os.path.join(REVIEW, f)
        if os.path.isfile(src):
            shutil.copy2(src, out)
        else:
            missing.append(src)
    src = os.path.join(DESIGNER, "form_matches.csv")
    if os.path.isfile(src):
        shutil.copy2(src, os.path.join(out, "form table matches.csv"))
    else:
        missing.append(src)

    # every field's designer settings, from the build's jobs
    jobs = os.path.join(DESIGNER, f"jobs{sfx}")
    rows = []
    for f in sorted(os.listdir(jobs)) if os.path.isdir(jobs) else []:
        if not f.endswith(".json"):
            continue
        job = json.load(open(os.path.join(jobs, f), encoding="utf-8"))
        for name, s in job["fields"].items():
            fmt = (s.get("format") or {}).get("category", "")
            if fmt == "number":
                fmt = f"number, {s['format'].get('decimals', 2)} decimals"
            rows.append([job["production name"], name, s.get("kind", ""), s.get("label", ""),
                         "Yes" if s.get("loadValidated") else "",
                         "Yes*" if s.get("customSaveValidated") else ("Yes" if s.get("saveValidated") else ""),
                         s.get("customerFacingName", ""), "Editable" if s.get("editable", True) else "Read Only",
                         fmt, s.get("maxLength") or "", "yes" if s.get("multiline") else "",
                         s.get("note", "")])
    with open(os.path.join(out, "designer settings.csv"), "w", newline="", encoding="utf-8-sig") as fh:
        wr = csv.writer(fh)
        wr.writerow(["form", "field", "kind", "label on form", "Load Validation", "Save Validation",
                     "error message name", "access", "format", "character limit", "multiline",
                     "note / rule"])
        wr.writerows(rows)

    # the subject experts' sticky notes on the forms: requirements in their
    # own words ("Fields are conditional based on loan type"), which the
    # designer's export strips along with every other comment
    import fitz
    notes = []
    for f in sorted(os.listdir(named)):
        if not f.endswith(".pdf"):
            continue
        with fitz.open(os.path.join(named, f)) as doc:
            for page in doc:
                for a in page.annots():
                    if a.type[1] in ("Text", "FreeText", "Highlight", "Underline", "Square", "Ink"):
                        notes.append([f[:-4], page.number + 1, a.type[1], a.info.get("title", ""),
                                      " ".join((a.info.get("content") or "").split())])
    with open(os.path.join(out, "reviewer comments.csv"), "w", newline="", encoding="utf-8-sig") as fh:
        wr = csv.writer(fh)
        wr.writerow(["form", "page", "kind", "by", "comment"])
        wr.writerows(notes)

    with open(os.path.join(out, "README.txt"), "w", encoding="utf-8") as fh:
        fh.write(README if args.set == "main" else README_AS_IS)

    # what each form is, for the review station to tell a rebuilt form from
    # one only written again (designer/review_server.mjs)
    prints = {}
    for root, _, files in os.walk(out):
        for f in files:
            if f.endswith(".pdf") and (os.path.basename(root) + ".pdf" == f
                                       or f.endswith(" - designer output.pdf")):
                prints[os.path.relpath(os.path.join(root, f), out).replace(os.sep, "/")] = \
                    fingerprint(os.path.join(root, f))
    with open(os.path.join(out, "fingerprints.json"), "w", encoding="utf-8") as fh:
        json.dump(prints, fh, indent=1, sort_keys=True)
    # and what each one holds, field by field, so the station can say what
    # changed since a verdict rather than only that something did
    with open(os.path.join(out, "snapshots.json"), "w", encoding="utf-8") as fh:
        json.dump({rel.split("/")[1]: snapshot(os.path.join(out, rel)) for rel in prints},
                  fh, sort_keys=True)

    print(f"{out}:")
    print(f"   {n_clean} cleaned forms, {n_design} designer outputs, {n_apart} not in the form table, "
          f"{n_proofs} proofs, {n_lists} field lists, {len(rows)} field settings")
    for d in sorted(os.listdir(designed)):
        have = os.listdir(os.path.join(designed, d))
        if len([f for f in have if f.endswith((".pdf", ".xlsx"))]) != 2:
            said.append(f"   {d}: designer output has {have}")
    for s in said:
        print(s)
    for m in missing:
        print(f"   MISSING: {m}")
    return 1 if said or missing else 0


if __name__ == "__main__":
    sys.exit(main())
