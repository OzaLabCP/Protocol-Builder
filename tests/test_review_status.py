"""Epic-3 L3 response-label projection (`app.server.compute_review_status`).

`review_status` is the single 5-value response label, and it is decided by a PURE,
host-owned function — never by a model. These tests pin the exact rule table (spec §4.2,
first match wins) so each of the five labels is reachable under the right conditions
(requirement 5):

    1. not attempted                     -> skipped
    2. audit/verifier raised             -> unavailable
    3. deterministic gate blocked        -> failed
    4. unresolved (blocking or remain)   -> failed
    5. verified_clean + findings existed -> passed_with_findings_fixed
    6. otherwise                         -> passed

No network, no model.

Runnable directly (`python tests/test_review_status.py`) or under pytest.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.server import REVIEW_STATUS, compute_review_status  # noqa: E402


def _report(gate_status="ok"):
    return {"quality_gate": {"status": gate_status, "counts": {"errors": 0, "warnings": 0}}}


def test_all_labels_are_in_the_vocab():
    assert REVIEW_STATUS == {"passed", "passed_with_findings_fixed", "failed",
                             "unavailable", "skipped"}


def test_skipped_when_not_attempted():
    assert compute_review_status(None, None, review_attempted=False) == "skipped"
    # skipped wins even if a gate/fv would otherwise say something else
    fv = {"status": "issues_remain", "unresolved_blocking": True}
    assert compute_review_status(_report("blocked"), fv, review_attempted=False) == "skipped"


def test_unavailable_on_audit_error():
    assert compute_review_status(_report("ok"), None, review_attempted=True,
                                 audit_status="unavailable") == "unavailable"


def test_unavailable_on_verifier_error():
    fv = {"status": "not_reviewed", "reason": "verifier_error", "checked": False}
    assert compute_review_status(_report("ok"), fv, review_attempted=True) == "unavailable"


def test_failed_on_gate_blocked_precedes_verified_clean():
    # rule 3 (gate blocked) beats rule 5 (verified_clean): never ship a gate-blocked proto
    fv = {"status": "verified_clean", "reviewed_count": 2, "unresolved_blocking": False}
    assert compute_review_status(_report("blocked"), fv, review_attempted=True) == "failed"


def test_failed_on_issues_remain():
    fv = {"status": "issues_remain", "unresolved_blocking": False, "reviewed_count": 1}
    assert compute_review_status(_report("ok"), fv, review_attempted=True) == "failed"


def test_failed_on_unresolved_blocking_even_if_status_not_issues_remain():
    fv = {"status": "verified_clean", "unresolved_blocking": True, "reviewed_count": 1}
    assert compute_review_status(_report("ok"), fv, review_attempted=True) == "failed"


def test_passed_with_findings_fixed():
    fv = {"status": "verified_clean", "unresolved_blocking": False, "reviewed_count": 3}
    assert compute_review_status(_report("ok"), fv, review_attempted=True) \
        == "passed_with_findings_fixed"


def test_passed_when_verified_clean_but_zero_reviewed():
    fv = {"status": "verified_clean", "unresolved_blocking": False, "reviewed_count": 0}
    assert compute_review_status(_report("ok"), fv, review_attempted=True) == "passed"


def test_passed_on_no_original_findings_is_not_unavailable():
    # not_reviewed + no_original_findings is a clean run, NOT an error -> passed
    fv = {"status": "not_reviewed", "reason": "no_original_findings", "checked": False}
    assert compute_review_status(_report("ok"), fv, review_attempted=True) == "passed"


def test_passed_on_gate_warnings_only():
    assert compute_review_status(_report("warnings"), None, review_attempted=True) == "passed"


def test_passed_when_no_fix_verification():
    assert compute_review_status(_report("ok"), None, review_attempted=True) == "passed"


def test_every_result_is_in_the_vocab():
    cases = [
        (None, None, False, None),
        (_report("ok"), None, True, "unavailable"),
        (_report("blocked"), None, True, None),
        (_report("ok"), {"status": "issues_remain"}, True, None),
        (_report("ok"), {"status": "verified_clean", "reviewed_count": 1}, True, None),
        (_report("ok"), None, True, None),
    ]
    for report, fv, attempted, audit in cases:
        assert compute_review_status(report, fv, review_attempted=attempted,
                                     audit_status=audit) in REVIEW_STATUS


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
