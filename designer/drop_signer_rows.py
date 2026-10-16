"""Take the signer rows off the forms nobody signs electronically.

    python designer/drop_signer_rows.py

The business's PDF Standardization sheet: "Print Only or Submit for
Processing > Does NOT require Member Signature section". A form whose
workbook row offers no Phone Auth, In-Branch eSign or Remote eSign loses its
Member{i}Name / Signature / SignDate fields (client, 2026-10-16). Its printed
lines stay: a printed form is still signed in pen.

Edits each such form's field naming/<form>/mapping.json: a signer field the
form already had goes on the drop list, a blank that was to become one is
skipped with the reason. Says what it took off.
"""
import json
import os
import re

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIGNER = re.compile(r"^Member\d+(Name|Signature|SignDate)$")
CHANNELS = ("PhoneAuthButton", "DigitalSignatureButton", "RemoteESignButton")
WHY = ("signer row taken off: the form table offers no Phone Auth or eSign, and the "
       "standard says a Print Only / Submit for Processing form needs no member signature section")


def main():
    matches = json.load(open(os.path.join(HERE, "designer", "form_matches.json"), encoding="utf-8"))
    done = set()
    for m in matches:
        if m["match"] != "exact" or not m["folder"] or m["folder"] in done:
            continue
        if any(m["buttons"].get(c) for c in CHANNELS):
            continue
        done.add(m["folder"])
        path = os.path.join(HERE, "field naming", m["folder"], "mapping.json")
        mp = json.load(open(path, encoding="utf-8"))
        ctx = json.load(open(os.path.join(os.path.dirname(path), "context.json"), encoding="utf-8"))
        existing = {i["id"]: i["existing"] for i in ctx["items"]}
        drop, skip = list(mp.get("drop", [])), dict(mp.get("skip", {}))
        gone = []
        for fid, it in list(mp["items"].items()):
            if not SIGNER.match(it["name"]):
                continue
            del mp["items"][fid]
            if existing.get(fid) is not None:
                drop.append(fid)
            else:
                skip[fid] = WHY
            gone.append(f"{fid} {it['name']}")
        mp["drop"], mp["skip"] = drop, skip
        json.dump(mp, open(path, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
        print(f"{m['folder'][:55]:55} {len(gone):2} signer field(s) off" + (": " + ", ".join(gone[:6]) if gone else ""))


if __name__ == "__main__":
    main()
