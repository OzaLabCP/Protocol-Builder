"""Agent-loop tests with a fake OpenRouter client — verify the client-tool dispatch
(search_pubmed) executes and the loop drives to the terminal tool. No network, no key.

The fake returns OpenAI/OpenRouter-shaped responses; the agent talks to it via
`.chat(...)` exactly as it would to the real provider."""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, literature  # noqa: E402
from app.agent import AgentError, GapFillerAgent, RunState, Session  # noqa: E402
from app.prompts import DISCOVERY_SYSTEM_PROMPT, SYSTEM_ASK, SYSTEM_PROMPT  # noqa: E402


class FakeLLM:
    """Returns queued responses; records the requests it was called with."""

    def __init__(self, queue):
        self.queue = list(queue)
        self.calls = []

    def chat(self, messages, tools=None, tool_choice=None, model=None, effort=None, max_tokens=None):
        self.calls.append({"messages": messages, "tools": tools,
                           "tool_choice": tool_choice, "model": model, "effort": effort,
                           "max_tokens": max_tokens})
        return self.queue.pop(0)


def tool_msg(*calls):
    """calls: (name, args_dict, id) tuples -> an assistant turn with those tool_calls."""
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None,
        "tool_calls": [
            {"id": cid, "type": "function",
             "function": {"name": name, "arguments": json.dumps(args)}}
            for (name, args, cid) in calls
        ]}}]}


def text_msg(text):
    return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": text}}]}


def _text_of(content):
    """Read a message's text whether it's a plain string or cache-marked content parts."""
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content)
    return content


PROTO = {"title": "P", "summary": "s", "estimated_duration": "1 h",
         "materials": [], "steps": [], "assumptions_log": []}


def make_agent(queue, monkeypatch_search=True):
    agent = GapFillerAgent(client=FakeLLM(queue), model="fake")
    if monkeypatch_search:
        literature.search_pubmed = lambda q, retmax=5: [
            {"pmid": "12345678", "title": "Grounding source", "authors": "Doe J", "year": 2020, "doi": "10.1/x"}
        ]
    return agent


def test_phase1_dispatches_pubmed_then_returns_clarifications():
    queue = [
        tool_msg(("search_pubmed", {"query": "PANOx-SP incubation time"}, "s1")),
        tool_msg(("request_clarifications", {"usable": True, "gaps": []}, "c1")),
    ]
    agent = make_agent(queue)
    session = agent.analyze("A real methods section describing a CFPS reaction with S30 extract.")
    assert session.phase1 == {"usable": True, "gaps": []}
    assert session.request_tool_use_id == "c1"
    assert session.grounding_log == ["search_pubmed: PANOx-SP incubation time"]
    # a tool message answering the search was fed back
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == "s1" for m in session.messages)
    # phase 1 runs under the scoped ASK prompt (no emitting rules), not the full prompt
    assert agent.client.calls[0]["messages"][0] == {"role": "system", "content": SYSTEM_ASK}


def test_phase2_grounds_then_emits():
    session = Session(messages=[{"role": "user", "content": "seed"}], request_tool_use_id="c1")
    queue = [
        tool_msg(("search_pubmed", {"query": "Mg2+ optimum CFPS"}, "s1")),
        tool_msg(("emit_protocol", PROTO, "e1")),
    ]
    agent = make_agent(queue)
    out = agent.continue_with_answers(session, answers=[{"id": "g1", "value": "50", "skipped": False}])
    assert out["title"] == "P"
    assert session.request_tool_use_id is None  # consumed
    assert session.pending_tool_use_id == "e1"


def test_phase2_nudges_when_no_emit():
    session = Session(messages=[{"role": "user", "content": "seed"}], request_tool_use_id="c1")
    queue = [
        text_msg("Let me think about this..."),      # model ends with text, no tool call
        tool_msg(("emit_protocol", PROTO, "e1")),     # after the forced nudge
    ]
    agent = make_agent(queue)
    out = agent.continue_with_answers(session, answers=[])
    assert out["title"] == "P"
    # the nudge forced the terminal tool
    assert agent.client.calls[-1]["tool_choice"] == {"type": "function", "function": {"name": "emit_protocol"}}
    assert any(m["role"] == "user" and isinstance(m["content"], str) and "emit_protocol" in m["content"]
               for m in session.messages)


def test_revise_reemits_with_correction():
    proto2 = dict(PROTO, title="P2")
    session = Session(messages=[{"role": "user", "content": "seed"}], pending_tool_use_id="e0")
    queue = [tool_msg(("emit_protocol", proto2, "e1"))]
    agent = make_agent(queue)
    out = agent.revise(session, "use 150 uL wells")
    assert out["title"] == "P2"
    assert session.pending_tool_use_id == "e1"  # updated to the new emit
    # the prior emit was acked with a tool message, and the correction was appended
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == "e0" and m.get("content") == "Received."
               for m in session.messages)
    assert any(m["role"] == "user" and "150 uL" in m["content"] for m in session.messages)


def test_design_review_emits():
    review = {"question": "q", "hypothesis": "h",
              "variables": {"independent": [], "dependent": [], "controlled": []},
              "controls": [], "readout": {"measures": "m", "answers_question": True},
              "replication": {"rationale": "r"}, "expected_results": [], "interpretation_limits": []}
    session = Session(messages=[{"role": "user", "content": "seed"}], pending_tool_use_id="e1")
    queue = [tool_msg(("emit_design_review", review, "d1"))]
    agent = make_agent(queue)
    out = agent.design_review(session)
    assert out["hypothesis"] == "h"
    assert session.pending_tool_use_id == "d1"  # can chain further follow-ups


def test_correctness_review_emits():
    review = {"verdict": "issues_found", "summary": "one issue",
              "findings": [{"severity": "critical", "category": "ordering",
                            "problem": "enzyme before buffer", "fix": "add buffer first"}],
              "strengths": []}
    session = Session(messages=[{"role": "user", "content": "seed"}], pending_tool_use_id="e1")
    agent = make_agent([tool_msg(("emit_correctness_review", review, "cr1"))])
    out = agent.correctness_review(session)
    assert out["verdict"] == "issues_found"
    assert out["findings"][0]["category"] == "ordering"
    assert session.pending_tool_use_id == "cr1"


def test_apply_correctness_fixes_reemits():
    session = Session(messages=[{"role": "user", "content": "seed"}], pending_tool_use_id="e1")
    findings = [{"severity": "critical", "category": "ordering", "location": "step 3",
                 "problem": "enzyme before buffer", "fix": "add buffer before enzyme"},
                {"severity": "minor", "problem": "no fix here"}]  # skipped (no fix)
    agent = make_agent([tool_msg(("emit_protocol", dict(PROTO, title="Fixed"), "e2"))])
    out = agent.apply_correctness_fixes(session, findings)
    assert out["title"] == "Fixed"
    instr = [m for m in session.messages if m["role"] == "user"][-1]["content"]
    assert "add buffer before enzyme" in instr and "Apply EVERY fix" in instr
    assert session.pending_tool_use_id == "e2"


def test_apply_correctness_fixes_no_fixes_raises():
    session = Session(messages=[{"role": "user", "content": "seed"}], pending_tool_use_id="e1")
    agent = make_agent([])  # raises before any model call
    try:
        agent.apply_correctness_fixes(session, [{"severity": "minor", "problem": "x"}])
        assert False, "expected AgentError"
    except AgentError:
        pass


def test_discover_emits_assay_options():
    opts = {"usable": True, "hypothesis_restated": "H",
            "assays": [{"id": "fp", "name": "FP", "measures": "m", "why_tests_hypothesis": "w",
                        "critical_comparison": "c", "throughput": "high", "difficulty": "low",
                        "materials_burden": "cheap", "key_limitation": "k", "provenance": "best_practice"}],
            "recommended_assay_id": "fp", "recommendation_rationale": "r"}
    queue = [
        tool_msg(("search_pubmed", {"query": "FP binding assay"}, "s1")),
        tool_msg(("emit_assay_options", opts, "a1")),
    ]
    agent = make_agent(queue)
    session = agent.discover("Does adding X increase binding of Y?", {"equipment": "plate reader"})
    assert session.source_kind == "hypothesis"
    assert session.hypothesis == "Does adding X increase binding of Y?"
    assert session.assay_options["assays"][0]["id"] == "fp"
    assert session.pending_tool_use_id == "a1"
    assert session.grounding_log == ["search_pubmed: FP binding assay"]
    # discovery ran under the discovery system prompt
    assert agent.client.calls[0]["messages"][0]["content"] == DISCOVERY_SYSTEM_PROMPT


def test_discover_rejected_returns_unusable():
    queue = [tool_msg(("emit_assay_options", {"usable": False, "reason": "not a testable hypothesis"}, "a1"))]
    agent = make_agent(queue)
    session = agent.discover("banana banana banana")
    assert session.assay_options["usable"] is False
    assert session.assay_options["reason"] == "not a testable hypothesis"
    assert session.pending_tool_use_id == "a1"


def test_choose_assay_threads_to_request_clarifications():
    session = Session(
        messages=[{"role": "user", "content": "seed"}],
        assay_options={"assays": [{"id": "x", "name": "X", "measures": "m", "critical_comparison": "c"}]},
        pending_tool_use_id="a1", source_kind="hypothesis", hypothesis="H",
    )
    queue = [tool_msg(("request_clarifications", {"usable": True, "gaps": []}, "c1"))]
    agent = make_agent(queue)
    phase1 = agent.choose_assay(session, "x")
    assert session.chosen_assay["id"] == "x"
    assert session.request_tool_use_id == "c1"
    assert session.pending_tool_use_id is None  # re-threaded
    assert phase1 == {"usable": True, "gaps": []}
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == "a1" for m in session.messages)
    brief = [m for m in session.messages if m["role"] == "user"][-1]["content"]
    assert brief.startswith("=== DESIGN BRIEF ===")


def test_choose_assay_unknown_id_raises():
    session = Session(messages=[{"role": "user", "content": "seed"}],
                      assay_options={"assays": [{"id": "x"}]}, pending_tool_use_id="a1")
    agent = make_agent([])  # empty queue: raising before any model call proves no spend
    try:
        agent.choose_assay(session, "nope")
        assert False, "expected AgentError"
    except AgentError:
        pass


def test_hypothesis_first_reaches_emit_protocol():
    session = Session(messages=[{"role": "user", "content": "seed"}],
                      request_tool_use_id="c1", source_kind="hypothesis")
    queue = [tool_msg(("emit_protocol", PROTO, "e1"))]
    agent = make_agent(queue)
    out = agent.continue_with_answers(session, answers=[])
    assert out["title"] == "P"  # hypothesis-first converges on the untouched phase-2/3 path


def test_analyze_retains_source_text_and_is_exact_for_paste():
    queue = [tool_msg(("request_clarifications", {"usable": True, "gaps": []}, "c1"))]
    agent = make_agent(queue)
    text = "A methods section: reactions incubated at 30 C for 4 h in 50 uL."
    session = agent.analyze(text)
    assert session.source_text == text  # retained for host-side quote verification
    assert session.source_exact is True  # pasted text is exactly what the model read


def test_analyze_full_paper_is_lossy():
    queue = [tool_msg(("request_clarifications", {"usable": True, "gaps": []}, "c1"))]
    agent = make_agent(queue)
    session = agent.analyze("Full paper text ... Methods: incubated at 30 C ...", is_full_paper=True)
    assert session.source_exact is False  # PDF-extracted text is lossy vs the paper
    # inline (no line-start Methods heading) -> extraction declines, full text is shipped
    assert "=== PAPER TEXT ===" in _text_of(session.messages[0]["content"])


def test_analyze_captures_hypothesis_and_injects_preamble():
    queue = [tool_msg(("request_clarifications", {"usable": True, "gaps": []}, "c1"))]
    agent = make_agent(queue)
    session = agent.analyze("A methods section describing a CFPS reaction with S30 extract.",
                            hypothesis="Adding DsbC increases folded scFv yield")
    assert session.hypothesis == "Adding DsbC increases folded scFv yield"
    assert "Adding DsbC increases folded scFv yield" in _text_of(session.messages[0]["content"])


def test_pubmed_budget_exhaustion():
    agent = make_agent([])  # no queue needed; test _dispatch directly
    literature.search_pubmed = lambda q, retmax=5: [{"pmid": "1", "title": "t", "authors": "A", "year": 2020, "doi": None}]
    state = RunState(session=Session(), searches_left=1)
    first = agent._dispatch("search_pubmed", {"query": "a"}, state)
    second = agent._dispatch("search_pubmed", {"query": "b"}, state)
    assert "PMID 1" in first
    assert "budget exhausted" in second.lower()


def test_dispatch_state_is_per_call_not_shared():
    """Concurrency guard: one shared agent must not share budget/session across calls."""
    agent = make_agent([])
    literature.search_pubmed = lambda q, retmax=5: [{"pmid": "1", "title": "t", "authors": "A", "year": 2020, "doi": None}]
    a = RunState(session=Session(), searches_left=1)
    b = RunState(session=Session(), searches_left=1)
    agent._dispatch("search_pubmed", {"query": "qa"}, a)
    out_b = agent._dispatch("search_pubmed", {"query": "qb"}, b)
    assert "PMID 1" in out_b
    assert a.searches_left == 0 and b.searches_left == 0
    assert a.session.grounding_log == ["search_pubmed: qa"]
    assert b.session.grounding_log == ["search_pubmed: qb"]


# --- token-efficiency levers ------------------------------------------------

def test_source_message_is_cache_marked_when_enabled():
    # caching is on by default for openrouter -> the source message carries a cache breakpoint
    from app import config
    assert config.PROMPT_CACHE is True
    agent = make_agent([tool_msg(("request_clarifications", {"usable": True, "gaps": []}, "c1"))])
    session = agent.analyze("A methods section: reactions incubated at 30 C for 4 h in 50 uL wells.")
    content = session.messages[0]["content"]
    assert isinstance(content, list) and content[0]["cache_control"] == {"type": "ephemeral"}
    assert "30 C" in content[0]["text"]
    # the system message is NOT reshaped (the prefix cache covers it): stays a plain string
    assert agent.client.calls[0]["messages"][0] == {"role": "system", "content": SYSTEM_ASK}


def test_full_paper_trimmed_to_methods_section():
    paper = (
        "Abstract\nWe engineered a biosensor and characterized it.\n\n"
        "Introduction\nProtein biosensors are useful for many reasons discussed here at length.\n\n"
        "Methods\nCells were grown to OD 0.6 at 37 C, induced with 0.5 mM IPTG, and lysed in "
        "RIPA buffer. Lysates were clarified at 12000 g for 10 min and protein quantified by BCA. "
        "Samples were resolved by SDS-PAGE and transferred to PVDF for immunoblotting.\n\n"
        "Results\nThe biosensor showed a strong response we quantified as ratio changes.\n\n"
        "References\n1. Somebody et al., 2019.\n"
    )
    agent = make_agent([tool_msg(("request_clarifications", {"usable": True, "gaps": []}, "c1"))])
    session = agent.analyze(paper, is_full_paper=True)
    sent = _text_of(session.messages[0]["content"])
    assert "=== METHODS (extracted) ===" in sent          # trimmed path, not full-paper path
    assert "RIPA buffer" in sent                            # methods content kept
    assert "Introduction" not in sent and "Results" not in sent  # noise dropped
    assert session.source_text and "RIPA buffer" in session.source_text  # verify against what it read
    assert "Introduction" not in session.source_text


def _trunc():
    return {"choices": [{"finish_reason": "length",
                         "message": {"role": "assistant", "content": "half a proto"}}]}


def test_truncation_escalates_budget_then_succeeds():
    # A first truncation shouldn't hard-fail: retry the same call with a doubled budget.
    queue = [_trunc(), tool_msg(("request_clarifications", {"usable": True, "gaps": []}, "c1"))]
    agent = make_agent(queue)
    session = agent.analyze("A methods section describing a reaction incubated at 30 C for 4 h.")
    assert (session.phase1 or {}).get("usable") is True          # completed after the retry
    assert agent.client.calls[0]["max_tokens"] == config.MAX_TOKENS      # first try: base budget
    assert agent.client.calls[1]["max_tokens"] == config.MAX_TOKENS_CAP  # retry: doubled (to cap)


def test_truncation_raises_actionable_error_at_cap():
    # If it still truncates at the cap, fail loud with an actionable message.
    agent = make_agent([_trunc(), _trunc()])
    try:
        agent.analyze("A methods section describing a reaction incubated at 30 C for 4 h.")
        assert False, "expected AgentError when truncation persists to the cap"
    except AgentError as e:
        assert "MAX_TOKENS" in str(e)


def test_followup_restores_session_on_failure():
    # A follow-up that fails mid-run must leave the session re-appliable: the acked emit id
    # is re-armed and the appended ack+instruction are dropped, so a retry (Apply fixes,
    # Re-review, Revise) still works instead of dying with "Nothing to build on yet".
    session = Session(
        messages=[
            {"role": "user", "content": "seed"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "e0", "type": "function", "function": {"name": "emit_protocol", "arguments": "{}"}}]},
        ],
        pending_tool_use_id="e0",
    )
    before_len = len(session.messages)
    agent = make_agent([text_msg("I won't call the tool")])  # no tool call -> _run None -> AgentError
    try:
        agent.revise(session, "use 150 uL wells")
        assert False, "expected AgentError"
    except AgentError:
        pass
    assert session.pending_tool_use_id == "e0"       # emit re-armed
    assert len(session.messages) == before_len       # appended ack + instruction dropped
    # ...and a subsequent apply now works on the restored session (proves it wasn't bricked)
    agent2 = make_agent([tool_msg(("emit_protocol", dict(PROTO, title="Fixed"), "e1"))])
    out = agent2.revise(session, "use 150 uL wells")
    assert out["title"] == "Fixed"


def test_grounding_results_compacted_on_followup():
    big = "PMID 1 — Grounding source (Doe 2020) " + "x" * 500
    session = Session(
        messages=[
            {"role": "user", "content": "seed"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "s1", "type": "function", "function": {"name": "search_pubmed", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "s1", "content": big},
        ],
        pending_tool_use_id="e0", grounding_call_ids=["s1"],
    )
    agent = make_agent([tool_msg(("emit_protocol", dict(PROTO, title="P2"), "e1"))])
    agent.revise(session, "use 150 uL wells")
    tmsg = [m for m in session.messages if m.get("role") == "tool" and m.get("tool_call_id") == "s1"][0]
    assert "omitted to save tokens" in tmsg["content"]      # bulky hits compacted
    assert len(tmsg["content"]) < len(big)


# --- per-phase model + forced-emit + parallel grounding ---------------------

def test_fast_tier_used_for_analyze_main_for_emit():
    agent = GapFillerAgent(client=FakeLLM([
        tool_msg(("request_clarifications", {"usable": True, "gaps": []}, "c1"))]),
        model="main-x", model_fast="fast-x")
    agent.effort, agent.effort_fast = "high", "low"
    literature.search_pubmed = lambda q, retmax=5: []
    agent.analyze("A methods section describing a CFPS reaction at 30 C for 4 h.")
    assert agent.client.calls[0]["model"] == "fast-x"  # analyze on the fast tier
    assert agent.client.calls[0]["effort"] == "low"    # and low reasoning effort

    agent2 = GapFillerAgent(client=FakeLLM([tool_msg(("emit_protocol", PROTO, "e1"))]),
                            model="main-x", model_fast="fast-x")
    agent2.effort, agent2.effort_fast = "high", "low"
    sess = Session(messages=[{"role": "user", "content": "seed"}], request_tool_use_id="c1")
    agent2.continue_with_answers(sess, answers=[])
    assert agent2.client.calls[-1]["model"] == "main-x"   # emit on the main tier
    assert agent2.client.calls[-1]["effort"] == "high"    # and full reasoning effort


def test_forces_terminal_when_no_grounding_tools():
    # grounding disabled -> the first emit call is forced (no wasted auto->text->nudge)
    import app.config as cfg
    saved = (cfg.ENABLE_PUBMED, cfg.ENABLE_PREPRINTS)
    cfg.ENABLE_PUBMED = cfg.ENABLE_PREPRINTS = False
    try:
        agent = make_agent([tool_msg(("emit_protocol", PROTO, "e1"))], monkeypatch_search=False)
        sess = Session(messages=[{"role": "user", "content": "seed"}], request_tool_use_id="c1")
        out = agent.continue_with_answers(sess, answers=[])
        assert out["title"] == "P"
        # exactly one model call, and it was forced to the terminal
        assert len(agent.client.calls) == 1
        assert agent.client.calls[0]["tool_choice"] == {"type": "function", "function": {"name": "emit_protocol"}}
    finally:
        cfg.ENABLE_PUBMED, cfg.ENABLE_PREPRINTS = saved


def test_parallel_grounding_answers_all_calls_in_one_turn():
    calls = []
    def fake_search(q, retmax=5):
        calls.append(q)
        return [{"pmid": "1", "title": "t", "authors": "A", "year": 2020, "doi": None}]
    literature.search_pubmed = fake_search
    # one assistant turn emits TWO search calls, then the next turn emits the protocol
    queue = [
        tool_msg(("search_pubmed", {"query": "q1"}, "s1"), ("search_pubmed", {"query": "q2"}, "s2")),
        tool_msg(("emit_protocol", PROTO, "e1")),
    ]
    session = Session(messages=[{"role": "user", "content": "seed"}], request_tool_use_id="c1")
    agent = GapFillerAgent(client=FakeLLM(queue), model="fake")
    out = agent.continue_with_answers(session, answers=[])
    assert out["title"] == "P"
    # both searches ran and both were answered with role:tool messages
    assert set(calls) == {"q1", "q2"}
    answered = {m["tool_call_id"] for m in session.messages if m.get("role") == "tool"}
    assert {"s1", "s2"} <= answered
    assert set(session.grounding_call_ids) == {"s1", "s2"}


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
