#!/usr/bin/env python3
"""Name a form's fields, and place the ones it lacks, from its mapping.

    python apply_fields.py "field naming/RRSP Application" [...]

The second half of field naming. field_context.py numbered every field and
every blank on the form; mapping.json, written from that by hand, says what
each one is:

    {"items": {"F3": {"name": "PRIMARY_MEMBER_NUMBER", "kind": "db",
                      "label": "Member Number", "desc": "the member's number"},
               "B30": {"name": "POS_TRACER_NUMBER", "kind": "entry", ...}},
     "drop": ["F11"],
     "skip": {"B1": "the spouse account box, moved here as F18"},
     "add": [{"page": 0, "rect": [186, 328, 237, 339], "name": "LOAN_BALANCE",
              "kind": "candidate", "label": "...", "desc": "...", "type": "text"}],
     "notes": ["what a person has to settle that no field can"]}

"add" places a field where nothing was found to put one - a value the form
prints as sample text, a box the layout never drew.

kind is one of
    db         a name in dbFields.json - the filler loads it
    special    the designer's signer and joint fields (Member1Name, JointMembers)
    candidate  a value the DB probably holds that dbFields.json does not list yet
    entry      typed by the member or staff; nothing loads it

A field already on the form is renamed (and rebuilt where its type has to
change - a signer name is a dropdown); a blank gets a new field of the type
asked for, Arial (Helvetica) 10 like the designer's default. Writes, beside
the mapping:

    <form>.pdf       the form with its fields named
    fields.csv       one row per field: page, name, kind, the label beside it,
                     what goes there
    named pN.png     each page with each field's name over it

and refuses nothing silently: an item the mapping leaves out, a "db" name not
in the list, a plain name that IS in the list, are all said, and in fields.csv.
"""
import argparse
import csv
import json
import os
import re
import sys

import fitz

HERE = os.path.dirname(os.path.abspath(__file__))
DB = {d["identifier"] for d in json.load(open(os.path.join(HERE, "dbFields.json"), encoding="utf-8"))}
FORMS = os.path.join(HERE, "final selection at 10pt")

SPECIAL = re.compile(r"^(Member[1-6](Name|Signature|SignDate)|JointMember[1-5](Name|Number)|JointMembers)$")
KINDS = ("db", "special", "candidate", "entry")
# what a field name may be made of: the filler matches it exactly, and the
# designer's export values allow no more
NAME_OK = re.compile(r"^[A-Za-z0-9_]+$")

TYPES = {"text": fitz.PDF_WIDGET_TYPE_TEXT, "checkbox": fitz.PDF_WIDGET_TYPE_CHECKBOX,
         "dropdown": fitz.PDF_WIDGET_TYPE_COMBOBOX}
HAS = {fitz.PDF_WIDGET_TYPE_TEXT: "text", fitz.PDF_WIDGET_TYPE_CHECKBOX: "checkbox",
       fitz.PDF_WIDGET_TYPE_COMBOBOX: "dropdown", fitz.PDF_WIDGET_TYPE_RADIOBUTTON: "radio",
       fitz.PDF_WIDGET_TYPE_SIGNATURE: "signature", fitz.PDF_WIDGET_TYPE_LISTBOX: "listbox",
       fitz.PDF_WIDGET_TYPE_BUTTON: "button"}

COLOURS = {"db": (0.1, 0.55, 0.2), "special": (0.0, 0.5, 0.55),
           "candidate": (0.9, 0.45, 0.0), "entry": (0.35, 0.35, 0.35)}


def type_for(name, asked):
    """What a field has to be: the designer fixes the type of its own names
    (a signer's name is a dropdown it fills at run time, the rest text)."""
    if re.match(r"^Member\d+Name$", name):
        return "dropdown"
    if SPECIAL.match(name):
        return "text"
    return asked


def problems(item_id, m):
    out = []
    name, kind = m.get("name", ""), m.get("kind")
    if kind not in KINDS:
        out.append(f"{item_id}: kind {kind!r} is not one of {', '.join(KINDS)}")
    if not NAME_OK.match(name or ""):
        out.append(f"{item_id}: name {name!r} has characters the filler can't match")
    if kind == "db" and name not in DB:
        out.append(f"{item_id}: {name} is marked db but is not in dbFields.json")
    if kind == "special" and not SPECIAL.match(name):
        out.append(f"{item_id}: {name} is marked special but is no signer or joint name")
    if kind in ("candidate", "entry") and name in DB:
        out.append(f"{item_id}: {name} is marked {kind} but IS in dbFields.json")
    return out


SCRIPTS = ("script", "script_calc", "script_format", "script_change", "script_stroke",
           "script_blur", "script_focus")


def scripts_of(w):
    """A widget's scripts - its calculation, format and keystroke code - so a
    field rebuilt in place keeps them. GIC Authorization's maturity fields fill
    one another from a calculation script, and a rebuilt field without it is a
    form that quietly stops working."""
    return {k: getattr(w, k) for k in SCRIPTS if getattr(w, k, None)}


def box_group(doc, xref):
    """The group a check box hangs under, or None: a parent that is only a
    name over its boxes (no type of its own), as restyle_pdf's
    radios_to_checkboxes makes them."""
    par = doc.xref_get_key(xref, "Parent")
    if par[0] != "xref":
        return None
    px = int(par[1].split()[0])
    if doc.xref_get_key(px, "FT")[0] != "null":
        return None
    t = doc.xref_get_key(px, "T")
    return t[1] if t[0] == "string" else None


def add_field(page, rect, name, kind_of, choices=(), scripts=None):
    if kind_of not in TYPES:
        kind_of = "text"
    w = fitz.Widget()
    w.field_type = TYPES[kind_of]
    w.rect = fitz.Rect(rect)
    w.field_name = name
    if kind_of != "checkbox":
        w.text_font, w.text_fontsize = "Helv", 10
    if kind_of == "dropdown":
        # a signer's is filled from DNA when the form opens and the designer
        # gives it no options; any other keeps the ones the form had
        w.choice_values = [c for c in choices if c] or [" "]
    for k, v in (scripts or {}).items():
        setattr(w, k, v)
    w.border_color, w.border_width = (0.6, 0.6, 0.6), 0.5
    page.add_widget(w)


# a field named in a script: getField("Name"), or one of AFSimple_Calculate's
# quoted names; and a name built in code, getField("Rate" + i)
NAMED = re.compile(r"""getField\(\s*(["'])(.+?)\1\s*\)|(["'])([^"'\n]{1,80})\3""")
BUILT = re.compile(r"""getField\(\s*(["'])([^"']+)\1\s*\+""")


def follow_renames(doc, renamed, original_names):
    """Point every script at the fields' new names, and say what it cannot.

    A script names the fields it reads by their exact quoted names, so a
    rename is followed by rewriting those quotes - only for old names no field
    still carries. A name a script builds in code ("Rate" + i) cannot be
    followed: the fields it means keep their names or the script breaks, and
    a mapping that renamed them is reported."""
    said = []
    now = {w.field_name for p in doc for w in p.widgets()}
    gone = {o: n for o, n in renamed.items() if o not in now and o != n}
    for page in doc:
        for w in page.widgets():
            changed = False
            for k in SCRIPTS:
                s = getattr(w, k, None)
                if not s:
                    continue
                new = s
                for old, nn in gone.items():
                    new = re.sub(r"""(["'])""" + re.escape(old) + r"""\1""",
                                 lambda m: m.group(1) + nn + m.group(1), new)
                if new != s:
                    setattr(w, k, new)
                    changed = True
                for m in BUILT.finditer(new):
                    pre = m.group(2)
                    lost = sorted(o for o in original_names if re.fullmatch(re.escape(pre) + r"\d+", o)
                                  and o not in now)
                    if lost:
                        said.append(f"{w.field_name}: a script builds names from {pre!r} + a number, "
                                    f"and {', '.join(lost[:4])} were renamed - the script breaks")
                for m in NAMED.finditer(new):
                    ref = m.group(2) or m.group(4)
                    if ref in original_names and ref not in now and ref not in gone:
                        said.append(f"{w.field_name}: a script still names {ref!r}, which is no longer on the form")
            if changed:
                w.update()
    return sorted(set(said))


def prune_fields(doc):
    """Take off the form's field list every entry with no widget left under
    it. A widget renamed by being rebuilt leaves the field it hung from - the
    original's "[primary_applicant" over "fullname1" over "label2" - with no
    kids, and a field list naming it shows the designer a field that is not
    on any page. Returns how many went."""
    acro = doc.xref_get_key(doc.pdf_catalog(), "AcroForm")
    if acro[0] != "xref":
        return 0
    ax = int(acro[1].split()[0])
    kind, val = doc.xref_get_key(ax, "Fields")
    if kind not in ("array", "xref"):
        return 0
    if kind == "xref":
        val = doc.xref_object(int(val.split()[0]))
    live = {w.xref for p in doc for w in p.widgets()}

    def has_live(x, depth=0):
        if x in live:
            return True
        if depth > 16:
            return False
        k, v = doc.xref_get_key(x, "Kids")
        if k == "xref":
            v = doc.xref_object(int(v.split()[0]))
        return any(has_live(int(c), depth + 1) for c in re.findall(r"(\d+) 0 R", v if k in ("array", "xref") else ""))

    refs = [int(x) for x in re.findall(r"(\d+) 0 R", val)]
    keep = [x for x in refs if has_live(x)]
    if len(keep) != len(refs):
        doc.xref_set_key(ax, "Fields", "[" + " ".join(f"{x} 0 R" for x in keep) + "]")
    return len(refs) - len(keep)


def merge_repeats(path):
    """A name placed more than once becomes ONE field with a widget per
    placement, the shape Acrobat and the designer both use: one value, filled
    once, shown everywhere. Written as placed, each copy was a field of its
    own that merely shared the name, which the PDF rules don't allow. The
    parent carries the name, type and font; each widget keeps its own place."""
    doc = fitz.open(path)
    cat = doc.pdf_catalog()
    groups = {}
    for page in doc:
        for w in page.widgets():
            groups.setdefault(w.field_name, []).append(w.xref)
    # (a radio group is already one field over its options: its widgets
    # have their parent, and wrapping them in another broke the group)
    repeated = {n: xs for n, xs in groups.items() if len(xs) > 1
                and not any(doc.xref_get_key(x, "Parent")[0] == "xref" for x in xs)}
    if not repeated:
        return 0
    fields_ref = doc.xref_get_key(cat, "AcroForm/Fields")
    held = doc.xref_object(int(fields_ref[1].split()[0])) if fields_ref[0] == "xref" else fields_ref[1]
    refs = [int(x) for x in re.findall(r"(\d+) 0 R", held)]
    for name, xrefs in repeated.items():
        first = xrefs[0]
        parent = doc.get_new_xref()
        ft = doc.xref_get_key(first, "FT")[1]
        da = doc.xref_get_key(first, "DA")
        ff = doc.xref_get_key(first, "Ff")
        body = f"<</T({name})/FT{ft}/Kids[{' '.join(f'{x} 0 R' for x in xrefs)}]"
        if da[0] == "string":
            body += f"/DA({da[1]})"
        if ff[0] != "null":
            body += f"/Ff {ff[1]}"
        doc.update_object(parent, body + ">>")
        for x in xrefs:
            # the field's own entries come off the widget altogether - set to
            # null they stay in it as nulls, which pypdf refuses to read
            kept = [ln for ln in doc.xref_object(x).splitlines()
                    if not re.match(r"\s*/(T|FT|DA|Ff|V|Parent)\b", ln)]
            doc.update_object(x, "\n".join(kept))
            doc.xref_set_key(x, "Parent", f"{parent} 0 R")
        refs = [r for r in refs if r not in xrefs] + [parent]
    # set on the AcroForm itself: through the catalog's "AcroForm/Fields" path,
    # where the AcroForm is an object of its own, PyMuPDF writes the string
    # "fitz: replace me!" in place of the array - eight forms lost their
    # field list that way and pypdf could not read them
    new_fields = "[" + " ".join(f"{r} 0 R" for r in refs) + "]"
    acro = doc.xref_get_key(cat, "AcroForm")
    if acro[0] == "xref":
        doc.xref_set_key(int(acro[1].split()[0]), "Fields", new_fields)
    else:
        doc.xref_set_key(cat, "AcroForm/Fields", new_fields)
    doc.save(path + ".tmp", garbage=3, deflate=True)
    doc.close()
    os.replace(path + ".tmp", path)
    return len(repeated)


def apply(folder):
    stem = os.path.basename(os.path.normpath(folder))
    ctx = json.load(open(os.path.join(folder, "context.json"), encoding="utf-8"))
    mapping = json.load(open(os.path.join(folder, "mapping.json"), encoding="utf-8"))
    named, drop = mapping.get("items", {}), set(mapping.get("drop", []))
    # blanks looked at and judged not to be a place for a value, with why
    skip = mapping.get("skip", {})
    extra, notes = mapping.get("add", []), mapping.get("notes", [])
    doc = fitz.open(os.path.join(FORMS, ctx["form"]))
    said, rows = [], []
    by_id = {i["id"]: i for i in ctx["items"]}

    for i in ctx["items"]:
        if i["id"] not in named and i["id"] not in drop and i["id"] not in skip:
            if i["existing"] is not None:
                said.append(f"{i['id']}: field {i['existing']!r} is in no mapping - left as it was")
            else:
                said.append(f"{i['id']}: {i['type']} blank at page {i['page'] + 1} "
                            f"{[round(v) for v in i['rect']]} is in no mapping - no field placed")
    for k in list(named) + list(drop) + list(skip):
        if k not in by_id:
            said.append(f"{k}: in the mapping but not on the form")
    for k, m in named.items():
        said += problems(k, m)
    for n, m in enumerate(extra, 1):
        said += problems(f"add {n}", m)

    # existing fields first, each matched to its item by name and the nearest
    # rect - two fields can share a name and overlap (Assignment's old date
    # boxes under the block's), and the first that merely touched was wrong
    def near(item, w):
        return sum(abs(a - b) for a, b in zip(item["rect"], fitz.Rect(w.rect)))

    radios, renamed = {}, {}
    original_names = {w.field_name for p in doc for w in p.widgets()}
    for page in doc:
        for w in list(page.widgets()):
            cands = [i for i in ctx["items"] if i["existing"] == w.field_name
                     and i["page"] == page.number]
            if not cands:
                continue
            item = min(cands, key=lambda i: near(i, w))
            if item["id"] in drop:
                page.delete_widget(w)
                continue
            m = named.get(item["id"])
            if not m:
                continue
            # a radio group is one field whose options are its widgets: it is
            # named as a whole, below, never pulled apart
            # (unless the mapping rebuilds it as something else: LIRA Trans
            # Info's two Yes/No questions shared their groups by column)
            if w.field_type == fitz.PDF_WIDGET_TYPE_RADIOBUTTON and not m.get("type"):
                radios.setdefault(w.field_name, []).append((w.xref, item["id"], m["name"]))
                continue
            # and so is a check box group - the radio buttons the restyle
            # made check boxes, "Group1.Choice1" under "Group1": the group
            # takes the name, each box keeps its option's (or the mapping's
            # "option" for it)
            if (w.field_type == fitz.PDF_WIDGET_TYPE_CHECKBOX and not m.get("type")
                    and box_group(doc, w.xref)):
                radios.setdefault(box_group(doc, w.xref), []).append(
                    (w.xref, item["id"], m["name"]))
                if m.get("option"):
                    doc.xref_set_key(w.xref, "T", fitz.get_pdf_str(m["option"]))
                continue
            have = HAS.get(w.field_type, "text")
            # (a mapping can ask for another type: Indemnity's tick boxes were
            # small text fields)
            want = type_for(m["name"], m.get("type") or (have if have in TYPES else "text"))
            rect = fitz.Rect(m.get("rect") or w.rect)
            # a widget that shares its field with others (one name placed
            # twice on the original) is rebuilt as a field of its own, or
            # renaming it renames its siblings too
            shared = doc.xref_get_key(w.xref, "Parent")[0] == "xref"
            renamed.setdefault(w.field_name, m["name"])
            # a field keeping its name, type and place is left exactly as it
            # is - the T4 worksheets' calculation fields keep their names on
            # purpose, and rebuilt they lose their place in the calculation order
            if m["name"] == w.field_name and have == want and not m.get("rect"):
                continue
            if have != want or m.get("rect") or shared:
                choices = list(w.choice_values or []) if want == "dropdown" else []
                kept = scripts_of(w)
                page.delete_widget(w)
                add_field(page, rect, m["name"], want, choices, kept)
            else:
                w.field_name = m["name"]
                w.update()
    for old, ws in radios.items():
        names = {n for _, _, n in ws}
        if len(names) > 1:
            said.append(f"radio group {old!r} ({', '.join(i for _, i, _ in ws)}) was given "
                        f"{len(names)} names - a group takes one; left as it was")
            continue
        new = names.pop()
        # named on the parent where the group has one; where its options are
        # fields of their own that only share a name (Bill Payment's Group1),
        # on each, and merge_repeats makes them one group after
        parent = doc.xref_get_key(ws[0][0], "Parent")
        targets = ({int(parent[1].split()[0])} if parent[0] == "xref"
                   else {x for x, _, _ in ws})
        for target in targets:
            doc.xref_set_key(target, "T", fitz.get_pdf_str(new))
        renamed[old] = new
    for k, m in named.items():
        i = by_id.get(k)
        if i is None or i["existing"] is not None:
            continue
        asked = m.get("type") or ("checkbox" if i["type"] == "tick" else "text")
        add_field(doc[i["page"]], m.get("rect") or i["rect"], m["name"], type_for(m["name"], asked))

    for m in extra:
        add_field(doc[m["page"]], m["rect"], m["name"], type_for(m["name"], m.get("type", "text")))

    said += follow_renames(doc, renamed, original_names)
    import restyle_pdf
    pruned = prune_fields(doc)
    if pruned:
        print(f"   ({pruned} empty parent field(s) left by the renaming taken off the field list)")
    unlisted = restyle_pdf.list_all_fields(doc)
    if unlisted:
        said.append(f"{unlisted} field(s) were missing from the form's field list - put back "
                    f"(the form at 10pt had them unlisted)")
    out_pdf = os.path.join(folder, f"{stem}.pdf")
    doc.save(out_pdf, garbage=3, deflate=True)
    doc.close()
    shared = merge_repeats(out_pdf)

    for k, m in named.items():
        i = by_id.get(k, {})
        # (a check box group's box is named as the group's option)
        full = f"{m['name']}.{m['option']}" if m.get("option") else m["name"]
        if (not m.get("option") and not m.get("type") and i.get("type") == "CheckBox"
                and "." in (i.get("existing") or "")):
            full = f"{m['name']}.{i['existing'].rsplit('.', 1)[1]}"
        rows.append({"id": k, "page": i.get("page", -1) + 1, "name": full, "kind": m["kind"],
                     "label on form": m.get("label", ""), "what goes there": m.get("desc", ""),
                     "was": i.get("existing") or f"(new, on a {i.get('type')})"})
    for n, m in enumerate(extra, 1):
        rows.append({"id": f"A{n}", "page": m["page"] + 1, "name": m["name"], "kind": m["kind"],
                     "label on form": m.get("label", ""), "what goes there": m.get("desc", ""),
                     "was": "(new, placed by hand)"})
    for k, why in skip.items():
        i = by_id.get(k, {})
        rows.append({"id": k, "page": i.get("page", -1) + 1, "name": "", "kind": "not a field",
                     "label on form": "", "what goes there": why,
                     "was": f"(a {i.get('type')} blank)"})
    where = {f"A{n}": m["rect"] for n, m in enumerate(extra, 1)}
    rows.sort(key=lambda r: (r["page"], (by_id.get(r["id"], {}).get("rect")
                                         or where.get(r["id"]) or [0, 0])[1]))
    with open(os.path.join(folder, "fields.csv"), "w", newline="", encoding="utf-8-sig") as fh:
        wr = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["id"])
        wr.writeheader()
        wr.writerows(rows)
        for n in notes:
            wr.writerow({"id": "NOTE", "what goes there": n})
        for s in said:
            wr.writerow({"id": "CHECK", "name": s})

    # the proof: each field's name over it, coloured by kind
    saved = fitz.open(out_pdf)
    kind_of = {m["name"]: m["kind"] for m in list(named.values()) + extra}
    for page in saved:
        marks = [(fitz.Rect(w.rect), w.field_name) for w in page.widgets()]
        for r, n in marks:
            colour = COLOURS.get(kind_of.get(n), (0.8, 0.0, 0.0))
            page.draw_rect(r, color=colour, width=0.9)
            # inside the box where it fits - stacked boxes 13pt apart, as in a
            # letter's address block, put every name over the box above it -
            # and over a tick box, which has no room in it
            if r.height >= 9 and r.width >= 30:
                page.insert_text(fitz.Point(r.x0 + 1.5, r.y0 + 6.5), n, fontsize=5.5, color=colour)
            else:
                page.insert_text(fitz.Point(r.x0 + 1, r.y0 - 1.5), n, fontsize=5.5, color=colour)
        page.get_pixmap(dpi=110, annots=False).save(os.path.join(folder, f"named p{page.number + 1}.png"))

    counts = {k: sum(1 for m in list(named.values()) + extra if m["kind"] == k) for k in KINDS}
    print(f"{stem}: " + ", ".join(f"{n} {k}" for k, n in counts.items()) + f", {len(drop)} dropped"
          + (f"; {shared} name(s) shown in more than one place, written as one field each"
             if shared else ""))
    for n in notes:
        print("   NOTE  " + n)
    for s in said:
        print("   CHECK " + s)
    return rows, said


# areas of the missing-from-DB list: (name pattern, area, note for the area)
GROUPS = [
    (r"PRIMARY_MAIL_(ADDR_LINE|CARE_OF)", "Member mailing address, as letter lines",
     "The forms' merge set the address as 3 lines (and a c/o line). Either the feed adds them, or the forms take PRIMARY_MAIL_FULL_LINE or PRIMARY_LEGAL_STREET/CTY/PRV/PST instead."),
    (r"PRIMARY_(DATE_OF_DEATH|DEATH_)", "Member's date of death",
     "One value to add (PRIMARY_DATE_OF_DEATH). Some forms show it whole, others as day / month / year boxes."),
    (r"SPOUSE_", "Spouse / common-law partner",
     "The DB list has only SPOUSE_FULL_NAME. Also wanted on forms as day/month/year: SPOUSE_DATE_OF_BIRTH."),
    (r"(REQUESTUSER|ACCTMGR)_", "Staff member preparing or signing", "REQUESTUSER and ACCTMGR_FULL_NAME exist; their position and email do not."),
    (r"ORG_OFFICER", "Organization signing officers", "Officer names come from the signer dropdowns (NAMES_MDAS); their titles do not."),
    (r"LOAN_PAYOUT_", "Loan payout statements", "No loan group in the DB list."),
    (r"RENEWAL_", "Loan renewal / conversion offers", "The rates and payments offered per term."),
    (r"LOAN_", "Loans", "No loan group in the DB list. If loans live in the mortgage tables, these are MORT_ fields (MORT_ACCOUNT_ORIGINALAMOUNT, MORT_ACCOUNT_PAYMENT, ...)."),
    (r"TERM_", "Term deposits / GICs", "Beyond the TERM_ fields already listed: sub number and maturity/interest instructions."),
    (r"SDB_", "Safe deposit boxes", ""),
    (r"ESTATE_", "Estates", ""),
]


def gather(root):
    """One list across every form named so far, for the people who build the
    datafeed: each candidate value once, with what it is and every form and
    label it turns up under - and every DB match a person still has to
    confirm. Read back from each form's fields.csv, so it is the same list
    the forms say."""
    cands, checks = {}, []
    for stem in sorted(os.listdir(root)):
        path = os.path.join(root, stem, "fields.csv")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                if r.get("kind") == "candidate":
                    # (a box of a check box group is asked for as its group)
                    c = cands.setdefault(r["name"].split(".")[0], {"desc": r["what goes there"],
                                                     "labels": [], "forms": []})
                    if r["label on form"] and r["label on form"] not in c["labels"]:
                        c["labels"].append(r["label on form"])
                    if stem not in c["forms"]:
                        c["forms"].append(stem)
                elif r.get("kind") == "db" and "CHECK" in r.get("what goes there", ""):
                    checks.append((stem, r["name"], r["label on form"], r["what goes there"]))
                elif r.get("id") == "NOTE":
                    checks.append((stem, "", "", r["what goes there"]))
    # one list for the people who build the datafeed, by area: what the forms
    # ask the DB for that dbFields.json does not list yet
    order = []
    rows = []
    for n, c in cands.items():
        g, note = next(((g, note) for pat, g, note in GROUPS if re.match(pat, n)), ("Other", ""))
        if g not in order:
            order.append(g)
        rows.append((g, n, c, note))
    rank = {g: k for k, (_, g, _) in enumerate(GROUPS)}
    rows.sort(key=lambda r: (rank.get(r[0], 99), r[1]))
    stale = os.path.join(root, "DB candidates.csv")
    if os.path.exists(stale):
        os.remove(stale)
    with open(os.path.join(root, "missing from DB.csv"), "w", newline="", encoding="utf-8-sig") as fh:
        wr = csv.writer(fh)
        wr.writerow(["area", "proposed DB field", "what it is", "forms using it", "labels on the forms",
                     "forms", "note"])
        for g, n, c, note in rows:
            wr.writerow([g, n, c["desc"], len(c["forms"]), " | ".join(c["labels"]),
                         " | ".join(c["forms"]), note])
    # and the names alone, one per line, for whoever just wants the list
    with open(os.path.join(root, "missing from DB.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(n for _, n, _, _ in rows) + "\n")
    # every field of every form in one table, and every named form in one
    # folder - the per-form folders hold the same, one form at a time
    import shutil
    named = os.path.join(root, "named forms")
    os.makedirs(named, exist_ok=True)
    every = []
    for stem in sorted(os.listdir(root)):
        path = os.path.join(root, stem, "fields.csv")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8-sig") as fh:
            every += [{"form": stem, **r} for r in csv.DictReader(fh) if r.get("id") not in ("NOTE", "CHECK")]
        pdf = os.path.join(root, stem, f"{stem}.pdf")
        if os.path.exists(pdf):
            shutil.copy2(pdf, named)
    with open(os.path.join(root, "all fields.csv"), "w", newline="", encoding="utf-8-sig") as fh:
        wr = csv.DictWriter(fh, fieldnames=["form", "page", "name", "kind", "label on form",
                                            "what goes there", "id", "was"], extrasaction="ignore")
        wr.writeheader()
        wr.writerows(every)
    with open(os.path.join(root, "to confirm.csv"), "w", newline="", encoding="utf-8-sig") as fh:
        wr = csv.writer(fh)
        wr.writerow(["form", "field", "label on form", "question"])
        wr.writerows(checks)
    print(f"{len(cands)} value(s) missing from the DB list -> missing from DB.csv; "
          f"{len(checks)} thing(s) to confirm -> to confirm.csv")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folders", nargs="+")
    args = ap.parse_args()
    bad = 0
    for f in args.folders:
        if not os.path.exists(os.path.join(f, "context.json")):
            continue                  # "named forms" and the like: not a form
        _, said = apply(f)
        bad += len(said)
    gather(os.path.dirname(os.path.normpath(args.folders[0])))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
