"""Which form-table row each form of a batch is, and the Config it gets.

    python designer/match_batch.py "2026-10-22 batch" designer/form_table_2026-10-22.json

A batch arrives with its own copy of Combined Form Requirements (read into
the table JSON by form_table.mjs). Each form takes, as the designer's
"Import Configuration from Form Table" would:

  1. its row on the sheet the designer opens on (Confirmed Forms), by name;
  2. where that row is shifted a column (the import reads Membership "Type"
     as "NO", Remote eSign as "Combined" - Funeral Home GIC, GIC, Indemnity,
     LIF Spousal Consent in the 2026-10-22 workbook), its FO Data Line row
     instead, which is aligned - keeping Confirmed Forms' tag element (Form
     ID) where FO Data Line has none;
  3. where the chosen row has no product codes (doc_sys), FO Data Line's.

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
    out = []
    for f in sorted(os.listdir(batch)):
        if not f.lower().endswith(".pdf"):
            continue
        stem = f[:-4]
        root = next((r for r in roots if os.path.isfile(os.path.join(HERE, r, stem, f"{stem}.pdf"))), None)
        conf, fo = rows.get((CONFIRMED, norm(stem))), rows.get((FO, norm(stem)))
        said = []
        row = conf or fo
        if row is None:
            out.append({"production name": stem, "file": stem, "folder": stem if root else None, "root": root,
                        "match": "unmatched - no row in the batch's workbook", "config": {}, "buttons": {}, "notes": []})
            continue
        config, buttons = dict(row["config"]), dict(row["buttons"])
        if conf and conf["notes"] and fo:
            config, buttons = dict(fo["config"]), dict(fo["buttons"])
            if "formId" not in config and "formId" in conf["config"]:
                config["formId"] = conf["config"]["formId"]
            said.append(f"Confirmed Forms row {conf['row']} is shifted a column ({'; '.join(conf['notes'])}): "
                        f"Config and buttons from FO Data Line row {fo['row']}, Form ID from Confirmed Forms")
        if not config.get("productCodes") and fo and fo["config"].get("productCodes"):
            config["productCodes"] = fo["config"]["productCodes"]
            said.append(f"product codes from FO Data Line row {fo['row']}")
        out.append({"production name": stem, "file": stem, "folder": stem if root else None, "root": root,
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
