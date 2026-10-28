# Form cleaner

Restyles the business's PDF forms (Arial 10pt, header, spellcheck), rebuilds
the ones that need it, names their fields against the DB list and builds the
handovers. Each script's own docstring says how to run it.

## Setting it up on another computer (Windows)

1. Install Python 3.13, Git and, for the designer step only, Node 24.
2. Clone this repo, then in its folder:

       pip install -r requirements.txt

3. Check it works by running the sabotage tests (see `tests/README.md`):

       python tests/test_census.py tests/restyle_pdf.baseline.py restyle_pdf.py

   If a script stops with a `UnicodeEncodeError` while its output is piped
   or redirected, set `PYTHONIOENCODING=utf-8` first.

## What is not in the repo

The repo holds only the code and the data the code reads (field mappings,
the DB field list, the allowlists, the header logo). Copy these over by hand
when you need them; none of them may be put online:

- **The forms themselves.** Every PDF (`input forms/`, `Final Selection/`,
  the batch folders) along with the proofs, Word originals and handover zips.
  `tests/test_line_art.py` needs one form from `input forms/`; the other
  tests make their own.
- **The requirements workbook** (`Combined Form Requirements.xlsx`). It
  carries live account numbers.
- **The designer app.** `designer/` drives the PDF Requirement Designer,
  which is its own repo. It is expected next to this folder:

      Desktop/
        form cleaner/            this repo
        form_requirements/       the designer (release-build branch), plus the workbook
        form_requirements_bulk/  bulk-review branch, built with npm run build, for Review.cmd

  Without it, everything up to and including the field naming still runs.
  Only the coded PDFs, the requirements workbooks and the review station
  need it.
