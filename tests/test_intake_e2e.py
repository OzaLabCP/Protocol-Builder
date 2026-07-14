"""Browser end-to-end tests for the universal-intake -> workflow -> analyze flow.

These drive the REAL static/index.html in headless Chromium and assert on the
network requests the frontend actually issues — the class of UI data-flow bug
that backend/fake-LLM tests structurally cannot catch (e.g. a PDF uploaded on
the landing page being dropped on the way into the paper panel).

Self-skips (exit 0) when Playwright or a Chromium binary is unavailable, so it
is inert in a browserless CI and only runs where a browser exists. All /api/*
calls are intercepted and fulfilled with canned responses — no backend, no LLM,
no network. Run: `python tests/test_intake_e2e.py`  (needs `pip install playwright`).
"""
from __future__ import annotations

import functools
import glob
import http.server
import json
import os
import socketserver
import sys
import threading

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(REPO, "static")


def _skip(msg: str) -> None:
    print(f"SKIP test_intake_e2e: {msg}")
    sys.exit(0)


try:
    from playwright.sync_api import sync_playwright
except Exception:  # noqa: BLE001
    _skip("playwright not installed (pip install playwright)")


def _chromium_path():
    for pat in (
        "/opt/pw-browsers/chromium-*/chrome-linux/chrome",
        os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux/chrome"),
    ):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]
    return None


CHROME = _chromium_path()
if not CHROME:
    _skip("no Chromium binary found under PLAYWRIGHT_BROWSERS_PATH")


# --- a tiny static server for static/index.html (relative /api/* is intercepted) ---------
class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):  # noqa: D401 - silence access log
        pass


def _serve_static():
    handler = functools.partial(_QuietHandler, directory=STATIC)
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


# --- canned API responses (frontend never reaches a real backend) -----------------------
def _draft(workflow: str, entry_mode: str, filename: str):
    return {
        "id": "proj_e2e", "workflow": workflow, "detected_workflow": workflow,
        "entry_mode": entry_mode, "detection_confidence": 0.9,
        "detection_reason": "canned detection for the e2e test",
        "confirmation_required": True, "lifecycle_status": "awaiting_confirmation",
        "seed_input": {"text": "", "filename": filename},
    }


def _project(workflow: str, entry_mode: str, filename: str, seed_text: str, hypothesis: str):
    return {
        "id": "proj_e2e", "workflow": workflow, "detected_workflow": workflow,
        "entry_mode": entry_mode, "title": "E2E project", "lifecycle_status": "workflow_confirmed",
        "version": 0, "session_id": None, "session_live": False, "hypothesis": hypothesis,
        "scientific_goal": None, "measurement_objective": None, "constraints": {},
        "input_summary": {}, "seed_input": {"text": seed_text, "filename": filename},
        "current_protocol_version_id": None, "validation_summary": None,
        "latest_result": None, "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00", "protocol_versions": [],
    }


def _router(captured, *, workflow, entry_mode, filename, seed_text, hypothesis):
    def _body(req):
        try:
            b = req.post_data or ""
        except Exception:  # noqa: BLE001
            b = ""
        if not b:
            try:
                b = (req.post_data_buffer or b"").decode("latin-1", "ignore")
            except Exception:  # noqa: BLE001
                b = ""
        return b

    def handler(route):
        req = route.request
        path = req.url.split("?", 1)[0]
        j = lambda o: route.fulfill(status=200, content_type="application/json", body=json.dumps(o))
        if path.endswith("/api/analyze"):
            b = _body(req)
            captured["analyze_called"] = True
            captured["analyze_has_file"] = ('name="file"' in b) or bool(filename and filename in b)
            captured["analyze_has_methods"] = 'name="methods_text"' in b
            return j({"phase": "complete", "session_id": "s1",
                      "protocol": {"title": "T", "steps": [], "materials": []}})
        if path.endswith("/api/discover"):
            captured["discover_called"] = True
            return j({"phase": "assays", "session_id": "s1",
                      "assay_options": {"usable": True, "assays": []}})
        if "/api/project/" in path and path.endswith("/workflow"):
            return j(_project(workflow, entry_mode, filename, seed_text, hypothesis))
        if path.endswith("/api/project"):
            return j(_draft(workflow, entry_mode, filename))
        if "/api/projects" in path:
            return j([])
        if "/api/progress/" in path:
            return j({"steps": [], "done": True})
        return j({})

    return handler


PDF_BYTES = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def _page(browser, port, captured, **kw):
    ctx = browser.new_context()
    page = ctx.new_page()
    page.set_default_timeout(15000)
    page.route("**/api/**", _router(captured, **kw))
    page.goto(f"http://127.0.0.1:{port}/")
    return ctx, page


# --- the tests --------------------------------------------------------------------------
def test_landing_pdf_reaches_analyze(browser, port):
    """REGRESSION: a PDF uploaded on the landing page and routed via 'reproduce' must
    arrive in the paper panel AND be attached to the /api/analyze request — the exact
    bug where the file was detected-only and then dropped on the way into the panel."""
    cap = {}
    ctx, page = _page(browser, port, cap, workflow="reproduce", entry_mode="paper",
                      filename="paper.pdf", seed_text="", hypothesis="")
    try:
        page.set_input_files("#u-file", files=[{
            "name": "paper.pdf", "mimeType": "application/pdf", "buffer": PDF_BYTES}])
        page.click("#u-go")
        page.wait_for_selector("#detect-confirm", state="visible")
        with page.expect_request("**/api/analyze"):
            page.click("#detect-confirm")
        # the file must have been carried into the paper panel's #pdf input...
        assert page.eval_on_selector("#pdf", "el => el.files.length") == 1, "PDF not carried into #pdf"
        # ...and the analyze request must actually carry the file (not an empty methods field).
        assert cap.get("analyze_called"), "analyze was never called (auto-run did not fire)"
        assert cap.get("analyze_has_file"), "analyze request did not carry the uploaded PDF"
    finally:
        ctx.close()


def test_landing_text_reaches_analyze_as_methods(browser, port):
    """A typed methods paste routed via 'reproduce' reaches analyze as methods_text
    (guards that the PDF-carry fix did not break the text path)."""
    cap = {}
    seed = "Cells were lysed and the supernatant clarified by centrifugation at 12000 g for 10 min."
    ctx, page = _page(browser, port, cap, workflow="reproduce", entry_mode="paper",
                      filename="", seed_text=seed, hypothesis="")
    try:
        page.fill("#u-input", seed)
        page.click("#u-go")
        page.wait_for_selector("#detect-confirm", state="visible")
        with page.expect_request("**/api/analyze"):
            page.click("#detect-confirm")
        assert page.eval_on_selector("#pdf", "el => el.files.length") == 0, "unexpected file on text path"
        assert cap.get("analyze_has_methods"), "analyze did not carry methods_text on the text path"
        assert not cap.get("analyze_has_file"), "text path unexpectedly attached a file"
    finally:
        ctx.close()


def test_landing_hypothesis_routes_to_discovery(browser, port):
    """A hypothesis-mode workflow routes into the discovery panel with the goal seeded,
    not the paper panel."""
    cap = {}
    goal = "Overexpressing GroEL raises the soluble yield of my membrane protein"
    ctx, page = _page(browser, port, cap, workflow="measure", entry_mode="hypothesis",
                      filename="", seed_text=goal, hypothesis=goal)
    try:
        page.fill("#u-input", goal)
        page.click("#u-go")
        page.wait_for_selector("#detect-confirm", state="visible")
        # auto-run fires /api/discover (hypothesis path) — assert on that + the seeded goal,
        # not on the transient discovery-panel visibility (auto-run advances past it).
        with page.expect_request("**/api/discover"):
            page.click("#detect-confirm")
        assert goal[:20] in page.eval_on_selector("#disc-hyp", "el => el.value"), "goal not seeded into discovery"
        assert not cap.get("analyze_called"), "hypothesis path must not hit the paper analyze route"
    finally:
        ctx.close()


if __name__ == "__main__":
    import traceback

    httpd, port = _serve_static()
    fns = [(k, v) for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        try:
            for name, fn in fns:
                try:
                    fn(browser, port)
                    print(f"PASS {name}")
                except Exception:  # noqa: BLE001
                    failed += 1
                    print(f"FAIL {name}")
                    traceback.print_exc()
        finally:
            browser.close()
    httpd.shutdown()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
