#!/usr/bin/env python3
"""Draft mappings for a batch of forms the business cleaned itself, carried
from the reviewed mappings of the same forms.

    python carry_batch.py "field naming 2026-10-22" [--write]

A batch form is taken as is (mapping "as_is"): nothing on its page moves, its
fields stay where and as big as they are, and a blank it prints with no field
gets one, the blank's own size. Its context ids differ from the reviewed
copy's, so each place is matched to the reviewed copy's, in this order:

  1. a field already carrying a reviewed name (the business started from our
     handover): that name's rules;
  2. a field carrying an old merge tag the reviewed copy also had: what the
     reviewed copy made of that tag;
  3. anything else, a blank included: the reviewed place whose words around
     it (label, caption, words above, section) read most alike, PROPOSED -
     these are listed with "check", to confirm against the page.

Writes <batch>/<form>/mapping.json with --write; without it, says what it
would do. A proposal is only a draft: the mapping is reviewed by hand after.
"""
import argparse
import difflib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NAMING = os.path.join(HERE, "field naming")
AS_IS = os.path.join(HERE, "field naming as is")
# the reviewed copy's folder where its name differs
REVIEWED = {"Guaranteed Investment Certificate": "Guaranteed Investment Certificate Authorization"}
KEEP = ("name", "kind", "label", "desc", "save", "custom", "save_on", "option", "format",
        "facing", "editable", "load", "note", "maxlen", "type")


def words(item):
    return " | ".join(re.sub(r"\s+", " ", str(item.get(k) or "")).strip().lower()
                      for k in ("left", "below", "above", "right", "section"))


def near(a, b):
    return sum(abs(x - y) for x, y in zip(a, b))


def reviewed(form):
    """(context, mapping) of the reviewed copy: the as-is copy where the
    business's version is one, else the restyled one."""
    for root in (AS_IS, NAMING):
        d = os.path.join(root, REVIEWED.get(form, form))
        if os.path.exists(os.path.join(d, "mapping.json")):
            return (json.load(open(os.path.join(d, "context.json"), encoding="utf-8")),
                    json.load(open(os.path.join(d, "mapping.json"), encoding="utf-8")), d)
    return None, None, None


def carry(folder):
    form = os.path.basename(os.path.normpath(folder))
    ctx = json.load(open(os.path.join(folder, "context.json"), encoding="utf-8"))
    octx, old, where = reviewed(form)
    said = []
    if old is None:
        said.append("no reviewed copy - every place to map by hand")
        return {"as_is": True, "items": {}, "skip": {}, "drop": []}, said, []
    oby = {o["id"]: o for o in octx["items"]}
    # every reviewed place with its rules: mapped context items, and "add"
    # entries (placed by the review, with no context item of their own)
    places = []
    for k, it in old["items"].items():
        o = oby.get(k)
        if o:
            places.append({"page": o["page"], "rect": it.get("rect") or o["rect"], "words": words(o),
                           "tag": o["existing"], "rules": {x: it[x] for x in KEEP if x in it}})
    for a in old.get("add", []):
        places.append({"page": a["page"], "rect": a["rect"], "words": " | ".join(
            [a.get("label", "").lower(), "", "", "", ""]), "tag": None,
            "rules": {x: a[x] for x in KEEP if x in a}})
    by_name = {}
    for p in places:
        by_name.setdefault(p["rules"].get("name"), []).append(p)

    items, checks = {}, []
    for i in ctx["items"]:
        k, name = i["id"], i["existing"]
        if name and name in by_name:
            p = min(by_name[name], key=lambda p: (p["page"] != i["page"], near(p["rect"], i["rect"])))
            items[k] = dict(p["rules"])
            continue
        tagged = [p for p in places if name and p["tag"] == name and p["page"] == i["page"]]
        if tagged:
            p = min(tagged, key=lambda p: near(p["rect"], i["rect"]))
            items[k] = dict(p["rules"])
            continue
        mine = words(i)
        best = max(places, key=lambda p: (difflib.SequenceMatcher(None, mine, p["words"]).ratio(),
                                          -abs(p["page"] - i["page"])), default=None)
        if best is None:
            continue
        score = difflib.SequenceMatcher(None, mine, best["words"]).ratio()
        items[k] = dict(best["rules"])
        checks.append((k, name, i["type"], i["page"] + 1, [round(v) for v in i["rect"]], mine[:70],
                       best["rules"].get("name"), round(score, 2)))
    out = {"as_is": True, "items": items, "skip": {}, "drop": [],
           "notes": old.get("notes", []), "reviewed copy": os.path.relpath(where, HERE)}
    return out, said, checks


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("batch")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    for form in sorted(os.listdir(args.batch)):
        folder = os.path.join(args.batch, form)
        if not os.path.exists(os.path.join(folder, "context.json")):
            continue
        m, said, checks = carry(folder)
        print(f"== {form}: {len(m['items']) - len(checks)} carried, {len(checks)} proposed by their words")
        for s in said:
            print("   ", s)
        for c in checks:
            print(f"    check {c[0]:4} {str(c[1])[:28]:28} {c[2]:8} p{c[3]} {c[4]} '{c[5]}' -> {c[6]} ({c[7]})")
        if args.write:
            with open(os.path.join(folder, "mapping.json"), "w", encoding="utf-8", newline="\n") as fh:
                json.dump(m, fh, indent=1, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
