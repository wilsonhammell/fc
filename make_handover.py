"""Gather everything for the final forms into one folder to hand over.

    python make_handover.py [-o handover]

Run after make_review_folder.py and apply_fields.py. The output folder is
emptied first and filled from:
    field naming/named forms/      the finished forms, fields named
    forms for review/forms at 10pt the same forms before their fields were named
    forms for review/proofs        each form beside its original
    field naming/<form>/           each form's field list and named pages
    field naming/*.csv, *.txt      the lists across all forms
Anything missing is said at the end - nothing is left out without a word.
"""
import argparse
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NAMING = os.path.join(HERE, "field naming")
REVIEW = os.path.join(HERE, "forms for review")

README = """\
Libro forms - final selection
=============================

1 finished forms\\
    The forms to use: Arial 10pt, Libro header, signing areas, and every
    field named from the DB list (dbFields.json).

2 forms before field naming\\
    The same forms with their fields as the source files had them.

3 proofs\\
    Each form beside its original. The name in [brackets] says how it was
    reformatted: plain / condense / stubborn / rebuild.

4 field lists\\<form>\\
    fields.csv     one row per field: page, name, kind, the label beside it,
                   what goes there, and what the field was called before
    named pN.png   each page with its fields boxed and named

missing from DB.csv / .txt
    values the forms ask for that the DB list does not have yet
to confirm.csv
    DB matches and notes a person should check
all fields.csv
    every field on every form, in one list
form mappings.txt
    which file is which production form
missing forms.txt
    production forms with no source file yet

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


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--output", default=os.path.join(HERE, "handover"))
    args = ap.parse_args()
    out = args.output
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(out)
    missing = []
    n_named = copy_tree(os.path.join(NAMING, "named forms"), os.path.join(out, "1 finished forms"), missing)
    n_plain = copy_tree(os.path.join(REVIEW, "forms at 10pt"),
                        os.path.join(out, "2 forms before field naming"), missing)
    n_proofs = copy_tree(os.path.join(REVIEW, "proofs"), os.path.join(out, "3 proofs"), missing)

    lists = os.path.join(out, "4 field lists")
    os.makedirs(lists)
    n_lists = 0
    for stem in sorted(os.listdir(NAMING)):
        folder = os.path.join(NAMING, stem)
        csv_path = os.path.join(folder, "fields.csv")
        if not os.path.isfile(csv_path):
            continue
        dst = os.path.join(lists, stem)
        os.makedirs(dst)
        for f in sorted(os.listdir(folder)):
            if f == "fields.csv" or (f.startswith("named p") and f.endswith(".png")):
                shutil.copy2(os.path.join(folder, f), dst)
        n_lists += 1

    for src in (os.path.join(NAMING, f) for f in
                ("missing from DB.csv", "missing from DB.txt", "to confirm.csv", "all fields.csv")):
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
    with open(os.path.join(out, "README.txt"), "w", encoding="utf-8") as fh:
        fh.write(README)

    print(f"{out}:")
    print(f"   {n_named} finished forms, {n_plain} before field naming, "
          f"{n_proofs} proofs, {n_lists} field lists")
    # every finished form has its proof and field list, and the other way round
    stems = lambda names: {os.path.splitext(n)[0].split(" [")[0].strip() for n in names}
    sets = {"finished forms": stems(os.listdir(os.path.join(out, "1 finished forms")))
            if n_named else set(),
            "proofs": stems(os.listdir(os.path.join(out, "3 proofs"))) if n_proofs else set(),
            "field lists": set(os.listdir(lists))}
    every = set().union(*sets.values())
    bad = 0
    for what, have in sets.items():
        for s in sorted(every - have):
            print(f"   no {what[:-1] if what.endswith('s') else what} for: {s}")
            bad += 1
    for m in missing:
        print(f"   MISSING: {m}")
    return 1 if bad or missing else 0


if __name__ == "__main__":
    sys.exit(main())
