"""Build "forms for review" from the pilot list.

    python make_review_folder.py [--list pilot_forms.txt] [--forms "forms at 10pt"]
                                 [--proofs "final proofs"] [-o "forms for review"]

Copies, for each input file named in pilot_forms.txt, its form at 10pt and
its proof into two folders under the output folder, which is emptied first.
Every production name with no input file, and every input file with no
output yet, is listed at the end - nothing is left out without a word.
"""
import argparse
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def read_list(path):
    """[(production name, input file stem or "")] in the list's order."""
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=>" not in line:
                continue
            name, stem = (part.strip() for part in line.split("=>", 1))
            out.append((name, stem))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", default=os.path.join(HERE, "pilot_forms.txt"))
    ap.add_argument("--forms", default=os.path.join(HERE, "forms at 10pt"))
    ap.add_argument("--proofs", default=os.path.join(HERE, "final proofs"))
    ap.add_argument("-o", "--output", default=os.path.join(HERE, "forms for review"))
    args = ap.parse_args()

    entries = read_list(args.list)
    missing = [name for name, stem in entries if not stem]
    stems = list(dict.fromkeys(stem for _, stem in entries if stem))

    if os.path.isdir(args.output):
        shutil.rmtree(args.output)
    forms_out = os.path.join(args.output, "forms at 10pt")
    proofs_out = os.path.join(args.output, "proofs")
    os.makedirs(forms_out)
    os.makedirs(proofs_out)

    proofs = os.listdir(args.proofs) if os.path.isdir(args.proofs) else []
    # file names are matched ignoring spacing and case: several input files
    # carry a space before ".pdf" ("Direction of Chicken Quota .pdf")
    key = lambda t: " ".join(t.split()).casefold()
    on_disk = {key(os.path.splitext(f)[0]): f for f in os.listdir(args.forms)
               if f.lower().endswith(".pdf")}
    no_form, no_proof = [], []
    for stem in stems:
        found = on_disk.get(key(stem))
        if found:
            shutil.copy2(os.path.join(args.forms, found), forms_out)
            stem = os.path.splitext(found)[0]
        else:
            no_form.append(stem)
        # a proof is "<stem> [rung]" or "<stem> pNN [rung]"
        mine = [p for p in proofs if p.startswith(stem + " [") or
                (p.startswith(stem + " p") and p[len(stem) + 2:len(stem) + 4].isdigit())]
        for p in mine:
            shutil.copy2(os.path.join(args.proofs, p), proofs_out)
        if not mine and found:
            no_proof.append(stem.strip())

    # the two lists, for whoever opens the folder: what each pilot form is
    # made from, and which pilot forms have nothing to make them from yet
    width = max(len(name) for name, _ in entries)
    with open(os.path.join(args.output, "form mappings.txt"), "w", encoding="utf-8") as fh:
        fh.write("Pilot form (production name)".ljust(width) + "   File in this folder\n")
        fh.write("-" * (width + 3 + 40) + "\n")
        for name, stem in entries:
            if stem:
                f_ = on_disk.get(key(stem))
                fh.write(f"{name.ljust(width)}   {f_ if f_ else stem + '.pdf (not produced)'}\n")
    with open(os.path.join(args.output, "missing forms.txt"), "w", encoding="utf-8") as fh:
        fh.write(f"Pilot forms with no source file yet ({len(missing)}):\n\n")
        for name in missing:
            fh.write(f"{name}\n")

    print(f"{len(entries)} pilot form(s) listed; {len(stems)} input file(s) "
          f"copied to {args.output}")
    if missing:
        print(f"\n{len(missing)} pilot form(s) with no input file yet:")
        for name in missing:
            print(f"   {name}")
    for what, names in (("no form at 10pt", no_form), ("no proof", no_proof)):
        if names:
            print(f"\nWARNING: {len(names)} listed file(s) with {what}:")
            for n in names:
                print(f"   {n}")
    return 1 if (no_form or no_proof) else 0


if __name__ == "__main__":
    sys.exit(main())
