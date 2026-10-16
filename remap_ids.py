"""Carry a form's mapping across to a fresh context.json whose ids moved.

    python remap_ids.py <old context.json> <form's field naming folder>

field_context.py numbers a form's fields F1.. and its blanks B1.. in page
order, so taking a block off a form (the standard signing block it no longer
gets) renumbers everything after it, and mapping.json, keyed by those ids,
would name the wrong boxes. This pairs each old item with its new one and
rewrites mapping.json's keys (items, drop, skip) to the new ids, in passes:

  1. the same place: same page and kind, rects within 2pt, and for a field
     the same name - or the name it now has, a radio group's boxes having
     come back under the group's own ("Group4.Choice1" now "Group4")
  2. a field by its own name, in reading order - a form that moved to
     another rung (ATM & POS Trace, rebuilt before, plain now) has every box
     somewhere else, but its fields keep their names
  3. a blank by its type and the words beside it, in reading order

An old id with nothing for it is taken out of the mapping and listed; so is
a new item no mapping entry reaches. Nothing else is guessed.
"""
import json
import os
import re
import sys


def close(a, b, tol=2.0):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def words(text):
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def reading(item):
    return (item["page"], round(item["rect"][1] / 3), item["rect"][0])


def pair(old_items, new_items):
    new_of, taken = {}, set()

    def same_name(o, n):
        if o["existing"] is None:
            return True
        return n["existing"] == o["existing"] or n["existing"] == o["existing"].rsplit(".", 1)[0]

    # 1. the same place
    for o in old_items:
        cands = [n for n in new_items if n["id"] not in taken
                 and n["page"] == o["page"] and n["id"][0] == o["id"][0]
                 and same_name(o, n) and close(n["rect"], o["rect"])]
        if len(cands) == 1:
            new_of[o["id"]] = cands[0]["id"]
            taken.add(cands[0]["id"])

    def in_order(key):
        groups_o, groups_n = {}, {}
        for o in sorted(old_items, key=reading):
            if o["id"] not in new_of and key(o) is not None:
                groups_o.setdefault(key(o), []).append(o)
        for n in sorted(new_items, key=reading):
            if n["id"] not in taken and key(n) is not None:
                groups_n.setdefault(key(n), []).append(n)
        for k, os_ in groups_o.items():
            ns = groups_n.get(k, [])
            if len(os_) == len(ns):           # paired only when the counts agree
                for o, n in zip(os_, ns):
                    new_of[o["id"]] = n["id"]
                    taken.add(n["id"])

    # 2. a field by its name; 3. a blank by its type and the words beside it
    in_order(lambda i: ("F", i["existing"]) if i["id"][0] == "F" and i["existing"] else None)
    # (a radio group's boxes, by the group's name: "Group1.Choice1" is a box
    # of what is "Group1" now)
    in_order(lambda i: ("F", i["existing"].split(".")[0]) if i["id"][0] == "F" and i["existing"] else None)
    in_order(lambda i: ("B", i["type"], words(i.get("left")), words(i.get("above")))
             if i["id"][0] == "B" else None)
    return new_of, []


def main(old_path, folder):
    old = json.load(open(old_path, encoding="utf-8"))["items"]
    new = json.load(open(os.path.join(folder, "context.json"), encoding="utf-8"))["items"]
    path = os.path.join(folder, "mapping.json")
    mp = json.load(open(path, encoding="utf-8"))
    new_of, doubt = pair(old, new)
    if all(k == v for k, v in new_of.items()) and len(new_of) == len(old) == len(new):
        print(f"{os.path.basename(folder)}: ids unchanged")
        return 0
    gone, out = [], dict(mp)
    items = {}
    for k, v in mp.get("items", {}).items():
        if k in new_of:
            items[new_of[k]] = v
        else:
            gone.append(f"{k} {v.get('name', '')}")
    out["items"] = items
    out["drop"] = [new_of[k] for k in mp.get("drop", []) if k in new_of]
    gone += [k for k in mp.get("drop", []) if k not in new_of]
    out["skip"] = {new_of[k]: v for k, v in mp.get("skip", {}).items() if k in new_of}
    gone += [k for k in mp.get("skip", {}) if k not in new_of]
    reached = set(out["items"]) | set(out["drop"]) | set(out["skip"])
    loose = [f"{n['id']} ({n['existing'] or n['type']}, page {n['page'] + 1})"
             for n in new if n["id"] not in reached]
    json.dump(out, open(path, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    moved = sum(1 for k, v in new_of.items() if k != v)
    print(f"{os.path.basename(folder)}: {moved} id(s) renumbered")
    for g in gone:
        print(f"   no longer on the form, taken out of the mapping: {g}")
    for d in doubt:
        print(f"   DOUBT: {d} has two items in its place - left out")
    for l in loose:
        print(f"   NOT MAPPED: {l}")
    return 1 if doubt or loose else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
