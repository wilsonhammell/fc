"""What the PDF Requirement Designer needs for each final form, worked out
ahead of it: one job file per production form for build.mjs to run.

    python designer/requirements.py

A BA sets these in the designer by hand (Forms_Development_Procedure.docx,
"BA Development"). Here they come from:

  Config and signing buttons   the form's row of Combined Form Requirements
                               (designer/form_matches.json)
  Load Validation              every field named from the DB list: it is
                               pulled from the DB (client, 2026-10-16), and
                               read-only - but PRIMARY_SIGN_DATE, pulled and
                               still editable, as the business's own forms
                               have it, since staff correct a wrong date
  Error Message Field Name     the label beside the field on the form
  Save Validation              mapping.json: "save": true on the field, a
                               best guess from reading the form; Member1Name
                               always, as on every signed example form
  Custom Save Validation       mapping.json: "custom": "<the rule>", which
                               becomes its note - the developer writes the code
  Format                       dates dd-mmm-yyyy; amounts 2 decimals; rates
                               and percentages 2 decimals (Number, not
                               Percentage, which multiplies by 100); a field
                               already formatted keeps its own
  Multiline                    a text box two lines tall or more
  Initials markers             blanks skipped as "initials", with the role
                               written before the word

A mapping entry can override any of these: "format", "facing", "editable",
"load": false, "note".

Writes designer/jobs/<production name>.json and the page pictures the
requirements workbook shows, designer/jobs/<production name> pN.png.
"""
import csv
import json
import os
import re
import shutil

import fitz

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAMING = os.path.join(HERE, "field naming")
JOBS = os.path.join(HERE, "designer", "jobs")
DB = {e["identifier"]: e["label"] for e in json.load(open(os.path.join(HERE, "dbFields.json"), encoding="utf-8"))}
SYSTEM = {e["identifier"] for e in json.load(open(
    r"C:\Users\Wilson\Desktop\form_requirements\public\systemFields.json", encoding="utf-8"))}

EDITABLE_DB = {"PRIMARY_SIGN_DATE"}
# Pulled from the DB but NOT Load Validated: values a member can lack. Load
# Validation means "must arrive filled, or the form locks with 'The following
# required information is missing'" (helpers.js LoadValidation), so a joint
# signer's slot, a middle name or a second phone checked that way would lock
# the form for every member without one. The business's coded forms load-
# validate only what is always there - member number and name, account
# number, REQUESTUSER, PRIMARY_SIGN_DATE.
OPTIONAL_DB = re.compile(r"^SIGNER\d_|_MIDDLE$|ADDR_LINE[23]$|_LINE[23]$|^(?!.*BRANCH).*PHONE|"
                         r"EMAIL|^SPOUSE_|_ID_EXPIRYDATE$|_FAX$", re.I)
SIGNER = re.compile(r"^Member(\d+)(Name|Signature|SignDate)$")
JOINT = re.compile(r"^JointMember(\d+)(Name|Number)$|^JointMembers$")
DATE_NAME = re.compile(r"(SIGN_?DATE|_DATE\b|DATE_|DATE$|DATE_OF_BIRTH|_DOB\b|DOB$|EXPIRY|MATURITY|"
                       r"OPENED$|DATEOPENED|_DT$|SignDate$|FUNDDATE|INTDATE)", re.I)
AMOUNT_NAME = re.compile(r"(AMOUNT|AMT\b|_AMT|BALANCE|PAYMENT_AMOUNT|PRINCIPAL|LIMIT|_FEE\b|FEE$|"
                         r"PRICE|COST|TOTAL|INCOME|WITHHELD|INTEREST_AMOUNT|PAYOUT)", re.I)
RATE_NAME = re.compile(r"(RATE\d*$|_RATE\b|PERCENT|PCT\b|_PCT)", re.I)
TALL = 26.0                  # two lines at 10pt and more is a box written in sentences

# the buttons, left to right, where the business's coded forms put them:
# the Remote eSign one last, since its label and code box run on to its right
BUTTON_ORDER = ["PhoneAuthButton", "DigitalSignatureButton", "PrintButton",
                "SubmitForProcessingButton", "RemoteESignButton"]
BUTTON_W = {"SubmitForProcessingButton": 112}
BUTTON_LEFT, BUTTON_GAP, BUTTON_TOP, BUTTON_H = 151.0, 5.0, 3.0, 18.0
ESIGN_RUN = 93 + 86          # eSignLBL and eSign_Code, flush to its right


# the names the business's coded forms give these in their error messages
FRIENDLY = {"PRIMARY_MEMBER_NUMBER": "Member Number", "PRIMARY_FULL_LEGAL_NAME": "Member Name",
            "REQUESTUSER": "Staff Name", "PRIMARY_SIGN_DATE": "Date", "PRIMARY_SIN": "SIN",
            "PRIMARY_BRANCH_NAME": "Branch", "PRIMARY_BRANCH": "Branch Number"}
# words a label beside a box can be that say nothing about it
NOT_LABELS = {"and", "or", "of", "to", "the", "at", "by", "for", "with", "from", "in", "on",
              "per", "re", "between", "i", "we", "name", "date", "member", "dated", "amount"}


def good_label(t):
    """Is the text beside a box a label someone would recognise - 'Member
    Number', 'Date of Birth' - rather than the end of a sentence ('and',
    ', in the Province of Ontario, on')?"""
    words = t.split()
    return (3 <= len(t) <= 40 and len(words) <= 6 and t[0].isupper()
            and t.lower() not in NOT_LABELS and not re.match(r"(RE|BETWEEN)\b", t)
            and ". " not in t
            and not t.lower().startswith(("please ", "i ", "the following", "if ")))


def db_friendly(name):
    if name in FRIENDLY:
        return FRIENDLY[name]
    lab = DB.get(name) or re.sub(r"_+", " ", name).title()
    lab = re.sub(r"^Primary\s+", "", lab)
    return re.sub(r"^Signer (\d)", r"Joint Member \1", lab)


def facing(label, name, desc=""):
    """The name the error message uses for a field: the form's own label
    where it is one, otherwise a plain name for what goes there."""
    t = " ".join(str(label or "").split())
    t = re.sub(r"\((dd|mm|yy)[^)]*\)", "", t, flags=re.I)
    t = t.strip(" :;,.$_-–")
    if good_label(t):
        return t
    if name in DB:
        return db_friendly(name)
    d = " ".join(str(desc or "").split()).rstrip(".")
    d = re.sub(r"^(Pick one|Tick|Check box group)[^:]*:\s*", "", d)
    if 3 <= len(d) <= 50:
        return d[0].upper() + d[1:]
    return re.sub(r"_+", " ", name).title()


def number(decimals):
    return {"category": "number", "decimals": decimals, "separator": "none",
            "currency": "none", "parentheses": False, "redText": False}


def snake(name):
    """A CamelCase name as words, so the name patterns read it the way they
    read a DB one: PaymentDate -> PAYMENT_DATE, ATMTransactionDate ->
    ATM_TRANSACTION_DATE."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name).upper()


def format_for(name, label, kind):
    if kind != "Text":
        return None
    if SIGNER.match(name):
        return {"category": "date"} if name.endswith("SignDate") else None
    name = snake(name) if not re.fullmatch(r"[A-Z0-9_]+", name) else name
    lab = (label or "").lower()
    if "DATETIME" in name.upper():
        return None
    if DATE_NAME.search(name) or (re.search(r"\bdate\b|d\.o\.b|date of birth|birth date", lab)
                                  and "update" not in lab):
        return {"category": "date"}
    if RATE_NAME.search(name) or "%" in lab or "percent" in lab:
        return number(2)
    if AMOUNT_NAME.search(name) or "$" in lab:
        return number(2)
    return None


def page_widgets(pdf):
    """{name: [(page, rect, kind, has_format)]} off the named form."""
    out = {}
    doc = fitz.open(pdf)
    for page in doc:
        for w in page.widgets():
            out.setdefault(w.field_name, []).append(
                (page.number, fitz.Rect(w.rect), w.field_type_string, bool(w.script_format)))
    return doc, out


def settings_for(name, rows, places, mapping_items, production=""):
    """The designer settings for one field name, from every mapping entry
    and fields.csv row carrying it. production is the form being built: one
    cleaned file can be several production forms (Hypothecation Agreement is
    Bonds, Mutual Funds and Stocks), and "save_on": [names] makes a field
    required on those only."""
    kinds = {p[2] for p in places}
    kind = next(iter(kinds)) if kinds else "Text"
    m = {}
    for it in mapping_items:              # the entries naming this field
        m.update({k: v for k, v in it.items() if k in
                  ("save", "custom", "format", "facing", "editable", "load", "note")})
        if it.get("save_on"):
            m["save"] = production in it["save_on"]
    label = next((r["label on form"] for r in rows if r["label on form"].strip()), "")
    desc = next((r["what goes there"] for r in rows if r["what goes there"].strip()), "")
    s = {"how": [], "kind": rows[0]["kind"] if rows else ""}
    in_db = name in DB
    optional = bool(OPTIONAL_DB.search(name))
    if in_db and m.get("load", not optional):
        s["loadValidated"] = True
        s["customerFacingName"] = m.get("facing") or facing(label, name, desc)
        s["editable"] = name in EDITABLE_DB
        s["how"].append("load: a DB field")
    elif in_db:
        s["editable"] = name in EDITABLE_DB
        s["note"] = ("Pulled from the DB, not Load Validated: a member can be without one, and "
                     "Load Validation would lock the form for them.")
        s["how"].append("pulled, not load validated: can be empty")
    elif rows and rows[0]["kind"] == "candidate":
        s["editable"] = True
        s["note"] = (f"Meant to come from the DB, but {name} is not in the DB list yet (it is in "
                     f"'missing from DB'). Pull it and set Load Validation once the list has it.")
    else:
        s["editable"] = True
    if JOINT.match(name):
        s["editable"] = False         # written from DNA when the form opens
    signer = SIGNER.match(name)
    if signer and signer.group(2) == "Name" and signer.group(1) == "1":
        s["saveValidated"] = True
        s["how"].append("save: the first signer is always required")
    if "editable" in m:
        s["editable"] = bool(m["editable"])
    if m.get("custom"):
        s["customSaveValidated"] = True
        s["saveValidated"] = False
        s["note"] = str(m["custom"])
        s["how"].append("custom save: reviewed")
    elif m.get("save"):
        s["saveValidated"] = True
        s["how"].append("save: reviewed")
    if m.get("note"):
        s["note"] = (s.get("note", "") + " " + m["note"]).strip()
    if s.get("saveValidated") or s.get("customSaveValidated"):
        s.setdefault("customerFacingName", m.get("facing") or facing(label, name, desc))
    fmt = m.get("format") if "format" in m else format_for(name, label, kind)
    if fmt and not any(p[3] for p in places):
        s["format"] = fmt
    if kind == "Text" and any(p[1].height >= TALL for p in places):
        s["multiline"] = True
    s["label"], s["desc"] = label, desc
    return s


def markers_of(folder, page_heights):
    ctx = json.load(open(os.path.join(folder, "context.json"), encoding="utf-8"))
    mp = json.load(open(os.path.join(folder, "mapping.json"), encoding="utf-8"))
    by_id = {i["id"]: i for i in ctx["items"]}
    out = []
    for fid, why in mp.get("skip", {}).items():
        hit = re.match(r"(.+?)\s+initials\b(.*)", why, re.I)
        if not hit or fid not in by_id:
            continue
        i = by_id[fid]
        x0, y0, x1, y1 = i["rect"]
        h = page_heights[i["page"]]
        out.append({"page": i["page"], "rect": {"x": x0, "y": h - y1, "width": x1 - x0, "height": y1 - y0},
                    "role": hit.group(1).strip(), "note": ""})
    return out


def button_rects(buttons, page):
    """Each placed button's rect in PDF user space, left to right along the
    strip the cleaned form keeps clear at the top of page 1."""
    on = [b for b in BUTTON_ORDER if buttons.get(b)]
    out, x = {}, BUTTON_LEFT
    for b in on:
        w = BUTTON_W.get(b, 80)
        out[b] = {"x": x, "y": page.rect.height - BUTTON_TOP - BUTTON_H, "width": w, "height": BUTTON_H}
        x += w + BUTTON_GAP
    clash = []
    if on:
        right = x - BUTTON_GAP + (ESIGN_RUN if "RemoteESignButton" in on else 0)
        strip = fitz.Rect(BUTTON_LEFT, BUTTON_TOP, right, BUTTON_TOP + BUTTON_H)
        if right > page.rect.width - 6:
            clash.append(f"the buttons run to x={right:.0f}, past the page edge")
        for b in page.get_text("words"):
            if fitz.Rect(b[:4]).intersects(strip):
                clash.append(f"over the words {b[4]!r} at {b[0]:.0f},{b[1]:.0f}")
        for d in page.get_images(full=True):
            for r in page.get_image_rects(d[0]):
                if r.intersects(strip):
                    clash.append(f"over an image at {r.x0:.0f},{r.y0:.0f}")
    return out, clash


def render_pages(doc, markers, stem):
    """One picture per page for the workbook: the printed form with no field
    boxes over it, 900 pixels wide, and the initials markers drawn on - what
    the designer's own pageImages.js makes."""
    paths = []
    for page in doc:
        zoom = 900 / page.rect.width
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), annots=False)
        if any(m["page"] == page.number for m in markers):
            tmp = fitz.open()
            tmp.insert_pdf(doc, from_page=page.number, to_page=page.number, widgets=False)
            p = tmp[0]
            for m in markers:
                if m["page"] != page.number:
                    continue
                r = m["rect"]
                rect = fitz.Rect(r["x"], page.rect.height - r["y"] - r["height"],
                                 r["x"] + r["width"], page.rect.height - r["y"])
                p.draw_rect(rect, color=(0.851, 0.467, 0.024), width=1.2, dashes="[3 2] 0")
                p.insert_text((rect.x0, rect.y0 - 2), m["role"], fontsize=7, color=(0.851, 0.467, 0.024))
            pix = p.get_pixmap(matrix=fitz.Matrix(zoom, zoom), annots=False)
        path = os.path.join(JOBS, f"{stem} p{page.number + 1}.png")
        pix.save(path)
        paths.append({"page": page.number, "path": path,
                      "width": round(page.rect.width), "height": round(page.rect.height)})
    return paths


def main():
    matches = json.load(open(os.path.join(HERE, "designer", "form_matches.json"), encoding="utf-8"))
    # both start empty, so nothing left from an earlier run reaches the handover
    for d in (JOBS, os.path.join(HERE, "designer", "out")):
        if os.path.isdir(d):
            shutil.rmtree(d)
    os.makedirs(JOBS)
    summary = []
    for mt in matches:
        folder = os.path.join(NAMING, mt["folder"] or "")
        pdf = os.path.join(folder, f"{mt['folder']}.pdf")
        if not mt["folder"] or not os.path.isfile(pdf):
            summary.append((mt["production name"], "NO NAMED FORM"))
            continue
        doc, places = page_widgets(pdf)
        rows = [r for r in csv.DictReader(open(os.path.join(folder, "fields.csv"), encoding="utf-8-sig"))
                if r.get("name")]
        mp = json.load(open(os.path.join(folder, "mapping.json"), encoding="utf-8"))
        items_by_name = {}
        for it in mp.get("items", {}).values():
            items_by_name.setdefault(it["name"], []).append(it)
        for it in mp.get("add", []):
            items_by_name.setdefault(it["name"], []).append(it)
        fields = {}
        for name, pl in places.items():
            if name in SYSTEM and name not in DB and not items_by_name.get(name):
                continue                 # one of the designer's own hidden ones
            fields[name] = settings_for(name, [r for r in rows if r["name"] == name], pl,
                                        items_by_name.get(name, []), mt["production name"].strip())
        heights = [p.rect.height for p in doc]
        markers = markers_of(folder, heights)
        rects, clash = button_rects(mt["buttons"], doc[0]) if mt["match"] == "exact" else ({}, [])
        stem = mt["production name"].strip()
        job = {
            "production name": stem, "file": mt["file"], "match": mt["match"],
            "source": pdf, "config": mt["config"], "buttons": rects, "button clash": clash,
            "fields": fields, "markers": markers,
            "images": render_pages(doc, markers, stem),
            "out": os.path.join(HERE, "designer", "out", stem),
        }
        json.dump(job, open(os.path.join(JOBS, f"{stem}.json"), "w", encoding="utf-8"),
                  indent=1, ensure_ascii=False)
        n = len(fields)
        summary.append((stem, f"{n} fields, {sum(1 for f in fields.values() if f.get('loadValidated'))} load, "
                              f"{sum(1 for f in fields.values() if f.get('saveValidated'))} save, "
                              f"{sum(1 for f in fields.values() if f.get('customSaveValidated'))} custom, "
                              f"{sum(1 for f in fields.values() if f.get('format'))} formatted, "
                              f"{len(markers)} marker(s), buttons {list(rects)}"
                              + (f"; BUTTONS CLASH: {clash[:3]}" if clash else "")))
    for s, what in summary:
        print(f"{s[:58]:58} {what}")


if __name__ == "__main__":
    main()
