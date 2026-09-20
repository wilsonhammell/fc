"""Checks on spelling out ligatures: the right letters go in, and a glyph
that isn't in a word is never touched. Run: python test_ligature.py <script>"""
import sys, importlib.util

spec = importlib.util.spec_from_file_location("r", sys.argv[1])
r = importlib.util.module_from_spec(spec); spec.loader.exec_module(r)
freq = r.word_frequency()
if freq is None:
    sys.exit("pyspellchecker isn't installed, so nothing to check")

cases = [
    # word as extracted, index of the glyph, what it must come out as
    ("en\ufffdtled", 2, "ti", "a word only 'ti' completes"),
    ("Op\ufffdon", 2, "ti", "the same, capitalised"),
    ("execu\ufffdon", 5, "ti", "longer word"),
    ("a\ufffder", 1, "ft", "'after' beats 'aster' on frequency"),
    ("cer\ufffdficate", 3, "ti", "with the fi ligature already spelt out"),
    ("\ufffd", 0, None, "a lone symbol: a checkbox, never letters"),
    ("\u2610", 0, None, "a checkbox that the font did name"),
    ("A.\ufffd", 2, None, "a list marker, not a word"),
    ("\ufffd\ufffd", 0, None, "two symbols side by side"),
]
bad = 0
for word, i, want, why in cases:
    hits = r.ligature_candidates(word, i, freq)
    got = hits[0][1] if hits and (len(hits) == 1 or hits[0][0] >= 10 * hits[1][0]) else None
    ok = got == want
    bad += not ok
    print(f"  {'ok  ' if ok else 'FAIL'} {word!r:18} -> {str(got):5} (want {want}) {why}")
    if len(hits) > 1:
        print(f"         others: {hits[1:4]}")
print("FAILURES:", bad) if bad else print("all ligature cases behave")
sys.exit(1 if bad else 0)
