"""Agent loop tests with a fake Anthropic client — verify the client-tool
dispatch (search_pubmed) executes and the loop drives to the terminal tool.
No network, no API key."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import literature  # noqa: E402
from app.agent import GapFillerAgent, RunState, Session  # noqa: E402


class Block:
    def __init__(self, name, id, tool_input):
        self.type = "tool_use"
        self.name = name
        self.id = id
        self.input = tool_input


class Resp:
    def __init__(self, content, stop_reason="tool_use"):
        self.content = content
        self.stop_reason = stop_reason


class FakeMessages:
    def __init__(self, queue):
        self.queue = list(queue)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.queue.pop(0)


class FakeClient:
    def __init__(self, queue):
        self.messages = FakeMessages(queue)


def make_agent(queue, monkeypatch_search=True):
    agent = GapFillerAgent(client=FakeClient(queue), model="fake-model")
    if monkeypatch_search:
        literature.search_pubmed = lambda q, retmax=5: [
            {"pmid": "12345678", "title": "Grounding source", "authors": "Doe J", "year": 2020, "doi": "10.1/x"}
        ]
    return agent


def test_phase1_dispatches_pubmed_then_returns_clarifications():
    queue = [
        Resp([Block("search_pubmed", "s1", {"query": "PANOx-SP incubation time"})]),
        Resp([Block("request_clarifications", "c1", {"usable": True, "gaps": []})]),
    ]
    agent = make_agent(queue)
    session = agent.analyze("A real methods section describing a CFPS reaction with S30 extract.")
    assert session.phase1 == {"usable": True, "gaps": []}
    assert session.request_tool_use_id == "c1"
    assert session.grounding_log == ["search_pubmed: PANOx-SP incubation time"]
    # a tool_result for the search must have been fed back
    user_turns = [m for m in session.messages if m["role"] == "user"]
    assert any(
        isinstance(m["content"], list) and m["content"][0].get("type") == "tool_result"
        for m in user_turns
    )


def test_phase2_grounds_then_emits():
    session = Session(messages=[{"role": "user", "content": "seed"}], request_tool_use_id="c1")
    protocol = {"title": "P", "summary": "s", "estimated_duration": "1 h", "materials": [], "steps": [], "assumptions_log": []}
    queue = [
        Resp([Block("search_pubmed", "s1", {"query": "Mg2+ optimum CFPS"})]),
        Resp([Block("emit_protocol", "e1", protocol)]),
    ]
    agent = make_agent(queue)
    out = agent.continue_with_answers(session, answers=[{"id": "g1", "value": "50", "skipped": False}])
    assert out["title"] == "P"
    assert session.request_tool_use_id is None  # consumed


def test_phase2_nudges_when_no_emit():
    session = Session(messages=[{"role": "user", "content": "seed"}], request_tool_use_id="c1")
    protocol = {"title": "P", "summary": "s", "estimated_duration": "1 h", "materials": [], "steps": [], "assumptions_log": []}
    queue = [
        Resp([], stop_reason="end_turn"),  # model ends without emitting
        Resp([Block("emit_protocol", "e1", protocol)]),  # after the nudge
    ]
    agent = make_agent(queue)
    out = agent.continue_with_answers(session, answers=[])
    assert out["title"] == "P"
    # a nudge user message was appended before the second run
    assert any(m["role"] == "user" and isinstance(m["content"], str) and "emit_protocol" in m["content"]
               for m in session.messages)


def test_revise_reemits_with_correction():
    protocol = {"title": "P2", "summary": "s", "estimated_duration": "1 h", "materials": [], "steps": [], "assumptions_log": []}
    session = Session(messages=[{"role": "user", "content": "seed"}], pending_tool_use_id="e0")
    queue = [Resp([Block("emit_protocol", "e1", protocol)])]
    agent = make_agent(queue)
    out = agent.revise(session, "use 150 uL wells")
    assert out["title"] == "P2"
    assert session.pending_tool_use_id == "e1"  # updated to the new emit
    # a tool_result for the previous emit + the correction text were appended
    last_user = [m for m in session.messages if m["role"] == "user"][-1]
    kinds = [b.get("type") for b in last_user["content"]]
    assert "tool_result" in kinds and "text" in kinds


def test_design_review_emits():
    review = {"question": "q", "hypothesis": "h",
              "variables": {"independent": [], "dependent": [], "controlled": []},
              "controls": [], "readout": {"measures": "m", "answers_question": True},
              "replication": {"rationale": "r"}, "expected_results": [], "interpretation_limits": []}
    session = Session(messages=[{"role": "user", "content": "seed"}], pending_tool_use_id="e1")
    queue = [Resp([Block("emit_design_review", "d1", review)])]
    agent = make_agent(queue)
    out = agent.design_review(session)
    assert out["hypothesis"] == "h"
    assert session.pending_tool_use_id == "d1"  # can chain further follow-ups


def test_pubmed_budget_exhaustion():
    agent = make_agent([])  # no queue needed; test _dispatch directly
    literature.search_pubmed = lambda q, retmax=5: [{"pmid": "1", "title": "t", "authors": "A", "year": 2020, "doi": None}]
    state = RunState(session=Session(), searches_left=1)
    first = agent._dispatch("search_pubmed", {"query": "a"}, state)
    second = agent._dispatch("search_pubmed", {"query": "b"}, state)
    assert "PMID 1" in first
    assert "budget exhausted" in second.lower()


def test_dispatch_state_is_per_call_not_shared():
    """Concurrency guard: one shared agent must not share budget/session across
    calls — each RunState is independent."""
    agent = make_agent([])
    literature.search_pubmed = lambda q, retmax=5: [{"pmid": "1", "title": "t", "authors": "A", "year": 2020, "doi": None}]
    a = RunState(session=Session(), searches_left=1)
    b = RunState(session=Session(), searches_left=1)
    agent._dispatch("search_pubmed", {"query": "qa"}, a)  # spends a's budget
    # b still has its own budget and its own grounding log
    out_b = agent._dispatch("search_pubmed", {"query": "qb"}, b)
    assert "PMID 1" in out_b
    assert a.searches_left == 0 and b.searches_left == 0
    assert a.session.grounding_log == ["search_pubmed: qa"]
    assert b.session.grounding_log == ["search_pubmed: qb"]  # not cross-contaminated


if __name__ == "__main__":
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
