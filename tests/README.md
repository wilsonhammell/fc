# Sabotage tests

These break the restyle on purpose and check that the safety nets report it.
They are the reason to trust a clean run: a check nobody has seen fail is a
check nobody knows works.

    python tests/test_census.py tests/restyle_pdf.baseline.py restyle_pdf.py
    python tests/test_reflow.py restyle_pdf.py
    python tests/test_relabel.py restyle_pdf.py relabel.txt
    python tests/test_ligature.py restyle_pdf.py
    python tests/test_fields.py restyle_pdf.py
    python tests/test_finish.py apply_fields.py
    python tests/test_looks.py

`test_census.py` runs each case through the baseline script as well, to show
what the census used to miss: of the six, the original caught one.

`test_line_art.py` rubs out a ruled line and draws a box that wasn't there,
and checks both are reported. It is the check that has to hold before
anything is allowed to move a form's rules and boxes around.

`test_looks.py` spoils one thing about how a small form looks in each copy -
a run-in bold heading set regular, Word's slanted italic set upright, an
underline dropped, a blue link set black, a shaded table head left white, a
tick box walked to the end of its line, prose broken with room to spare,
stacked labels pulled apart, a centred heading set flush left - and checks
each is reported, and that an untouched copy reports nothing. These are what
the client's own review found that every other check passed.
