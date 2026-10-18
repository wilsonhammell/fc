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
    (Bonds, Mutual Funds, Stocks), so it has three folders here.

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
    format and note, form by form - what the designer output carries
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
    ap.add_argument("-o", "--output", default=os.path.join(HERE, "handover"))
    args = ap.parse_args()
    out = args.output
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(out)
    missing, said = [], []
    matches = json.load(open(os.path.join(DESIGNER, "form_matches.json"), encoding="utf-8"))
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
    os.makedirs(apart)
    n_design = n_apart = 0
    for m in matches:
        prod = m["production name"].strip()
        src = os.path.join(DESIGNER, "out", prod)
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
    for stem in sorted(os.listdir(NAMING)):
        folder = os.path.join(NAMING, stem)
        if not os.path.isfile(os.path.join(folder, "fields.csv")):
            continue
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
    for f in ("form mappings.txt", "missing forms.txt"):
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
    jobs = os.path.join(DESIGNER, "jobs")
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
        fh.write(README)

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
