"""Recovery UI — served static/index.html source assertions (NO network, NO LLM).

Verifies the cancel/retry/start-over recovery surface: a cancel aborts the in-flight fetch
and an AbortError is treated as clean ("Canceled — your last protocol is unchanged.") without
touching CURR_PROTOCOL; Retry re-invokes LAST_OP.fn; startOver resets transient globals while
preserving CURR_PROTOCOL / SESSION / LOCKED. Follows the test_server ``client.get("/")`` harness.

Runnable directly (``python tests/test_recovery_ui.py``) or under pytest.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.server import app  # noqa: E402

_HTML = TestClient(app).get("/").text


def test_cancel_aborts_inflight_fetch():
    # An AbortController is created per long op; cancel aborts it.
    assert "CURRENT_ABORT = new AbortController()" in _HTML
    assert '$("#busy .busy-cancel").onclick' in _HTML
    assert "CURRENT_ABORT.abort()" in _HTML
    # postJSON forwards the signal; opSignal() feeds it in.
    assert "function opSignal()" in _HTML


def test_abort_is_clean_and_leaves_protocol():
    # AbortError is not an error toast — it announces "Canceled ..." and never mutates CURR_PROTOCOL.
    assert "function isAbort(" in _HTML
    assert 'AbortError' in _HTML
    assert "Canceled — your last protocol is unchanged." in _HTML


def test_retry_reinvokes_last_op():
    assert "LAST_OP = null" in _HTML or "let LAST_OP" in _HTML
    assert '$("#busy .busy-retry").onclick' in _HTML
    assert "op.fn()" in _HTML
    # Each mutating handler records LAST_OP before running.
    assert 'LAST_OP = { name: "revise"' in _HTML


def test_start_over_preserves_last_valid_and_identity():
    assert "function startOver()" in _HTML
    # Aborts, resets transient globals...
    assert "GAPS = [];" in _HTML
    assert "LAST_REVISE_INSTRUCTION = null;" in _HTML
    assert "PREV_PROTOCOL = null;" in _HTML
    # ...but re-renders the last valid protocol (CURR_PROTOCOL kept, not cleared).
    assert "if (CURR_PROTOCOL) { renderProtocol(CURR_PROTOCOL)" in _HTML


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
