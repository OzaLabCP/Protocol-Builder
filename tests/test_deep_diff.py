"""Deep diff — served static/index.html source assertions (NO network, NO LLM).

Verifies renderDeepDiff's tracked-field coverage (unit change, provenance change,
citation.identifier change, added/removed warning), stable-id matching that survives a
positional shift, and the accessible table shape (caption + thead/tbody hoist via tableWrap).
Follows the test_server ``client.get("/")`` harness.

Runnable directly (``python tests/test_deep_diff.py``) or under pytest.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.server import app  # noqa: E402

_HTML = TestClient(app).get("/").text


def test_deepdiff_tracks_unit_value_provenance_citation():
    assert "function _entFields(" in _HTML
    # value + unit (critical_parameter), amount + unit (material)
    assert 'f["value"] = e.value; f["unit"] = e.unit;' in _HTML
    assert 'f["amount"] = e.amount; f["unit"] = e.unit;' in _HTML
    # provenance (+ note) and citation identifier are tracked scalars
    assert 'f["provenance"] = e.provenance;' in _HTML
    assert 'f["provenance_note"] = e.provenance_note;' in _HTML
    assert 'f["citation"] = e.citation && e.citation.identifier;' in _HTML


def test_deepdiff_detects_warning_add_and_remove():
    assert '"warning added"' in _HTML
    assert '"warning removed"' in _HTML


def test_deepdiff_matches_by_stable_id_not_position():
    # _diffKey prefers the stable id so a matched entity survives a list reorder.
    assert "function _diffKey(" in _HTML
    assert "_stableId(rec.ent, rec.kind)" in _HTML
    assert "function renderDeepDiff(" in _HTML


def test_deepdiff_table_is_accessible():
    # renderDeepDiff wraps its <table> via tableWrap with an explicit caption...
    assert 'tableWrap(table, "Before / after — this revision"' in _HTML
    # ...and tableWrap builds a caption + hoists a header row into thead/tbody.
    assert 'document.createElement("caption")' in _HTML
    assert 'document.createElement("thead")' in _HTML
    assert 'document.createElement("tbody")' in _HTML
    # del/ins reuse the diff styling for before/after cells.
    assert '"<del>" + esc(r.before) + "</del>"' in _HTML
    assert '"<ins>" + esc(r.after) + "</ins>"' in _HTML


if __name__ == "__main__":
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1; print(f"FAIL {fn.__name__}"); traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
