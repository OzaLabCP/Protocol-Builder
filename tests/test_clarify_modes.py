"""Clarification backend — host outcome-critical heuristic + answer-mode normalizer.

Pure-function tests (NO network, NO LLM): the host heuristic ``_is_outcome_critical``
(app.agent) and the single-source-of-truth answer normalizer ``_normalize_answer``
(app.server). The central invariant under test is that an EMPTY answer field can never
become a ``default`` — it is always ``unresolved`` — and that ``mode`` is required
semantics the host derives deterministically for every legacy answer shape.

Runnable directly (``python tests/test_clarify_modes.py``) or under pytest.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agent import _is_outcome_critical  # noqa: E402
from app.server import _normalize_answer  # noqa: E402


# --- (1) host outcome-critical heuristic -----------------------------------

def test_is_outcome_critical_user_dependent_true():
    assert _is_outcome_critical({"classification": "user_dependent",
                                 "parameter": "sample id", "why_it_matters": "labeling"}) is True


def test_is_outcome_critical_deferred_false():
    # deferred wins even when a keyword ("concentration") is present.
    assert _is_outcome_critical({"classification": "deferred",
                                 "why_it_matters": "final concentration of the drug"}) is False


def test_is_outcome_critical_keyword():
    assert _is_outcome_critical({"parameter": "buffer", "question": "how much?",
                                 "why_it_matters": "the MgCl2 concentration drives yield"}) is True


def test_is_outcome_critical_number_range():
    assert _is_outcome_critical({"parameter": "wells", "answer_type": "number",
                                 "plausible_max": 96}) is True
    assert _is_outcome_critical({"parameter": "wells", "answer_type": "number",
                                 "plausible_min": 1}) is True


def test_is_outcome_critical_model_flag_or():
    # A nice-to-have text gap with no host trigger, but the model flagged it -> True (OR).
    gap = {"parameter": "notebook page", "why_it_matters": "bookkeeping",
           "question": "which page", "outcome_critical": True}
    assert _is_outcome_critical(gap) is True


def test_is_outcome_critical_plain_false():
    gap = {"parameter": "notebook page", "why_it_matters": "bookkeeping",
           "question": "which page", "answer_type": "text"}
    assert _is_outcome_critical(gap) is False


# --- (1) answer-mode normalizer --------------------------------------------

def test_normalize_answer_explicit_modes():
    a = _normalize_answer({"id": "g1", "value": "50", "mode": "answered"})
    assert a["mode"] == "answered" and a["value"] == "50" and a["skipped"] is False
    d = _normalize_answer({"id": "g2", "value": None, "mode": "default"})
    assert d["mode"] == "default" and d["skipped"] is True
    u = _normalize_answer({"id": "g3", "value": None, "mode": "unresolved"})
    assert u["mode"] == "unresolved" and u["skipped"] is True


def test_normalize_empty_never_becomes_default():
    # The core invariant: an empty field with no mode is unresolved, NEVER default.
    for val in ("", "   ", None, []):
        a = _normalize_answer({"id": "g", "value": val})
        assert a["mode"] == "unresolved", f"empty value {val!r} must be unresolved"


def test_normalize_answered_with_empty_downgrades():
    a = _normalize_answer({"id": "g", "value": "", "mode": "answered"})
    assert a["mode"] == "unresolved"
    b = _normalize_answer({"id": "g", "value": [], "mode": "answered"})
    assert b["mode"] == "unresolved"


def test_normalize_legacy_skipped_no_value_unresolved():
    a = _normalize_answer({"id": "g", "skipped": True})
    assert a["mode"] == "unresolved" and a["skipped"] is True


def test_normalize_legacy_skipped_with_value_answered():
    a = _normalize_answer({"id": "g", "skipped": True, "value": "50"})
    assert a["mode"] == "answered" and a["value"] == "50" and a["skipped"] is False


def test_normalize_legacy_plain_value_answered():
    a = _normalize_answer({"id": "g", "value": "50"})
    assert a["mode"] == "answered" and a["skipped"] is False


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
