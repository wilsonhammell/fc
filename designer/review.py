"""Read a form's fields and record which are required.

    python designer/review.py show "<form>"
    python designer/review.py set "<form>" F12=save F14="custom:Required when ..." F3=off

show lists the form's named fields in page order - id, type, name, kind,
the label beside it, the section it sits in and what goes there - with what
is recorded now (S = Save Validation, C = Custom Save Validation).

set records the reading in the form's mapping.json, on the field:
  save            Save Validation - the form cannot go without it
  custom:<rule>   Custom Save Validation, the rule as its note ("Required
                  when TRACE_REASON is Other"); the developer writes the code
  save_on:A|B     Save Validation on production forms A and B only - one
                  cleaned file can be several (Hypothecation Agreement)
  off             neither
A pick-one field (one name over several boxes) is required as a whole:
marking any one of its boxes marks the field.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAMING = os.path.join(HERE, "field naming")


def load(form):
    folder = os.path.join(NAMING, form)
    ctx = json.load(open(os.path.join(folder, "context.json"), encoding="utf-8"))
    path = os.path.join(folder, "mapping.json")
    return ctx, json.load(open(path, encoding="utf-8")), path


def show(form):
    ctx, mp, _ = load(form)
    by_id = {i["id"]: i for i in ctx["items"]}
    seen = set()
    order = sorted(mp["items"].items(), key=lambda kv: (by_id.get(kv[0], {}).get("page", 0),
                                                       round(by_id.get(kv[0], {}).get("rect", [0, 0])[1] / 4),
                                                       by_id.get(kv[0], {}).get("rect", [0])[0]))
    for fid, it in order:
        i = by_id.get(fid, {})
        mark = "S" if it.get("save") else ("C" if it.get("custom") else ("s" if it.get("save_on") else " "))
        again = "  (again)" if it["name"] in seen else ""
        seen.add(it["name"])
        kind = (i.get("type") or "")[:5]
        sec = " ".join((i.get("section") or "").split())[:28]
        lab = " ".join((it.get("label") or i.get("left") or "").split())[:30]
        desc = " ".join((it.get("desc") or "").split())[:60]
        opt = f" /{it['option']}" if it.get("option") else ""
        print(f"{mark} {fid:4} p{i.get('page', 0) + 1} {kind:5} {it['name'][:30] + opt:34} {it['kind'][:5]:5} "
              f"| {lab:30} | {sec:28} | {desc}{again}")
        if it.get("custom"):
            print(f"{'':12}custom: {it['custom']}")


def set_(form, pairs):
    ctx, mp, path = load(form)
    for p in pairs:
        fid, _, what = p.partition("=")
        it = mp["items"].get(fid)
        if it is None:
            print(f"   {fid}: not in the mapping")
            continue
        # a pick-one field is one field: the mark goes on every box of it
        mates = [k for k, v in mp["items"].items() if v["name"] == it["name"] and v.get("option")] or [fid]
        for k in mates:
            v = mp["items"][k]
            v.pop("save", None)
            v.pop("custom", None)
            v.pop("save_on", None)
            if what == "save":
                v["save"] = True
            elif what.startswith("save_on:"):
                v["save_on"] = [n.strip() for n in what[len("save_on:"):].split("|")]
            elif what.startswith("custom:"):
                v["custom"] = what[len("custom:"):].strip()
            elif what != "off":
                print(f"   {fid}: {what!r} is not save, custom:<rule> or off")
    json.dump(mp, open(path, "w", encoding="utf-8"), indent=1, ensure_ascii=False)


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")     # a console in cp1252 and a label with a tick box
    if sys.argv[1] == "show":
        show(sys.argv[2])
    elif sys.argv[1] == "set":
        set_(sys.argv[2], sys.argv[3:])
