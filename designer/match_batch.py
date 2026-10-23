"""Which form-table row each form of a batch is, and the Config it gets.

    python designer/match_batch.py "2026-10-22 batch" designer/form_table_2026-10-22.json

A batch arrives with its own copy of Combined Form Requirements (read into
the table JSON by form_table.mjs). Each form takes, as the designer's
"Import Configuration from Form Table" would:

  1. its row on the sheet the designer opens on (Confirmed Forms), by name;
  2. where that row is misaligned (the import reads Membership "Type" as
     "NO", Remote eSign as "Combined" - Funeral Home GIC, GIC, Indemnity,
     LIF Spousal Consent in the 2026-10-22 workbook), the buttons, membership
     type and menu group from its FO Data Line row, which is aligned - Form ID
     and product codes still from Confirmed Forms;
  3. product codes (doc_sys) from Confirmed Forms, the column the business
     keeps up to date; FO Data Line's only where Confirmed Forms has none.

The named form is looked for in "field naming <batch>" (the batch's own
naming) and then "field naming as is" (a batch file that is one of the forms
already taken as is, page for page). Writes designer/form_matches_<batch>.json.
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIRMED, FO = "Confirmed Forms(74)", "FO Data Line"


def norm(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def main():
    batch, table_path = sys.argv[1], sys.argv[2]
    tag = os.path.basename(os.path.normpath(batch)).replace(" batch", "")
    table = json.load(open(table_path, encoding="utf-8"))
    rows = {}
    for r in table["rows"]:
        rows.setdefault((r["sheet"], norm(r["formName"])), r)
    roots = [f"field naming {tag}", "field naming as is"]
    # a file whose name is not its row's: the main set's matches already say
    # which row it is ("Direction of Milk Quota" is "... Milk Quota Proceeds")
    known = {}
    for m in json.load(open(os.path.join(HERE, "designer", "form_matches.json"), encoding="utf-8")):
        if m.get("match") == "exact":
            known.setdefault(norm(m["file"]), []).append(m["production name"].strip())
    out = []
    for f in sorted(os.listdir(batch)):
        if not f.lower().endswith(".pdf"):
            continue
        folder = f[:-4]
        root = next((r for r in roots if os.path.isfile(os.path.join(HERE, r, folder, f"{folder}.pdf"))), None)
        stem = folder
        if (CONFIRMED, norm(stem)) not in rows and len(known.get(norm(stem), [])) == 1:
            stem = known[norm(stem)][0]
        conf, fo = rows.get((CONFIRMED, norm(stem))), rows.get((FO, norm(stem)))
        said = []
        row = conf or fo
        if row is None:
            out.append({"production name": stem, "file": folder, "folder": folder if root else None, "root": root,
                        "match": "unmatched - no row in the batch's workbook", "config": {}, "buttons": {}, "notes": []})
            continue
        config, buttons = dict(row["config"]), dict(row["buttons"])
        if conf and conf["notes"] and fo:
            config, buttons = dict(fo["config"]), dict(fo["buttons"])
            # Confirmed Forms' own Form ID and product codes still stand:
            # those columns read correctly on a misaligned row
            for key in ("formId", "productCodes"):
                if conf["config"].get(key):
                    config[key] = conf["config"][key]
            said.append(f"Confirmed Forms row {conf['row']} is misaligned ({'; '.join(conf['notes'])}): "
                        f"buttons, membership type and menu group from FO Data Line row {fo['row']}; "
                        f"Form ID and product codes from Confirmed Forms")
        if not config.get("productCodes") and fo and fo["config"].get("productCodes"):
            config["productCodes"] = fo["config"]["productCodes"]
            said.append(f"product codes from FO Data Line row {fo['row']}")
        out.append({"production name": stem, "file": folder, "folder": folder if root else None, "root": root,
                    "match": "exact", "score": 1.0, "sheet": row["sheet"], "row": row["row"],
                    "row name": row["formName"], "closest": "", "config": config,
                    "buttons": {k: v for k, v in buttons.items() if v}, "notes": said})
    path = os.path.join(HERE, "designer", f"form_matches_{tag}.json")
    json.dump(out, open(path, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    for m in out:
        print(f"{m['production name'][:45]:45} {str(m['root'])[:22]:22} {m.get('sheet', '-')[:9]:9} "
              f"{m.get('row', '')!s:4} buttons {list(m['buttons'])} codes {m['config'].get('productCodes')!r}")
        for s in m["notes"]:
            print("      ", s)
    print(f"{len(out)} forms -> {os.path.relpath(path, HERE)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
