#!/usr/bin/env python3
"""The handover for a batch of forms the business cleaned itself.

    python make_batch_handover.py 2026-10-22

Run after carry_batch.py and apply_fields.py ("field naming <batch>"),
designer/match_batch.py, designer/requirements.py, build.mjs and
designer/finish.py for the batch (their --matches/--jobs/--out). Writes
"handover <batch> batch/" and its zip.
"""
import csv
import json
import os
import shutil
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))

README = """Libro forms - {batch} batch
{rule}

{count} forms the business cleaned itself, as given in "{batch} batch". Nothing
on their pages was moved or restyled: every page prints exactly as given
(checked pixel for pixel), and every field the business drew is where and as
big as they drew it. They were only named and coded:

  - every field named by the same rules as the main handover. Where the main
    handover has the same form, each field takes the name, validation and
    rules that form's review settled; where the layout differs, by reading
    the page around each field;
  - a field placed on each printed blank that had none (a ruled line, a
    table cell, a tick box): Account Declaration Agreement, Bond of Indemnity
    and Designated Beneficiary Declaration came with no fields at all;
  - Acrobat's auto-detected leftovers that cover printed text or a whole
    table were taken off, and per-cell fields put in their place;
  - load and save validation, formats, character limits;
  - the designer's buttons, Config and requirements, from the Combined Form
    Requirements workbook that came with the batch (its doc_sys column gives
    the product codes; where a Confirmed Forms row is shifted a column, its
    FO Data Line row is used).

1 named forms\\              the named forms, before the designer
2 designer output\\<form>\\   the designer's export: the coded PDF and its
                            requirements workbook
3 field lists\\<form>\\       fields.csv and each page with its fields named
designer settings.csv      every field's validation, format, limit and rule
form table matches.csv     the workbook row each form took, and why
decisions.txt              what was decided on each form, to check
missing from DB.csv / .txt, to confirm.csv, all fields.csv
                           as in the main handover, for these forms

Kinds: db = pulled from the DB list; candidate = a value the DB should supply
but does not yet (listed in missing from DB); special = a signer or staff
field the designer handles; entry = typed in by the member or staff.
"""


def main():
    batch = sys.argv[1]
    naming = os.path.join(HERE, f"field naming {batch}")
    designer = os.path.join(HERE, "designer")
    matches = json.load(open(os.path.join(designer, f"form_matches_{batch}.json"), encoding="utf-8"))
    out = os.path.join(HERE, f"handover {batch} batch")
    if os.path.exists(out):
        shutil.rmtree(out)
    missing = []
    named_dir, design_dir, lists_dir = (os.path.join(out, d) for d in
                                        ("1 named forms", "2 designer output", "3 field lists"))
    for d in (named_dir, design_dir, lists_dir):
        os.makedirs(d)
    for m in matches:
        prod, folder = m["production name"], os.path.join(naming, m["folder"] or "")
        pdf = os.path.join(folder, f"{m['folder']}.pdf")
        if os.path.isfile(pdf):
            shutil.copy2(pdf, named_dir)
        else:
            missing.append(f"named form for {prod}")
        src = os.path.join(designer, f"out_{batch}", prod)
        if os.path.isdir(src) and len(os.listdir(src)) == 2:
            shutil.copytree(src, os.path.join(design_dir, prod))
        else:
            missing.append(f"designer output for {prod}")
        dst = os.path.join(lists_dir, prod)
        os.makedirs(dst)
        for f in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
            if f == "fields.csv" or (f.startswith("named p") and f.endswith(".png")):
                shutil.copy2(os.path.join(folder, f), dst)
    for f in ("missing from DB.csv", "missing from DB.txt", "typed now, old datafeed filled.csv",
              "to confirm.csv", "all fields.csv"):
        src = os.path.join(naming, f)
        if os.path.isfile(src):
            shutil.copy2(src, out)
        else:
            missing.append(src)

    with open(os.path.join(out, "form table matches.csv"), "w", newline="", encoding="utf-8-sig") as fh:
        wr = csv.writer(fh)
        wr.writerow(["form", "sheet", "row", "form ID", "cabinet", "membership type", "menu group",
                     "product codes", "buttons", "how"])
        for m in matches:
            c = m["config"]
            wr.writerow([m["production name"], m.get("sheet", ""), m.get("row", ""), c.get("formId", ""),
                         c.get("foCabinet", ""), c.get("formType", ""), c.get("formMenuGroups", ""),
                         c.get("productCodes", ""), ", ".join(m["buttons"]), "; ".join(m["notes"])])

    rows = []
    jobs = os.path.join(designer, f"jobs_{batch}")
    for f in sorted(os.listdir(jobs)):
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
                         fmt, s.get("maxLength") or "", "yes" if s.get("multiline") else "", s.get("note", "")])
    with open(os.path.join(out, "designer settings.csv"), "w", newline="", encoding="utf-8-sig") as fh:
        wr = csv.writer(fh)
        wr.writerow(["form", "field", "kind", "label on form", "Load Validation", "Save Validation",
                     "error message name", "access", "format", "character limit", "multiline",
                     "note / rule"])
        wr.writerows(rows)

    # each form's decisions: its notes, the blanks left without a field and
    # the fields taken off, with why
    with open(os.path.join(out, "decisions.txt"), "w", encoding="utf-8") as fh:
        for m in matches:
            mp = json.load(open(os.path.join(naming, m["folder"], "mapping.json"), encoding="utf-8"))
            ctx = {i["id"]: i for i in json.load(open(os.path.join(naming, m["folder"], "context.json"),
                                                      encoding="utf-8"))["items"]}
            lines = list(mp.get("notes", [])) + list(m["notes"])
            lines += [f"no field on a blank at page {ctx[k]['page'] + 1}: {why}" for k, why in mp.get("skip", {}).items()
                      if k in ctx]
            lines += [f"taken off: {ctx[k]['existing']!r} at page {ctx[k]['page'] + 1}, "
                      f"{[round(v) for v in ctx[k]['rect']]}" for k in mp.get("drop", []) if k in ctx]
            if lines:
                fh.write(f"{m['production name']}\n" + "".join(f"  - {s}\n" for s in lines) + "\n")

    with open(os.path.join(out, "README.txt"), "w", encoding="utf-8") as fh:
        title = f"Libro forms - {batch} batch"
        fh.write(README.format(batch=batch, rule="=" * len(title), count=len(matches)))

    zpath = f"{out}.zip"
    if os.path.exists(zpath):
        os.remove(zpath)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(out):
            for f in files:
                p = os.path.join(root, f)
                z.write(p, os.path.relpath(p, HERE))
    print(f"{out}: {len(os.listdir(named_dir))} named forms, {len(os.listdir(design_dir))} designer outputs, "
          f"{len(rows)} field settings; zip {os.path.getsize(zpath) / 1e6:.1f} MB")
    for s in missing:
        print("   MISSING:", s)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
