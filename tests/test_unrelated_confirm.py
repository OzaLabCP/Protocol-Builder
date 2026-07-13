"""Unrelated-change confirm gate — served static/index.html source assertions (NO network).

Verifies computeUnrelatedChanges' pure classification (a changed entity NOT referenced by the
instruction and NOT locked is flagged unrelated; a referenced change is related; a locked id
is excluded — belt-and-suspenders with the server restore) and that the gate is armed only
after a revise, rendered only when unrelated != ∅, with a host-only /restore revert action.
Follows the test_server ``client.get("/")`` harness.

Runnable directly (``python tests/test_unrelated_confirm.py``) or under pytest.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.server import app  # noqa: E402

_HTML = TestClient(app).get("/").text


def test_compute_flags_unreferenced_unlocked_change():
    assert "function computeUnrelatedChanges(" in _HTML
    # referenced-by-instruction (token or literal stable id) -> related, not unrelated
    assert "related.push(rec)" in _HTML
    assert "unrelated.push(rec)" in _HTML
    # a locked id is excluded from "unrelated"
    assert "if (sid && lockedSet.has(sid)) return;" in _HTML
    # added entities are shown in the diff but not counted as unrelated value changes
    assert "if (!pr) return;" in _HTML


def test_gate_armed_only_after_revise():
    # The instruction is threaded in only for a revise; empty -> no gate.
    assert "LAST_REVISE_INSTRUCTION" in _HTML
    assert "computeUnrelatedChanges(prev, curr, instruction, LOCKED)" in _HTML
    assert "if (unrelated.length) renderUnrelatedGate(" in _HTML


def test_gate_render_and_revert_action():
    assert "function renderUnrelatedGate(" in _HTML
    assert 'box.id = "unrelated-gate"' in _HTML
    assert 'role", "alertdialog"' in _HTML
    assert '"Keep"' in _HTML and '"Revert unrelated"' in _HTML
    # Revert calls the host-only /restore with target ids + the LOCKED set.
    assert '"/api/protocol/" + encodeURIComponent(SESSION) + "/restore"' in _HTML
    assert "target_ids: unrelated.map((u) => u.id)" in _HTML


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
