"""Value-lock UI — served static/index.html source assertions (NO network, NO LLM).

Follows the test_server ``client.get("/")`` harness. Verifies the lock affordance is wired
only when a stable id exists, that a toggle persists to localStorage (``gf_locks:<scope>``)
and reflects ``aria-pressed``, and that every mutating POST carries ``locked_ids`` == the
LOCKED set (so the client set single-sources both server _enforce_locks and the unrelated
gate).

Runnable directly (``python tests/test_locks_ui.py``) or under pytest.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.server import app  # noqa: E402

_HTML = TestClient(app).get("/").text


def test_lock_toggle_gated_on_stable_id():
    # A toggle is appended ONLY when the entity carries a stable id (m.material_id / cp.parameter_id).
    assert "if (m.material_id) amtCell.appendChild(lockToggle(m.material_id" in _HTML
    assert "if (cp.parameter_id) line.appendChild(lockToggle(cp.parameter_id" in _HTML


def test_lock_toggle_is_a_button_with_aria_pressed():
    assert "function lockToggle(" in _HTML
    assert 'el("button", "lock-toggle op-control"' in _HTML
    assert 'setAttribute("aria-pressed"' in _HTML
    assert "🔒" in _HTML and "🔓" in _HTML


def test_lock_persists_to_localstorage_scope():
    # Scoped localStorage key + persistence on every toggle.
    assert "gf_locks:" in _HTML
    assert "function saveLocks(" in _HTML and "function loadLocks(" in _HTML
    assert "function toggleLock(" in _HTML and "saveLocks(LOCKED)" in _HTML
    # scope = PROJECT.id ?? SESSION
    assert "function lockScope(" in _HTML


def test_revise_and_apply_send_locked_ids():
    # The LOCKED set is sent as locked_ids on revise/apply/critique/restore.
    assert "locked_ids: [...LOCKED]" in _HTML
    # present on more than one POST site
    assert _HTML.count("locked_ids: [...LOCKED]") >= 3


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
