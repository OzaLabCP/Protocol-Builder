"""Accessibility — table overflow wrapper + assertive announcer + provenance legend.

Served static/index.html source assertions (NO network, NO LLM), test_server ``get("/")``
harness. This is the structural a11y guard for item 6: every rendered protocol table is
mounted through tableWrap into a horizontally-scrollable ``.table-wrap`` region with a
caption and a thead/tbody hoist; the assertive ``#sr-alert`` live region exists; and the
provenance legend renders a plain-language sentence per tier.

Runnable directly (``python tests/test_a11y_tables.py``) or under pytest.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.server import app  # noqa: E402

_HTML = TestClient(app).get("/").text


def test_table_wrap_is_scrollable_region():
    assert "function tableWrap(" in _HTML
    # overflow-x auto on the wrap, focusable region role.
    assert ".table-wrap{overflow-x:auto" in _HTML
    assert 'wrap.setAttribute("role", "region")' in _HTML
    assert 'wrap.setAttribute("tabindex", "0")' in _HTML


def test_every_protocol_table_goes_through_tablewrap():
    # materials, titration, assumptions, and the deep-diff table are all wrapped.
    assert 'tableWrap(tbl, "Materials"' in _HTML
    assert 'tableWrap(tt, "Titration series worklist"' in _HTML
    assert 'tableWrap(tbl, "Assumptions log"' in _HTML
    assert 'tableWrap(table, "Before / after — this revision"' in _HTML


def test_tablewrap_builds_caption_thead_tbody():
    assert 'document.createElement("caption")' in _HTML
    assert 'document.createElement("thead")' in _HTML
    assert 'document.createElement("tbody")' in _HTML


def test_sr_alert_assertive_region_exists():
    assert 'id="sr-alert"' in _HTML
    assert 'role="alert"' in _HTML
    assert 'aria-live="assertive"' in _HTML
    assert "function announceBlocker(" in _HTML


def test_provenance_legend_has_plain_language_per_tier():
    assert "const PROV_EXPLAIN = {" in _HTML
    assert 'el("dl", "prov-legend")' in _HTML
    for tier in ("stated", "literature_grounded", "best_practice", "user_input", "default_verify"):
        assert tier in _HTML, f"missing legend tier {tier}"
    # each tier maps to a sentence, rendered as <dt> pill + <dd> sentence
    assert 'el("dd", null, esc(PROV_EXPLAIN[t] || ""))' in _HTML


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
