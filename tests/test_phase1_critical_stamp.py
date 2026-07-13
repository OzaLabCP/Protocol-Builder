"""Phase-1 stamp seam — every gap returned by ``request_clarifications`` is stamped with
a host-computed ``outcome_critical`` bool before it rides the ``phase1`` payload.

Fake OpenRouter client, NO network, NO key. Covers both stamp seams: the paper-first
``analyze()`` path and the hypothesis-first ``choose_assay()`` -> request_clarifications
path. Also confirms the model's own ``outcome_critical=true`` survives (OR-combine).

Runnable directly (``python tests/test_phase1_critical_stamp.py``) or under pytest.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import literature  # noqa: E402
from app.agent import GapFillerAgent, Session  # noqa: E402


class FakeLLM:
    def __init__(self, queue):
        self.queue = list(queue); self.calls = []

    def chat(self, messages, tools=None, tool_choice=None, model=None, effort=None, max_tokens=None):
        self.calls.append({"messages": messages}); return self.queue.pop(0)


def tool_msg(name, args, cid):
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": cid, "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)}}]}}]}


def make_agent(queue):
    agent = GapFillerAgent(client=FakeLLM(queue), model="fake")
    literature.search_pubmed = lambda q, retmax=5: []
    return agent


_GAPS = {
    "usable": True,
    "gaps": [
        # host keyword trigger (concentration)
        {"id": "g1", "parameter": "MgCl2", "why_it_matters": "concentration drives yield",
         "question": "how much?", "answer_type": "number", "plausible_max": 20},
        # plainly nice-to-have
        {"id": "g2", "parameter": "notebook page", "why_it_matters": "bookkeeping",
         "question": "which page?", "answer_type": "text"},
        # deferred -> False even though it mentions temperature
        {"id": "g3", "parameter": "storage", "why_it_matters": "temperature of freezer",
         "question": "which freezer?", "classification": "deferred"},
    ],
}


def test_phase1_stamps_outcome_critical():
    agent = make_agent([tool_msg("request_clarifications", _GAPS, "c1")])
    session = agent.analyze("A real methods section describing a CFPS reaction with S30 extract "
                            "and PANOx-SP energy mix incubated for some time.")
    gaps = session.phase1["gaps"]
    # Every gap carries an explicit bool (graceful-degradation guarantee for the client).
    assert all(isinstance(g["outcome_critical"], bool) for g in gaps)
    by_id = {g["id"]: g["outcome_critical"] for g in gaps}
    assert by_id["g1"] is True    # keyword + number-range
    assert by_id["g2"] is False   # nothing triggers
    assert by_id["g3"] is False   # deferred wins over keyword


def test_phase1_stamp_respects_model_flag():
    # A gap with no host trigger but the model set outcome_critical=true stays True.
    gaps = {"usable": True, "gaps": [
        {"id": "m1", "parameter": "notebook page", "why_it_matters": "bookkeeping",
         "question": "which page?", "answer_type": "text", "outcome_critical": True}]}
    agent = make_agent([tool_msg("request_clarifications", gaps, "c1")])
    session = agent.analyze("A real methods section describing a CFPS reaction with S30 extract "
                            "and an energy mix incubated at room temperature for a while.")
    assert session.phase1["gaps"][0]["outcome_critical"] is True


def test_choose_assay_path_also_stamps():
    # Seed a session that already emitted assay options; choosing one drives the
    # request_clarifications phase, which must stamp exactly like analyze().
    session = Session(source_kind="hypothesis",
                      messages=[{"role": "user", "content": "seed"}],
                      pending_tool_use_id="a1", hypothesis="X increases Y")
    session.assay_options = {"assays": [
        {"id": "assay1", "name": "Binding assay", "measures": "Kd",
         "critical_comparison": "bound vs free"}]}
    agent = make_agent([tool_msg("request_clarifications", _GAPS, "c1")])
    phase1 = agent.choose_assay(session, "assay1")
    by_id = {g["id"]: g["outcome_critical"] for g in phase1["gaps"]}
    assert all(isinstance(v, bool) for v in by_id.values())
    assert by_id["g1"] is True and by_id["g2"] is False and by_id["g3"] is False
    assert session.phase1["gaps"][0]["outcome_critical"] is True


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
