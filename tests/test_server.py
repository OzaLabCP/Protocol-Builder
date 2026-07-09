"""HTTP-surface tests via FastAPI TestClient. These exercise everything that
does NOT require the Anthropic API (health, validation, error paths, routing)."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.server import app  # noqa: E402

client = TestClient(app)


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "grounding" in body and "pubmed" in body["grounding"]


def test_index_served():
    r = client.get("/")
    assert r.status_code == 200
    assert "Methods Gap-Filler" in r.text


def test_analyze_empty_is_400():
    r = client.post("/api/analyze", data={"methods_text": ""})
    assert r.status_code == 400


def test_analyze_non_pdf_is_400():
    r = client.post("/api/analyze", files={"file": ("x.pdf", b"not a pdf", "application/pdf")})
    assert r.status_code == 400
    assert "PDF" in r.json()["detail"]


def test_unknown_session_markdown_is_404():
    r = client.get("/api/protocol/deadbeef.md")
    assert r.status_code == 404


def test_revise_unknown_session_is_404():
    r = client.post("/api/revise", json={"session_id": "nope", "instruction": "change it"})
    assert r.status_code == 404


def test_revise_empty_instruction_is_400():
    r = client.post("/api/revise", json={"session_id": "nope", "instruction": ""})
    assert r.status_code == 400


def test_design_unknown_session_is_404():
    r = client.post("/api/design", json={"session_id": "nope"})
    assert r.status_code == 404


def test_align_unknown_session_is_404():
    r = client.post("/api/align", json={"session_id": "nope"})
    assert r.status_code == 404


def test_discover_short_is_400():
    r = client.post("/api/discover", json={"hypothesis": "too short"})
    assert r.status_code == 400


def test_discover_accepts_constraints_shape():
    # constraints + auto_pick are accepted (a 400 on the short hypothesis, not a 422)
    r = client.post("/api/discover", json={"hypothesis": "x", "constraints": {"equipment": "plate reader"}, "auto_pick": True})
    assert r.status_code == 400


def test_choose_assay_unknown_session_is_404():
    r = client.post("/api/choose_assay", json={"session_id": "nope", "assay_id": "x"})
    assert r.status_code == 404


def test_choose_assay_wrong_flow_is_409():
    # a session that never went through discovery has no assay_options -> 409
    import time as _t

    from app.agent import Session
    from app.server import Store, _SESSIONS
    sid = "flowtest"
    _SESSIONS[sid] = Store(session=Session(), created=_t.time())
    try:
        r = client.post("/api/choose_assay", json={"session_id": sid, "assay_id": "x"})
        assert r.status_code == 409
    finally:
        _SESSIONS.pop(sid, None)


def test_choose_assay_bad_id_is_400():
    # endpoint validates assay_id against stored options BEFORE any model call
    import time as _t

    from app.agent import Session
    from app.server import Store, _SESSIONS
    sid = "idtest"
    sess = Session(source_kind="hypothesis", assay_options={"assays": [{"id": "x"}]})
    _SESSIONS[sid] = Store(session=sess, created=_t.time())
    try:
        r = client.post("/api/choose_assay", json={"session_id": sid, "assay_id": "bogus"})
        assert r.status_code == 400
    finally:
        _SESSIONS.pop(sid, None)


def test_analyze_accepts_hypothesis_field():
    # hypothesis is optional; with empty text the endpoint still 400s on the text,
    # proving the field is accepted (not a 422 unprocessable-entity from an unknown form field).
    r = client.post("/api/analyze", data={"methods_text": "", "hypothesis": "X increases Y"})
    assert r.status_code == 400


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
