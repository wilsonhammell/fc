#!/usr/bin/env python3
"""A first draft of a form's mapping, for a person to read and correct.

    python draft_mapping.py "field naming/<form>" [...] [--force]

Writes mapping.json beside each form's context.json (never over one already
there unless --force). Each item says how it was drafted ("how"), so the
review knows which ones to trust:

    signing   a line of the client's standard block: Member{i}Name /
              Signature / SignDate per member row, numbered per block, and
              REQUESTUSER / PRIMARY_SIGN_DATE on the staff row
    tag       an old merge tag, through TAGS below - the same tag gets the
              same name on every form
    label     a label rule (member number, SIN, date of birth, by section)
    guess     an entry name made from the label beside it - read every one

Nothing here is final; apply_fields.py checks the names against the DB list
and says what the mapping leaves out.
"""
import argparse
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
DB = {d["identifier"] for d in json.load(open(os.path.join(HERE, "dbFields.json"), encoding="utf-8"))}

# old merge tag (without a trailing _2, _3 copy number) -> (name, note)
TAGS = {
    "Member_Full_Name": ("PRIMARY_FULL_LEGAL_NAME", "Member's full name"),
    "Owner_Full_Name": ("PRIMARY_FULL_LEGAL_NAME", "Owner's full name"),
    "Member_First_Name": ("PRIMARY_NAME", "Member's first name"),
    "Member_Usual_Name": ("PRIMARY_NAME", "Member's usual (first) name"),
    "Member_Middle_Initial": ("PRIMARY_MIDDLE", "Member's middle name/initial"),
    "Member_Middle_Name": ("PRIMARY_MIDDLE", "Member's middle name"),
    "Member_Surname": ("PRIMARY_SURNAME", "Member's surname"),
    "Member_SIN": ("PRIMARY_SIN", "Member's SIN"),
    "Member_Birth_Date": ("PRIMARY_DATE_OF_BIRTH", "Member's date of birth"),
    "Member_PCode": ("PRIMARY_LEGAL_PST", "Member's postal code"),
    "Member_Addr_Line1": ("PRIMARY_MAIL_ADDR_LINE1", "Member's mailing address, line 1 (the merge's address lines; the DB list has PRIMARY_MAIL_FULL_LINE as one line, or legal street/city/province/postal)"),
    "Member_Addr_Line2": ("PRIMARY_MAIL_ADDR_LINE2", "Member's mailing address, line 2"),
    "Member_Addr_Line3": ("PRIMARY_MAIL_ADDR_LINE3", "Member's mailing address, line 3"),
    "Member_Addr_Care_Of": ("PRIMARY_MAIL_CARE_OF", "Mailing address 'care of' line"),
    "Customer_Number": ("PRIMARY_MEMBER_NUMBER", "Member number"),
    "Owner_Number": ("PRIMARY_MEMBER_NUMBER", "Owner (member) number"),
    "Customer_Branch": ("PRIMARY_BRANCH_NAME", "Member's branch. CHECK: name or number (PRIMARY_BRANCH)"),
    "Owner_Branch": ("PRIMARY_BRANCH_NAME", "Owner's branch. CHECK: name or number (PRIMARY_BRANCH)"),
    "Branch_No": ("PRIMARY_BRANCH", "Branch number"),
    "Branch_Phone": ("PRIMARY_BRANCH_PHONE", "Branch phone"),
    "Branch_Address1": ("PRIMARY_BRANCH_ADDR1", "Branch address line 1"),
    "Branch_Address2": ("PRIMARY_BRANCH_ADDR2", "Branch address line 2"),
    "Branch_PCode": ("PRIMARY_BRANCH_PC", "Branch postal code"),
    "Branch_TransitNo": ("PRIMARY_BRANCH_TRANSIT", "Branch transit number"),
    "Members_FSR_Full_Name": ("ACCTMGR_FULL_NAME", "The member's FSR, signing the letter. CHECK: is the FSR the account manager"),
    "Members_FSR_Position": ("ACCTMGR_POSITION", "The FSR's position / title"),
    "User_Name": ("REQUESTUSER", "Staff member preparing the form"),
    "User_Position": ("REQUESTUSER_POSITION", "Staff member's position / title"),
    "User_Email": ("REQUESTUSER_EMAIL", "Staff member's email"),
    "Member_Officer": ("ORG_OFFICER{n}_NAME", "Organization signing officer {n}, name"),
    "Member_Title": ("ORG_OFFICER{n}_TITLE", "Organization signing officer {n}, title"),
    "Join_First_Name": ("SIGNER{n}_NAME", "Joint owner {n}, first name"),
    "Join_Usual_Name": ("SIGNER{n}_NAME", "Joint owner {n}, usual (first) name"),
    "Join_Middle_Initial": ("SIGNER{n}_MIDDLE", "Joint owner {n}, middle name/initial"),
    "Join_Surname": ("SIGNER{n}_SURNAME", "Joint owner {n}, surname"),
    "Join_Full_Name": ("JointMember{n}Name", "Joint owner {n}, full name - the starter code fills it from SIGNER{n}"),
    "Join_Customer_Number": ("JointMember{n}Number", "Joint owner {n}, member number"),
    "Loan_Approved_Amount": ("LOAN_APPROVED_AMOUNT", "Loan's approved (original) amount"),
    "Loan_Amount": ("LOAN_APPROVED_AMOUNT", "Loan approved (authorized) amount"),
    "Loan_Balance": ("LOAN_BALANCE", "Loan's current balance"),
    "Loan_Fund_Date": ("LOAN_FUND_DATE", "Date the loan was funded"),
    "Loan_Product": ("LOAN_PRODUCT", "Loan product"),
    "Loan_ApprCommittee": ("LOAN_APPROVAL_COMMITTEE", "Committee that approved the loan"),
    "Maturity_Date": ("LOAN_MATURITY_DATE", "Loan's maturity date"),
    "Payment_Frequency": ("LOAN_PAYMENT_FREQUENCY", "Loan payment frequency"),
    "Next_Payment_Due_Date": ("LOAN_NEXT_PAYMENT_DATE", "Loan's next payment date"),
    "Total_Payment": ("LOAN_TOTAL_PAYMENT", "Loan's total payment"),
    "Term_Years": ("LOAN_TERM_YEARS", "Loan term, years"),
    "Years_of_Amortization": ("LOAN_AMORTIZATION_YEARS", "Loan amortization, years"),
    "Rate_Increment": ("LOAN_RATE_INCREMENT", "Rate over prime"),
    "Rate_Calculated": ("LOAN_INTEREST_RATE", "Loan's interest rate"),
    "Payo_Date": ("LOAN_PAYOUT_DATE", "Payout statement date"),
    "Payo_DischargeFee": ("LOAN_PAYOUT_DISCHARGE_FEE", "Payout: discharge fee"),
    "Payo_InterestDiff": ("LOAN_PAYOUT_INTEREST_DIFF", "Payout: interest rate differential"),
    "Payo_InterestDue": ("LOAN_PAYOUT_INTEREST_DUE", "Payout: interest due"),
    "Payo_Penalty": ("LOAN_PAYOUT_PENALTY", "Payout: penalty"),
    "Payo_PerDiem": ("LOAN_PAYOUT_PER_DIEM", "Payout: per diem interest"),
    "Payo_PrinAmount": ("LOAN_PAYOUT_PRINCIPAL", "Payout: principal"),
    "Payo_TaxBal": ("LOAN_PAYOUT_TAX_BALANCE", "Payout: tax account balance"),
    "Term_Amount": ("TERM_ACCOUNT_AMOUNT", "Term deposit amount"),
    "Start_Date": ("TERM_ACCOUNT_OPENED", "Term deposit start date. CHECK: or TERM_ACCOUNT_INTDATE"),
    "Term_Length": ("TERM_ACCOUNT_TERMFREQUENCY", "Term length, the number. CHECK: TERMFREQUENCY vs TERMPER"),
    "Term_Period": ("TERM_ACCOUNT_TERMPER", "Term length, the unit"),
    "Term_Rate": ("TERM_ACCOUNT_RATE1", "Term deposit rate"),
    "Term_Type": ("TERM_ACCOUNT_TYPE", "Term deposit type"),
    "Term_Maturity_Date": ("TERM_ACCOUNT_MATURITY", "Term deposit maturity date"),
    "Term_Interest_Schedule": ("TERM_ACCOUNT_INTSCHED", "Interest payment frequency"),
    "Term_Deposit_Code": ("TERM_ACCOUNT_CODE", "Term deposit product. CHECK: code or TERM_ACCOUNT_DESC"),
    "Registered_Contract_No": ("TERM_ACCOUNT_CONTRACT", "Term deposit contract number"),
}
SPECIAL = re.compile(r"^(Member[1-6](Name|Signature|SignDate)|JointMember[1-5](Name|Number)|JointMembers)$")
CAPS = {"name", "authorized signature", "date", "authorizing staff member"}


def kind_of(name):
    if SPECIAL.match(name):
        return "special"
    return "db" if name in DB else "candidate"


def from_tag(existing):
    """The merge tag an existing field still carries, through TAGS."""
    if not existing:
        return None
    base = re.sub(r"_\d+$", "", existing)
    m = re.match(r"^(Join)_?(\d)_?(.*)$", base) or re.match(r"^(Member_Officer|Member_Title)(\d)$", base)
    n = None
    if m and m.group(1) == "Join":
        n, base = m.group(2), "Join_" + m.group(3).lstrip("_")
    elif m:
        n, base = m.group(2), m.group(1)
    if base not in TAGS:
        return None
    name, desc = TAGS[base]
    return name.format(n=n), desc.format(n=n)


def label_of(i):
    """The words a person reads as this place's label."""
    for k in ("left", "below", "above", "right"):
        t = (i.get(k) or "").strip()
        if i["type"] in ("CheckBox", "tick") and k == "left":
            continue                     # a tick box is labelled after it
        if t:
            return t
    return ""


def by_label(i, label):
    """The few label rules worth trusting, read with the section around it."""
    ctx = f"{i.get('section', '')} {label}".lower()
    lab = label.lower()
    other = re.search(r"spouse|beneficiar|successor|trustee|executor|estate|witness|guarantor|"
                      r"transferee|receiving|relinquish|payee|deceased|applicant 2|joint", ctx)
    if re.search(r"\b(member|owner|annuitant|account holder|customer)\s*(number|no\.?|#)", lab) and not other:
        return "PRIMARY_MEMBER_NUMBER", "Member number"
    if re.search(r"\bs\.?\s?i\.?\s?n\b|social insurance", lab):
        if "spouse" in ctx:
            return "SPOUSE_SIN", "Spouse's SIN"
        if not other:
            return "PRIMARY_SIN", "Member's SIN"
    if re.search(r"date of birth|birth ?date|d\.o\.b", lab):
        if "spouse" in ctx:
            return "SPOUSE_DATE_OF_BIRTH", "Spouse's date of birth"
        if not other:
            return "PRIMARY_DATE_OF_BIRTH", "Member's date of birth"
    return None


def guess_name(label, section, taken):
    words = re.findall(r"[A-Za-z0-9]+", label.upper())
    stop = {"THE", "OF", "A", "AN", "AND", "FOR", "TO", "IF", "IS", "PLEASE", "YOUR", "IN", "ON", "BY"}
    words = [w for w in words if w not in stop][:5] or ["FIELD"]
    name = "_".join(words)
    if name[0].isdigit():
        name = "F_" + name
    base, k = name, 2
    while name in taken or name in DB:
        name, k = f"{base}_{k}", k + 1
    return name


def signing_rows(items):
    """The standard block's rows: items whose caption under them is one of the
    block's, grouped by line, numbered per block - the block's member rows are
    Member1, Member2 ..., starting again after each staff row."""
    capped = [i for i in items if (i.get("below") or "").strip().lower() in CAPS]
    rows = {}
    for i in capped:
        rows.setdefault((i["page"], round(i["rect"][3] / 6)), []).append(i)
    out, n = {}, 0
    for key in sorted(rows):
        row = sorted(rows[key], key=lambda i: i["rect"][0])
        caps = [(i.get("below") or "").strip().lower() for i in row]
        if "authorizing staff member" in caps:
            for i, c in zip(row, caps):
                if c == "authorizing staff member":
                    out[i["id"]] = ("REQUESTUSER", "Authorizing staff member")
                elif c == "date":
                    out[i["id"]] = ("PRIMARY_SIGN_DATE", "Staff date")
            n = 0
        elif "authorized signature" in caps:
            n += 1
            for i, c in zip(row, caps):
                part = {"name": "Name", "authorized signature": "Signature", "date": "SignDate"}[c]
                out[i["id"]] = (f"Member{min(n, 6)}{part}", f"Signer {n}")
    return out


def draft(folder, force=False):
    path = os.path.join(folder, "mapping.json")
    if os.path.exists(path) and not force:
        return None
    ctx = json.load(open(os.path.join(folder, "context.json"), encoding="utf-8"))
    items = ctx["items"]
    sign = signing_rows(items)
    out, taken = {}, set()
    for i in items:
        label = label_of(i)
        typed = "checkbox" if i["type"] in ("CheckBox", "tick") else None
        if i["id"] in sign:
            name, desc = sign[i["id"]]
            how = "signing"
        elif from_tag(i["existing"]):
            name, desc = from_tag(i["existing"])
            how = "tag"
        elif by_label(i, label):
            name, desc = by_label(i, label)
            how = "label"
        else:
            name = guess_name(label, i.get("section", ""), taken)
            desc = label or "(no label found)"
            how = "guess"
        if how == "guess":
            taken.add(name)
        m = {"name": name, "kind": "entry" if how == "guess" else kind_of(name),
             "label": label, "desc": desc, "how": how}
        if typed:
            m["type"] = typed
        out[i["id"]] = m
    mapping = {"items": out, "drop": [], "skip": {}, "add": [], "notes": []}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(mapping, fh, indent=1, ensure_ascii=False)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folders", nargs="+")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    for f in args.folders:
        if not os.path.exists(os.path.join(f, "context.json")):
            continue
        out = draft(f, args.force)
        if out is None:
            print(f"{os.path.basename(f)}: has a mapping already - left alone")
            continue
        hows = {}
        for m in out.values():
            hows[m["how"]] = hows.get(m["how"], 0) + 1
        print(f"{os.path.basename(f)}: " + ", ".join(f"{n} {h}" for h, n in sorted(hows.items())))


if __name__ == "__main__":
    main()
