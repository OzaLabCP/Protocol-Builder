"""Agent phase-3 mode consumption — ``continue_with_answers`` turns per-gap answer modes
into an explicit host directive appended to the transcript, and stamps ``session.decisions``
with each mode. Fake OpenRouter client, NO network, NO key.

The directive is how ``default``/``unresolved`` choices reach the model deterministically:
- ``default``   -> "USE its suggested_default ... default_verify ... open_question"
- ``unresolved``-> "LEAVE UNRESOLVED ... open_question" (no fabricated value)
- ``answered``  -> "use the provided value"
- ``answers==[]``-> no directive at all (preserves the no-gap fast path).

Runnable directly (``python tests/test_continue_modes.py``) or under pytest.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import literature  # noqa: E402
from app.agent import GapFillerAgent, Session  # noqa: E402

PROTO = {"title": "P", "summary": "s", "estimated_duration": "1 h",
         "materials": [], "steps": [], "assumptions_log": []}


class FakeLLM:
    def __init__(self, queue):
        self.queue = list(queue); self.calls = []

    def chat(self, messages, tools=None, tool_choice=None, model=None, effort=None, max_tokens=None):
        self.calls.append({"tool_choice": tool_choice}); return self.queue.pop(0)


def tool_msg(name, args, cid):
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": cid, "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)}}]}}]}


def make_agent(queue):
    agent = GapFillerAgent(client=FakeLLM(queue), model="fake")
    literature.search_pubmed = lambda q, retmax=5: []
    return agent


def _directive_of(session):
    """The last host directive user-message appended by continue_with_answers, or None."""
    for m in session.messages:
        if m.get("role") == "user" and isinstance(m.get("content"), str) \
                and "Honor these modes exactly" in m["content"]:
            return m["content"]
    return None


def _fresh_session():
    return Session(messages=[{"role": "user", "content": "seed"}], request_tool_use_id="c1")


def test_continue_with_answers_default_mode_directive():
    session = _fresh_session()
    agent = make_agent([tool_msg("emit_protocol", PROTO, "e1")])
    out = agent.continue_with_answers(
        session, answers=[{"id": "g1", "value": "20 mM", "mode": "default"}])
    assert out["title"] == "P"
    d = _directive_of(session)
    assert d is not None
    assert "suggested_default" in d and "default_verify" in d and "open_question" in d
    assert "g1" in d


def test_continue_with_answers_unresolved_mode_directive():
    session = _fresh_session()
    agent = make_agent([tool_msg("emit_protocol", PROTO, "e1")])
    agent.continue_with_answers(
        session, answers=[{"id": "g2", "value": None, "mode": "unresolved"}])
    d = _directive_of(session)
    assert d is not None
    assert "LEAVE UNRESOLVED" in d and "open_question" in d
    assert "do not fabricate" in d.lower()
    assert "g2" in d


def test_continue_with_answers_answered_mode():
    session = _fresh_session()
    agent = make_agent([tool_msg("emit_protocol", PROTO, "e1")])
    out = agent.continue_with_answers(
        session, answers=[{"id": "g3", "value": "50", "mode": "answered"}])
    assert out["title"] == "P"
    d = _directive_of(session)
    assert d is not None and "use the provided value" in d and "g3" in d
    # Still terminates by emitting (regression guard for test_phase2_grounds_then_emits).
    assert session.pending_tool_use_id == "e1"
    assert session.request_tool_use_id is None


def test_continue_with_answers_empty_answers_no_directive():
    session = _fresh_session()
    agent = make_agent([tool_msg("emit_protocol", PROTO, "e1")])
    out = agent.continue_with_answers(session, answers=[])
    assert out["title"] == "P"
    # Fast path: no directive appended when there are no answers.
    assert _directive_of(session) is None


def test_decisions_carry_mode():
    session = _fresh_session()
    session.phase1 = {"questions": ["What Mg concentration?", "What temperature?"]}
    agent = make_agent([tool_msg("emit_protocol", PROTO, "e1")])
    agent.continue_with_answers(session, answers=[
        {"id": "g1", "value": "20 mM", "mode": "answered"},
        {"id": "g2", "value": None, "mode": "unresolved"}])
    assert len(session.decisions) == 2
    assert all("mode" in d for d in session.decisions)
    assert session.decisions[0]["mode"] == "answered"
    assert session.decisions[1]["mode"] == "unresolved"


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
