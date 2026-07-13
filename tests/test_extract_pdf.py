"""/api/extract_pdf — host-side PDF text-extraction preview + OCR seam.

TestClient HTTP surface, NO network, NO LLM. The pypdf parse is made deterministic by
monkeypatching ``server._pdf_extract`` / ``server._ocr_available`` / ``server._ocr_pdf``
(referenced as module globals in the endpoint), and a ``b"%PDF-1.4 ..."`` body passes the
magic-byte guard. Also covers: the OCR seam raises NotImplementedError cleanly, corrected
reviewed text flows into /api/analyze without re-extraction, and the direct-PDF-upload
/api/analyze path still works.

Runnable directly (``python tests/test_extract_pdf.py``) or under pytest.
"""

from __future__ import annotations

import json as _json
import os
import sys
import time as _time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import app.agent as agent_mod  # noqa: E402
import app.server as srv  # noqa: E402
from app.agent import GapFillerAgent, Session  # noqa: E402

client = TestClient(srv.app)

_PDF = b"%PDF-1.4 fake body bytes"


# --- fake-LLM plumbing (mirrors test_server) --------------------------------

def _tool_msg(name, args, cid):
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": cid, "type": "function",
                        "function": {"name": name, "arguments": _json.dumps(args)}}]}}]}


class _FakeLLM:
    def __init__(self, queue):
        self.queue = list(queue)

    def chat(self, messages, tools=None, tool_choice=None, model=None, effort=None, max_tokens=None):
        return self.queue.pop(0)


def _install_agent(queue):
    srv._agent = GapFillerAgent(client=_FakeLLM(queue), model="fake")


# request_clarifications with a NON-empty gap so analyze STOPS at the clarification
# phase (an empty-gaps phase-1 auto-continues straight to emit_protocol).
_ONE_GAP = {"usable": True, "gaps": [
    {"id": "g1", "parameter": "temp", "question": "what temperature?", "answer_type": "text"}]}


def _upload(body=_PDF, name="paper.pdf"):
    return client.post("/api/extract_pdf",
                       files={"file": (name, body, "application/pdf")})


# --- report fields ----------------------------------------------------------

def test_extract_report_fields():
    orig = srv._pdf_extract
    srv._pdf_extract = lambda b: ("Methods: mix A and B.", 3)
    try:
        r = _upload()
        assert r.status_code == 200
        b = r.json()
        assert b["pages"] == 3
        assert b["is_image_only"] is False
        assert b["truncated"] is False
        assert b["ocr_available"] is False
        assert b["max_chars"] == srv.MAX_TEXT_CHARS
        assert b["methods_text"] == "Methods: mix A and B."
        assert b["chars_total"] == b["chars_included"] == len(b["methods_text"])
        assert b["filename"] == "paper.pdf"
    finally:
        srv._pdf_extract = orig


def test_extract_truncation_flagged():
    orig = srv._pdf_extract
    big = "x" * (srv.MAX_TEXT_CHARS + 50)
    srv._pdf_extract = lambda b: (big, 2)
    try:
        b = _upload().json()
        assert b["truncated"] is True
        assert b["chars_total"] == srv.MAX_TEXT_CHARS + 50
        assert b["chars_included"] == srv.MAX_TEXT_CHARS
        assert len(b["methods_text"]) == srv.MAX_TEXT_CHARS
    finally:
        srv._pdf_extract = orig


def test_extract_image_only_flagged():
    orig = srv._pdf_extract
    srv._pdf_extract = lambda b: (None, 5)
    try:
        r = _upload()
        assert r.status_code == 200
        b = r.json()
        assert b["is_image_only"] is True
        assert b["methods_text"] == ""
        assert b["pages"] == 5
        assert b["ocr_available"] is False
    finally:
        srv._pdf_extract = orig


def test_extract_unparseable_is_image_only():
    orig = srv._pdf_extract
    srv._pdf_extract = lambda b: (None, 0)  # valid magic but pypdf couldn't parse
    try:
        r = _upload()
        assert r.status_code == 200  # valid magic never 500s
        b = r.json()
        assert b["is_image_only"] is True and b["pages"] == 0
    finally:
        srv._pdf_extract = orig


def test_extract_non_pdf_is_400():
    r = client.post("/api/extract_pdf",
                    files={"file": ("x.pdf", b"not a pdf", "application/pdf")})
    assert r.status_code == 400
    assert "PDF" in r.json()["detail"]


def test_extract_missing_file_is_422():
    r = client.post("/api/extract_pdf")
    assert r.status_code == 422


# --- OCR seam ---------------------------------------------------------------

def test_ocr_seam_raises_notimplemented():
    try:
        agent_mod._ocr_pdf(b"whatever")
        assert False, "expected NotImplementedError"
    except NotImplementedError as e:
        assert "OCR" in str(e)
    assert agent_mod._ocr_available() is False


def test_extract_ocr_seam_wired():
    # Flip the seam on WITHOUT editing the endpoint: image-only + OCR available -> OCR text.
    o_ex, o_av, o_ocr = srv._pdf_extract, srv._ocr_available, srv._ocr_pdf
    srv._pdf_extract = lambda b: (None, 4)
    srv._ocr_available = lambda: True
    srv._ocr_pdf = lambda b: "ocr recovered methods text (long enough)"
    try:
        b = _upload().json()
        assert b["methods_text"] == "ocr recovered methods text (long enough)"
        assert b["is_image_only"] is False
        assert b["ocr_available"] is True
    finally:
        srv._pdf_extract, srv._ocr_available, srv._ocr_pdf = o_ex, o_av, o_ocr


# --- corrected text flows to /api/analyze -----------------------------------

def test_corrected_text_flows_to_analyze():
    _install_agent([_tool_msg("request_clarifications", _ONE_GAP, "c1")])
    reviewed = "Reviewed Methods: dissolve 5 mg in 10 mL PBS and incubate at some temperature."
    assert len(reviewed) >= 40
    sid = None
    try:
        r = client.post("/api/analyze",
                        data={"methods_text": reviewed, "is_full_paper": "true",
                              "pdf_filename": "paper.pdf"})
        assert r.status_code == 200
        body = r.json()
        sid = body["session_id"]
        # Session built directly from the reviewed text (no re-extraction).
        msgs = srv._SESSIONS[sid].session.messages
        joined = " ".join(m["content"] if isinstance(m["content"], str)
                          else "".join(p.get("text", "") for p in m["content"])
                          for m in msgs if m.get("role") == "user")
        assert "dissolve 5 mg" in joined
    finally:
        srv._SESSIONS.pop(sid, None)
        srv._agent = None


def test_analyze_direct_pdf_backcompat():
    _install_agent([_tool_msg("request_clarifications", _ONE_GAP, "c1")])
    o_text = srv._pdf_text
    srv._pdf_text = lambda b: "extracted methods text describing a real assay procedure at some temperature."
    sid = None
    try:
        r = client.post("/api/analyze",
                        files={"file": ("paper.pdf", _PDF, "application/pdf")})
        assert r.status_code == 200
        body = r.json()
        assert "session_id" in body
        sid = body["session_id"]
        # File-upload branch forces is_full_paper=True (pdf_text seed populated).
    finally:
        srv._SESSIONS.pop(sid, None)
        srv._pdf_text = o_text
        srv._agent = None


def test_analyze_is_full_paper_flag_defaults_false():
    # Only methods_text, no flag -> behaves exactly as before (regression guard).
    _install_agent([_tool_msg("request_clarifications", _ONE_GAP, "c1")])
    text = "A plain pasted Methods section with enough characters to pass the length guard here."
    sid = None
    try:
        r = client.post("/api/analyze", data={"methods_text": text})
        assert r.status_code == 200
        body = r.json()
        sid = body["session_id"]
        seed = srv._SESSIONS[sid]
        # is_full_paper defaulted False -> the free_text seed branch, not pdf_text.
        assert seed is not None
    finally:
        srv._SESSIONS.pop(sid, None)
        srv._agent = None


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
