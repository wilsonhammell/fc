#!/usr/bin/env python3
"""Take every form to 10pt by the lightest means that works.

The two tools trade risk against reach. `restyle_pdf.py` keeps a form's own
geometry and only re-sets its type, so it changes as little as possible - but
a dense form can only hold 10pt Arial if it is allowed to re-wrap, condense,
or be reshaped, and some cannot hold it at all. `redesign.py` sets every form
at 10pt by laying it out again from scratch, which always works and always
costs more: the fields are recreated, the pages re-flow, and a radio group
comes back as tick boxes.

So: try the rungs in order and stop at the first one that reaches 10pt.

    1  plain        re-set the type, re-wrap prose that will not fit
    2  condense     also let a line be set slightly narrower
    3  stubborn     also shrink the labels that are holding a page down
    4  rebuild      lay the form out again from nothing

There used to be a `restructure` rung between the third and the fourth. Run
against all 71 forms it improved the size of none of them - condense and
shrink-stubborn already reach everything it could - so it was a pass that
cost time and bought nothing.

Half the corpus clears rung 1, and a form only pays for what it needs. The
rungs are found with dry runs, which write nothing; then each group is run
for real in one go, all into the same output and proof folders. Every proof
is named with the rung that made it, so a reviewer always knows what they
are looking at.
"""

import argparse
import csv
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
RESTYLE = os.path.join(HERE, "restyle_pdf.py")
REDESIGN = os.path.join(HERE, "redesign.py")

# The rungs, lightest first. Each is the one before it plus one more freedom.
BASE = ["--best-fit", "--reflow", "--page-margin", "36", "--header",
        "--spellcheck", "--relabel", "--skip-third-party"]
RUNGS = [
    ("plain", []),
    ("condense", ["--condense", "0.94"]),
    ("stubborn", ["--condense", "0.94", "--shrink-stubborn"]),
]


RUNG_NO = {"plain": 1, "condense": 2, "stubborn": 3, "rebuild": 4}


def gather(raw, into, rung_of):
    """Collect every proof under one naming, so the folder scrolls in order.

    The tools name a proof for the size it reached, or "rebuilt". Neither
    pads the page number, so a form's page 10 sorts before its page 2, and
    neither says which rung made the file. Both matter when the folder is
    read straight through. A proof made with --proof-scroll holds the whole
    form already and is named "all" instead of a page number."""
    left = []
    for name in sorted(os.listdir(raw)):
        m = re.match(r"^(.*) \[(.+?)\] (?:p(\d+)|all)_proof\.png$", name)
        if not m:
            left.append(name)
            continue
        title, how, page = m.group(1), m.group(2), m.group(3)
        which = rung_of.get(title + ".pdf", "?")
        size = "10pt" if how == "rebuilt" else how
        tag = f"{RUNG_NO.get(which, 9)} {which} {size}"
        where = (f"{title} p{int(page):02d} [{tag}].png" if page
                 else f"{title} [{tag}].png")
        shutil.copy2(os.path.join(raw, name), os.path.join(into, where))
    # A proof the naming does not recognise would otherwise vanish without a
    # word, and a missing proof is the one thing nobody notices.
    if left:
        print(f"WARNING: {len(left)} proof(s) not collected, names unknown: "
              + ", ".join(left[:4]) + (" ..." if len(left) > 4 else ""))


def run(args, **kw):
    return subprocess.run([sys.executable] + args, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", **kw)


def wholly_theirs(path):
    """Is every page of this form someone else's - the CRA's Home Buyers'
    Plan request, both pages under the CRA's letterhead? A file can carry
    another issuer's form in front of Libro's own - the Dairy Farmers'
    direction ahead of Libro's security agreement - and that is still a
    Libro form to rebuild."""
    sys.path.insert(0, HERE)
    import pymupdf as fitz
    import restyle_pdf as R
    marks, issuers = R.house_marks(R.HOUSE_MARKS), R.load_issuers(R.ISSUERS)
    with fitz.open(path) as doc:
        return bool(doc.page_count) and all(R.third_party(p, 100.0, marks, issuers)[0]
                                            for p in doc)


def reached_10pt(report):
    """Which forms in a report held the target size. A form that had to drop
    below it gets a `size_reduced` row; everything else made it."""
    try:
        rows = [r for r in csv.reader(open(report, encoding="utf-8")) if len(r) == 4]
    except OSError:
        return set()
    seen = {r[0] for r in rows}
    return seen - {r[0] for r in rows if r[2] == "size_reduced"}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="folder of PDFs")
    ap.add_argument("-o", "--output", default="forms at 10pt")
    ap.add_argument("--png", default="final proofs",
                    help="one folder for every proof, whichever rung made "
                         "it, named so a form's pages sort in order and "
                         "the rung is on the file")
    ap.add_argument("--report", default="reports/ladder.csv")
    ap.add_argument("--jobs", default="0", help="restyle jobs per run (0 = one per core)")
    ap.add_argument("--signing", action="store_true",
                    help="bring each member signing area to the client's "
                         "standard, on every rung")
    ap.add_argument("--merge-fields", action="store_true",
                    help="take mail-merge tags off the page and put a text box "
                         "in each one's place, on every rung")
    ap.add_argument("--close-gaps", action="store_true",
                    help="on the rungs that keep the form's own layout, run a "
                         "rule back to the caption it labels where the two "
                         "stand apart. The rebuild sets its own rules, so this "
                         "does not reach it")
    ap.add_argument("--proof-scroll", action="store_true",
                    help="one tall proof per form instead of one per page: "
                         "every page joined top to bottom, form on the left, "
                         "result on the right, read in a single scroll")
    ap.add_argument("--top", default="rebuild", choices=["rebuild", "stubborn"],
                    help="how far to climb. 'stubborn' stops short of the "
                         "rebuild and leaves the rest below 10pt")
    ap.add_argument("--review", metavar="DIR",
                    help="also keep every rung a form passed through, one "
                         "folder per form, so a lower rung can be taken when "
                         "the winning one costs more than it is worth")
    args = ap.parse_args()

    forms = sorted(n for n in os.listdir(args.input) if n.lower().endswith(".pdf"))
    if not forms:
        sys.exit(f"no PDFs in {args.input}")
    for d in (args.output, args.png, os.path.dirname(args.report) or "."):
        os.makedirs(d, exist_ok=True)
    for stale in os.listdir(args.png):
        os.remove(os.path.join(args.png, stale))

    scratch = tempfile.mkdtemp(prefix="ladder_")
    # the tools name their proofs their own way; they are gathered and
    # renamed once at the end, so there is one folder to scroll through
    raw = os.path.join(scratch, "proofs")
    os.makedirs(raw, exist_ok=True)
    try:
        # --- which rung does each form need? dry runs write nothing ---
        left, rung_of = set(forms), {}
        for name, extra in RUNGS:
            if not left:
                break
            probe = os.path.join(scratch, "probe_" + name)
            os.makedirs(probe, exist_ok=True)
            for n in sorted(left):
                shutil.copy2(os.path.join(args.input, n), os.path.join(probe, n))
            report = os.path.join(scratch, f"{name}.csv")
            print(f"trying rung '{name}' on {len(left)} form(s)...", flush=True)
            r = run([RESTYLE, probe, "-o", os.path.join(scratch, "void"),
                     "--dry-run", "--report", report, "--jobs", args.jobs]
                    + BASE + extra)
            if r.returncode:
                print(r.stdout[-2000:] or r.stderr[-2000:])
                sys.exit(f"rung '{name}' failed")
            made = reached_10pt(report) & left
            for n in made:
                rung_of[n] = name
            left -= made
            print(f"   {len(made)} form(s) reach 10pt here; {len(left)} still short")
        # A form another issuer puts out, every page of it - the CRA's Home
        # Buyers' Plan request - keeps that issuer's layout. The rebuild lays a form out
        # from nothing, and did so to a government questionnaire, scrambling
        # its tables. Such a form stops at the last restyle rung instead,
        # below 10pt if that is where it lands.
        theirs = {n for n in left if wholly_theirs(os.path.join(args.input, n))}
        kept = sorted(theirs)
        for n in left:
            rung_of[n] = RUNGS[-1][0] if n in theirs else "rebuild"
        if kept:
            print(f"   {len(kept)} third-party form(s) kept off the rebuild: "
                  + ", ".join(kept))

        # --- run each group for real, all into the same two folders ---
        counts = {}
        for name, extra in RUNGS:
            group = sorted(n for n, r in rung_of.items() if r == name)
            counts[name] = len(group)
            if not group:
                continue
            folder = os.path.join(scratch, "run_" + name)
            os.makedirs(folder, exist_ok=True)
            for n in group:
                shutil.copy2(os.path.join(args.input, n), os.path.join(folder, n))
            print(f"\nrung '{name}': {len(group)} form(s)", flush=True)
            r = run([RESTYLE, folder, "-o", args.output, "--png", raw,
                     "--report", os.path.join(
                         os.path.dirname(args.report) or ".", f"rung_{name}.csv"),
                     "--jobs", args.jobs]
                    + (["--proof-scroll"] if args.proof_scroll else [])
                    + (["--close-gaps"] if args.close_gaps else [])
                    + (["--merge-fields"] if args.merge_fields else [])
                    + (["--signing"] if args.signing else [])
                    + BASE + extra)
            print(r.stdout[-700:] if r.stdout else r.stderr[-700:])

        rebuilt = sorted(n for n, r in rung_of.items() if r == "rebuild")
        counts["rebuild"] = len(rebuilt)
        if rebuilt and args.top == "rebuild":
            print(f"\nrung 'rebuild': {len(rebuilt)} form(s)", flush=True)
            for i, n in enumerate(rebuilt, 1):
                r = run([REDESIGN, "--relabel", "--skip-third-party",
                         "--png", raw]
                        + (["--proof-scroll"] if args.proof_scroll else [])
                        + (["--merge-fields"] if args.merge_fields else [])
                        + (["--signing"] if args.signing else [])
                        + [os.path.join(args.input, n),
                           "-o", os.path.join(args.output, n)])
                mark = "ok" if "checks: clean" in (r.stdout or "") else "see report"
                print(f"   [{i}/{len(rebuilt)}] {n[:58]}  {mark}", flush=True)
        elif rebuilt:
            print(f"\n{len(rebuilt)} form(s) left below 10pt (--top restructure)")

        with open(args.report, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["file", "rung"])
            for n in forms:
                w.writerow([n, rung_of[n]])

        gather(raw, args.png, rung_of)
        print("\n" + "=" * 58)
        print(f"{len(forms)} form(s) -> {args.output}")
        for name, _ in RUNGS:
            print(f"   {name:<14} {counts.get(name, 0):>3}")
        print(f"   {'rebuild':<14} {counts.get('rebuild', 0):>3}")
        print(f"proofs: {len(os.listdir(args.png))} image(s) in {args.png}")
        print(f"report: {args.report}")
        kept = sorted(glob.glob(os.path.join(
            os.path.dirname(args.report) or ".", "rung_*.csv")))
        if kept:
            print("         " + ", ".join(os.path.basename(k) for k in kept)
                  + "  (what each rung did, line by line)")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    main()
