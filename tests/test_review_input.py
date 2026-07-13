"""Epic-3 fresh-context reviewer-input builder (`app.agent._render_review_input`).

The correctness review must audit from a FRESH message history that contains ONLY the
whitelisted, host-rendered blocks — never the authoring transcript's reasoning. These
tests exercise the pure builder directly (no model, no network):

  * the five blocks are all present, in order;
  * NOTHING from a decoy `session.messages` leaks into the payload (requirement 1);
  * an oversized source is head+tail summarized and flagged; a lossy source declares
    `source_exact: false`; an absent source renders the hypothesis-first marker.

Runnable directly (`python tests/test_review_input.py`) or under pytest.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agent import _SOURCE_CAP, _render_review_input  # noqa: E402

_PROTO = {"title": "Assay X", "summary": "s", "estimated_duration": "1 h",
          "materials": [], "steps": [{"instruction": "mix"}], "assumptions_log": []}

_HEADERS = [
    "=== SOURCE",
    "=== PROTOCOL (JSON) ===",
    "=== USER DECISIONS ===",
    "=== RETRIEVED EVIDENCE (searches run) ===",
    "=== DETERMINISTIC VALIDATION FINDINGS (quality_gate) ===",
]


def test_render_review_input_includes_five_blocks_in_order():
    payload = _render_review_input(
        _PROTO, source_text="Methods: incubate at 30 C.",
        decisions=[{"question": "temp?", "answer": "30 C"}],
        grounding_log=["search_pubmed: CFPS incubation"],
        quality_gate={"status": "ok", "counts": {"errors": 0}},
    )
    positions = [payload.find(h) for h in _HEADERS]
    assert all(p >= 0 for p in positions), positions        # every block present
    assert positions == sorted(positions)                    # and in the specified order
    # the instruction is appended last, after the deterministic-findings block
    assert positions[-1] < payload.rindex("emit_correctness_review")


def test_render_review_input_excludes_authoring_transcript():
    """Requirement (1): the reviewer sees a fresh history. A decoy `session.messages`
    carrying the authoring chain-of-thought is NOT an input to the builder and CANNOT
    appear in the payload."""
    secret = "SECRET_AUTHORING_REASONING_because_I_assumed_the_buffer_was_fine"
    # The builder's signature has no `messages`/`session` parameter — by construction it
    # can only render the artifact + host-owned ground truth. Prove the transcript text is
    # absent even when it would have been the model's self-justification.
    payload = _render_review_input(
        _PROTO, source_text="Methods: incubate at 30 C.",
        decisions=[{"question": "temp?", "answer": "30 C"}],
        grounding_log=["search_pubmed: CFPS"],
        quality_gate={"status": "ok"},
    )
    assert secret not in payload
    # sanity: the artifact under audit IS present, so the exclusion is meaningful
    assert "Assay X" in payload


def test_render_review_input_source_full_and_exact_for_short_paste():
    text = "Methods: reactions incubated at 30 C for 4 h in 50 uL wells."
    payload = _render_review_input(_PROTO, source_text=text, source_exact=True)
    assert "=== SOURCE (methods) ===" in payload
    assert "source_exact: true" in payload
    assert text in payload                                    # full text, not summarized
    assert "(summarized)" not in payload


def test_render_review_input_source_summarized_and_lossy_flag():
    big = "HEAD_MARKER " + ("x" * (_SOURCE_CAP + 5000)) + " TAIL_MARKER"
    payload = _render_review_input(_PROTO, source_text=big, source_exact=False)
    assert "source_exact: false" in payload                   # lossy PDF not over-trusted
    assert "summarized" in payload                            # head+tail summary emitted
    assert "HEAD_MARKER" in payload and "TAIL_MARKER" in payload  # head+tail retained
    assert "[middle omitted]" in payload
    assert len(payload) < len(big)                            # middle actually dropped


def test_render_review_input_hypothesis_first_source_marker():
    payload = _render_review_input(_PROTO, source_text=None)
    assert "=== SOURCE === (none — hypothesis-first draft)" in payload
    assert "source_exact" not in payload                      # no exactness claim without a source


def test_render_review_input_empty_optional_blocks_render_placeholders():
    payload = _render_review_input(_PROTO, source_text=None, decisions=None,
                                   grounding_log=None, quality_gate=None)
    assert "=== USER DECISIONS ===" in payload
    assert "(no searches run)" in payload                     # empty grounding log placeholder
    assert "=== DETERMINISTIC VALIDATION FINDINGS (quality_gate) ===" in payload


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
