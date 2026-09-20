# Sabotage tests

These break the restyle on purpose and check that the safety nets report it.
They are the reason to trust a clean run: a check nobody has seen fail is a
check nobody knows works.

    python tests/test_census.py tests/restyle_pdf.baseline.py restyle_pdf.py
    python tests/test_reflow.py restyle_pdf.py
    python tests/test_relabel.py restyle_pdf.py relabel.txt
    python tests/test_ligature.py restyle_pdf.py

`test_census.py` runs each case through the baseline script as well, to show
what the census used to miss: of the six, the original caught one.
