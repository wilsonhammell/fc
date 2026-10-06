#!/usr/bin/env python3
"""Keep every rung a form passed through, so a lower one can be taken.

The ladder picks the lightest rung that reaches 10pt, but the step that
finally gets there can cost more than it is worth - a page, a re-laid
layout, a table drawn again from scratch - for half a point of type. That
trade is only visible if both sides of it are in front of you.

So for every form that does not clear the first rung, each rung it passed
through is built for real and kept in a folder of its own, with its proofs
and a CHOICES.txt saying what each one cost.
"""

import csv
import glob
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RESTYLE = os.path.join(HERE, "restyle_pdf.py")
REDESIGN = os.path.join(HERE, "redesign.py")

BASE = ["--best-fit", "--reflow", "--page-margin", "36", "--header",
        "--spellcheck", "--relabel", "--skip-third-party"]
RUNGS = [
    ("plain", []),
    ("condense", ["--condense", "0.94"]),
    ("stubborn", ["--condense", "0.94", "--shrink-stubborn"]),
]
REBUILD = "rebuild"
ORDER = [name for name, _ in RUNGS] + [REBUILD]

# what a report row means in plain words, and how to count it
TOLD = [
    ("reflowed", "re-wrapped %d paragraph(s)"),
    ("line_condensed", "%d line(s) set narrower"),
    ("paragraph_condensed", "%d paragraph(s) set narrower"),
    ("line_kept_smaller", "%d label(s) kept smaller than the body"),
    ("blank_moved", "%d blank(s) moved"),
    ("blank_shortened", "%d blank(s) shortened"),
    ("reflow_blocked", "%d line(s) it could not re-wrap"),
    ("relabelled", "%d label(s) swapped for house wording"),
]
SHOUT = re.compile(r"^[A-Z][A-Z_]{3,}$")
# reported as a count above, or expected on every run; not worth shouting
QUIET = {"RELABELLED", "RELABEL_REVIEW", "MERGE_TAG_TO_FIELD", "GAPS_CLOSED_UP",
         "SIGNING_AREA_LEFT", "SIGNING_AREA_STANDARD", "SIGNING_FIELDS"}


def run(args):
    return subprocess.run([sys.executable] + args, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def read_report(path):
    """{file: [(issue, detail), ...]} from a restyle report."""
    out = {}
    try:
        rows = [r for r in csv.reader(open(path, encoding="utf-8")) if len(r) == 4]
    except OSError:
        return out
    for name, _page, issue, detail in rows:
        out.setdefault(name, []).append((issue, detail))
    return out


def size_of(rows):
    """The size a form settled on, from its report rows."""
    for issue, detail in rows:
        if issue == "size_reduced":
            m = re.search(r"-> ([\d.]+)pt", detail)
            if m:
                return float(m.group(1))
    return 10.0


def summarise(rows):
    """What this rung had to do, in words."""
    tally = {}
    for issue, _ in rows:
        tally[issue] = tally.get(issue, 0) + 1
    said = [text % tally[key] for key, text in TOLD if tally.get(key)]
    shouted = sorted({i for i, _ in rows if SHOUT.match(i)} - QUIET)
    return said, shouted


def pages_of(path):
    import pymupdf as fitz
    d = fitz.open(path)
    n = d.page_count
    d.close()
    return n


def fields_of(path):
    import pymupdf as fitz
    d = fitz.open(path)
    n = sum(1 for p in d for _ in p.widgets())
    d.close()
    return n


def stem(name):
    return os.path.splitext(name)[0]


def folder_name(name):
    """A folder Windows will actually make.

    A name cannot end in a space or a dot - "Direction of Chicken Quota .pdf"
    does - and the whole path has a length limit, so a very long form name is
    cut short. The form's real name is written inside CHOICES.txt either
    way."""
    safe = stem(name).rstrip(" .")
    for ch in '<>:"/' + chr(92) + '|?*':
        safe = safe.replace(ch, "-")
    return safe[:70].rstrip(" .") or "form"


def collect(src_dir, name, rung_no, rung_name, made_pdf, proof_dir, into):
    """Move one rung's output and proofs into the form's review folder."""
    size = None
    hits = sorted(glob.glob(os.path.join(proof_dir, glob.escape(stem(name)) + " [[]*")))
    for path in hits:
        m = re.search(r"\[([\d.]+)pt\]|\[rebuilt\]", os.path.basename(path))
        if m and m.group(1):
            size = m.group(1)
            break
    tag = f"{rung_no} {rung_name}"
    if os.path.exists(made_pdf):
        pages = pages_of(made_pdf)
        # every file starts with the form's name, so one taken out of its
        # folder - attached to an email, dropped in with others - still says
        # which form it is
        label = f"{folder_name(name)} {tag} [{size or '10'}pt] {pages}p"
        shutil.copy2(made_pdf, os.path.join(into, label + ".pdf"))
        for path in hits:
            base = os.path.basename(path)
            page = re.search(r"p(\d+)_proof\.png$", base)
            # a --proof-scroll proof is the whole form in one file
            tail = f"p{page.group(1)}" if page else (
                "all" if base.endswith("all_proof.png") else "p1")
            shutil.copy2(path, os.path.join(into, f"{label} {tail}_proof.png"))
        return pages
    return None


def main():
    passed = ("--proof-scroll", "--close-gaps", "--merge-fields", "--signing")
    argv = [a for a in sys.argv[1:] if a not in passed]
    stack = [a for a in passed if a in sys.argv]
    # the rebuild lays its own rules, so gap closing never reaches it
    only_stack = [a for a in stack if a != "--close-gaps"]
    if len(argv) < 2:
        sys.exit("usage: review_rungs.py <input folder> <review folder> "
                 "[ladder.csv] [--proof-scroll] [--close-gaps] [--merge-fields] [--signing]")
    src, out = argv[0], argv[1]
    chosen_csv = argv[2] if len(argv) > 2 else "reports/ladder.csv"
    chosen = {r["file"]: r["rung"]
              for r in csv.DictReader(open(chosen_csv, encoding="utf-8"))}
    # Every form gets a folder, including the ones that clear the first
    # rung with nothing to compare. A reviewer works through one folder
    # per form; having a third of them missing means checking two places
    # and keeping track of which forms live where.
    need = dict(chosen)
    if not need:
        print(f"no forms listed in {chosen_csv}")
        return
    os.makedirs(out, exist_ok=True)
    work = os.path.join(out, "_work")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    more = sum(1 for r in need.values() if r != "plain")
    print(f"{len(need)} form(s); {more} need more than the first rung\n")

    per_form = {f: {} for f in need}
    reports = {}
    try:
        for i, (rung_name, extra) in enumerate(RUNGS, start=1):
            group = [f for f, r in need.items()
                     if ORDER.index(r) >= ORDER.index(rung_name)]
            if not group:
                continue
            folder = os.path.join(work, f"in{i}")
            made = os.path.join(work, f"out{i}")
            proofs = os.path.join(work, f"png{i}")
            os.makedirs(folder, exist_ok=True)
            for f in group:
                shutil.copy2(os.path.join(src, f), os.path.join(folder, f))
            report = os.path.join(work, f"{rung_name}.csv")
            print(f"rung {i} {rung_name}: {len(group)} form(s)", flush=True)
            r = run([RESTYLE, folder, "-o", made, "--png", proofs,
                     "--report", report, "--jobs", "0"]
                    + BASE + extra + stack)
            if "failed" in (r.stdout or "") and "0 failed" not in (r.stdout or ""):
                print(r.stdout[-600:])
            reports[rung_name] = read_report(report)
            for f in group:
                into = os.path.join(out, folder_name(f))
                os.makedirs(into, exist_ok=True)
                pages = collect(src, f, i, rung_name,
                                os.path.join(made, f), proofs, into)
                per_form[f][rung_name] = pages

        rebuilt = [f for f, r in need.items() if r == REBUILD]
        if rebuilt:
            print(f"rung {len(RUNGS) + 1} rebuild: {len(rebuilt)} form(s)", flush=True)
            for n, f in enumerate(rebuilt, 1):
                made = os.path.join(work, "outR")
                proofs = os.path.join(work, "pngR")
                os.makedirs(made, exist_ok=True)
                os.makedirs(proofs, exist_ok=True)
                r = run([REDESIGN, "--relabel", "--skip-third-party",
                         "--png", proofs] + only_stack
                        + [os.path.join(src, f),
                           "-o", os.path.join(made, f)])
                notes = [l.strip() for l in (r.stdout or "").splitlines()
                         if re.match(r"^[A-Z][A-Z_]{3,}:", l.strip())]
                reports.setdefault(REBUILD, {})[f] = [
                    (l.split(":", 1)[0], l.split(":", 1)[1].strip()) for l in notes]
                into = os.path.join(out, folder_name(f))
                os.makedirs(into, exist_ok=True)
                per_form[f][REBUILD] = collect(
                    src, f, len(RUNGS) + 1, REBUILD,
                    os.path.join(made, f), proofs, into)
                for path in glob.glob(os.path.join(proofs, "*")):
                    os.remove(path)
                print(f"   [{n}/{len(rebuilt)}] {f[:56]}", flush=True)

        # --- the note that says what each rung cost ---
        for f, won in sorted(need.items()):
            into = os.path.join(out, folder_name(f))
            was_pages = pages_of(os.path.join(src, f))
            was_fields = fields_of(os.path.join(src, f))
            lines = [f"{f}",
                     f"original: {was_pages} page(s), {was_fields} field(s)",
                     "",
                     f"  {'rung':<14}{'size':>6}{'pages':>7}   what it had to do",
                     f"  {'-' * 60}"]
            for i, name in enumerate(ORDER, start=1):
                if name not in per_form[f]:
                    continue
                rows = reports.get(name, {}).get(f, [])
                pages = per_form[f][name] or was_pages
                if name == REBUILD:
                    size, said = 10.0, ["laid out again from nothing"]
                    if pages != was_pages:
                        said.append(f"{pages - was_pages:+d} page(s)")
                    shouted = sorted({i for i, _ in rows} - QUIET)
                else:
                    size = size_of(rows)
                    said, shouted = summarise(rows)
                if name == REBUILD:
                    swaps = sum(1 for i, _ in rows if i == "RELABELLED")
                    if swaps:
                        said.append(f"{swaps} label(s) swapped for house wording")
                head = f"  {i} {name:<12}{size:>5.1f}pt{pages:>6}   "
                print_lines = said or ["nothing beyond re-setting the type"]
                lines.append(head + print_lines[0])
                for extra in print_lines[1:]:
                    lines.append(" " * len(head) + "+ " + extra)
                for s in shouted:
                    lines.append(" " * len(head) + s)
            if len(per_form[f]) == 1:
                lines += ["", "  this form holds 10pt on the first rung, "
                              "so there is nothing to weigh up."]
            else:
                lines += ["", f"  chosen: {ORDER.index(won) + 1} {won}, the "
                              "lightest rung that reaches 10pt."]
            below = (ORDER[ORDER.index(won) - 1]
                     if ORDER.index(won) and len(per_form[f]) > 1 else None)
            if below and below in per_form[f]:
                gap = 10.0 - size_of(reports.get(below, {}).get(f, []))
                cost = (per_form[f].get(won) or was_pages) - (per_form[f][below] or was_pages)
                lines.append(f"  the step from {ORDER.index(below) + 1} {below} "
                             f"buys {gap:.1f}pt"
                             + (f" and costs {cost:+d} page(s)." if cost else "."))
            with open(os.path.join(into, f"{folder_name(f)} CHOICES.txt"), "w",
                      encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    folders = [d for d in sorted(os.listdir(out))
               if os.path.isdir(os.path.join(out, d))]
    files = sum(len(os.listdir(os.path.join(out, d))) for d in folders)
    print(f"\n{len(folders)} folder(s), {files} file(s) in {out}")


if __name__ == "__main__":
    main()
