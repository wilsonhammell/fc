#!/usr/bin/env python3
"""Mappings for forms taken as is ("dont restructure"), carried from the
reviewed mappings of the same forms.

    python carry_as_is.py [--write]

A form in "dont restructure/" is the business's own hand-cleaned copy: its
fields stay exactly where and as big as they are (mapping "as_is"). Its
field_context ids differ from the restyled copy's, but its fields carry the
same old merge tags, so each field takes the name, kind and rules its tag
was given in "field naming/<form>/mapping.json" - every review round's
decision. Left as the draft, and said:
  - a field whose tag the reviewed mapping has no item for;
  - a split name the reviewed copy merged into one *_FULL_LEGAL_NAME field:
    merging needs one box over both, which moves the boxes;
  - a blank the form prints with no field on it: no field is added.
Fields the review added ("add") are listed, not placed.
"""
import argparse
import json
import os
import re
import sys

import fitz

HERE = os.path.dirname(os.path.abspath(__file__))
AS_IS = os.path.join(HERE, "field naming as is")
NAMING = os.path.join(HERE, "field naming")
# the reviewed copy's folder where its name differs
OLD_NAME = {"ATM POS Trace Request": "ATM & POS Trace Request",
            "Reimbursement Claim Form": "Reimbursment Claim Form"}
KEEP = ("name", "kind", "label", "desc", "save", "custom", "save_on", "option", "format",
        "facing", "editable", "load", "note", "maxlen", "type")


def near(a, b):
    return sum(abs(x - y) for x, y in zip(a, b))


def carry(form, write):
    new_dir = os.path.join(AS_IS, form)
    old_dir = os.path.join(NAMING, OLD_NAME.get(form, form))
    ctx = json.load(open(os.path.join(new_dir, "context.json"), encoding="utf-8"))
    draft = json.load(open(os.path.join(new_dir, "mapping.json"), encoding="utf-8"))
    said = []
    if not os.path.exists(os.path.join(old_dir, "mapping.json")):
        draft["as_is"] = True
        said.append("no reviewed copy - the draft stands, to review")
        return draft, said
    octx = json.load(open(os.path.join(old_dir, "context.json"), encoding="utf-8"))
    old = json.load(open(os.path.join(old_dir, "mapping.json"), encoding="utf-8"))
    merged = {k for k, it in old["items"].items()
              if it.get("name", "").endswith("_FULL_LEGAL_NAME") and it.get("rect")
              and "full legal name" in it.get("desc", "")}
    items, skip, drop = {}, {}, []
    for i in ctx["items"]:
        k = i["id"]
        if i["existing"] is None:
            skip[k] = "taken as is: the form prints this blank with no field, and none is added"
            continue
        cands = [o for o in octx["items"] if o["existing"] == i["existing"] and o["page"] == i["page"]]
        o = min(cands, key=lambda o: near(o["rect"], i["rect"])) if cands else None
        if o is None:
            items[k] = draft["items"].get(k) or {"name": i["existing"], "kind": "entry"}
            said.append(f"{k} {i['existing']!r}: no field with this tag in the reviewed copy - draft name "
                        f"{items[k].get('name')!r}, to review")
            continue
        if o["id"] in old.get("drop", []):
            if any(o["id"] == d for d in old["drop"]) and i["type"] == "Button":
                drop.append(k)
                continue
            items[k] = draft["items"].get(k) or {"name": i["existing"], "kind": "entry"}
            said.append(f"{k} {i['existing']!r}: merged away in the reviewed copy; kept as is as "
                        f"{items[k].get('name')!r}")
            continue
        it = old["items"].get(o["id"])
        if it is None:
            items[k] = draft["items"].get(k) or {"name": i["existing"], "kind": "entry"}
            said.append(f"{k} {i['existing']!r}: not in the reviewed mapping - draft {items[k].get('name')!r}")
            continue
        if o["id"] in merged:
            items[k] = draft["items"].get(k) or {"name": i["existing"], "kind": "entry"}
            said.append(f"{k} {i['existing']!r}: the reviewed copy made this one *_FULL_LEGAL_NAME box; "
                        f"as is it stays its own box, {items[k].get('name')!r}")
            continue
        items[k] = {key: it[key] for key in KEEP if key in it}
    for a in old.get("add", []):
        said.append(f"not placed (the reviewed copy added it): {a['name']} - {a.get('desc', '')[:60]}")
    out = {"as_is": True, "items": items, "drop": drop, "skip": skip,
           "notes": old.get("notes", [])}
    for key in ("production_pages",):
        if key in old:
            out[key] = old[key]
    return out, said


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    for form in sorted(os.listdir(AS_IS)):
        if not os.path.exists(os.path.join(AS_IS, form, "context.json")):
            continue
        m, said = carry(form, args.write)
        print(f"== {form}: {len(m['items'])} field(s) named, {len(m.get('skip', {}))} blank(s) left as they are")
        for s in said:
            print("   ", s)
        if args.write:
            with open(os.path.join(AS_IS, form, "mapping.json"), "w", encoding="utf-8", newline="\n") as fh:
                json.dump(m, fh, indent=1, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
