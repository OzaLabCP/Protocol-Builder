"""EPIC-2 §I — the versioned Pydantic model tree, ``schema_version``, the pure
emit-boundary gate (``coerce_emit_payload``), and the in-place structural repair
pre-pass (``repair_structure``).

Pure and offline: no network, no resolver, no model. The model layer is a
validate/repair gate only — these tests exercise it directly on hand-built dicts
and on the shipped example fixture. Runnable as a script (``python
tests/test_models.py``) or under pytest."""

from __future__ import annotations

import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pydantic import ValidationError  # noqa: E402

from app.models import (  # noqa: E402
    MATERIAL_NAME_PLACEHOLDER,
    PARAM_NAME_PLACEHOLDER,
    PARAM_VALUE_PLACEHOLDER,
    PROTOCOL_SCHEMA_VERSION,
    STEP_INSTR_PLACEHOLDER,
    EmittedProtocol,
    Material,
    Step,
    coerce_emit_payload,
    repair_structure,
)
from app.schemas import EMIT_PROTOCOL_TOOL  # noqa: E402
from app.validation import validate_and_finalize  # noqa: E402

FIXTURE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples", "binding_assay_eason2020.json",
)


def _no_network(_identifier):
    raise AssertionError("model/repair tests must not hit the network")


# --- schema_version --------------------------------------------------------

def test_schema_version_is_a_namespaced_string():
    # PROTOCOL_SCHEMA_VERSION is the STRING emitted-protocol version — never the
    # int projects.CURRENT_SCHEMA_VERSION.
    from app.projects import CURRENT_SCHEMA_VERSION
    assert PROTOCOL_SCHEMA_VERSION == "emit_protocol/1"
    assert isinstance(PROTOCOL_SCHEMA_VERSION, str)
    assert isinstance(CURRENT_SCHEMA_VERSION, int)
    assert PROTOCOL_SCHEMA_VERSION != CURRENT_SCHEMA_VERSION
    # re-exported from schemas.py for callers that import there
    from app.schemas import PROTOCOL_SCHEMA_VERSION as reexport
    assert reexport == PROTOCOL_SCHEMA_VERSION


def test_protocol_carries_schema_version_after_finalize():
    p = {
        "title": "T", "summary": "s", "estimated_duration": "1 h",
        "materials": [], "assumptions_log": [], "open_questions": [],
        "steps": [{"number": 1, "title": "Mix", "instruction": "mix",
                   "provenance": "stated", "critical_parameters": []}],
    }
    validate_and_finalize(p, _no_network)
    assert p["schema_version"] == PROTOCOL_SCHEMA_VERSION  # host-authoritative stamp


def test_schema_version_is_host_authoritative_overwrite():
    # A model-supplied schema_version is overwritten, not trusted.
    p = {
        "title": "T", "summary": "s", "estimated_duration": "1 h",
        "materials": [], "assumptions_log": [], "open_questions": [],
        "schema_version": "forged/999",
        "steps": [{"number": 1, "title": "Mix", "instruction": "mix",
                   "provenance": "stated", "critical_parameters": []}],
    }
    validate_and_finalize(p, _no_network)
    assert p["schema_version"] == PROTOCOL_SCHEMA_VERSION


def test_emit_tool_schema_version_optional_not_required():
    # schema_version is documented on the tool but MUST NOT be in `required`
    # (so no existing fixture breaks). The frozen required list is unchanged.
    props = EMIT_PROTOCOL_TOOL["input_schema"]["properties"]
    assert "schema_version" in props
    assert "schema_version" not in EMIT_PROTOCOL_TOOL["input_schema"]["required"]


# --- model tree: accept the valid, reject the truly-incomplete -------------

def test_models_accept_the_shipped_fixture():
    proto = json.load(open(FIXTURE))
    model = EmittedProtocol.model_validate(proto)   # must not raise
    # extra="allow": host keys survive a round-trip (no re-typing/reordering).
    dumped = model.model_dump()
    assert dumped["title"] == proto["title"]
    assert len(dumped["steps"]) == len(proto["steps"])


def test_models_accept_a_minimal_valid_protocol():
    EmittedProtocol.model_validate(
        {"title": "T", "steps": [{"instruction": "do the thing"}]})


def test_models_reject_empty_instruction():
    try:
        EmittedProtocol.model_validate({"title": "T", "steps": [{"instruction": "   "}]})
        assert False, "expected ValidationError for whitespace instruction"
    except ValidationError as e:
        assert any(err["loc"][-1] == "instruction" for err in e.errors())


def test_models_reject_empty_material_name():
    try:
        Material.model_validate({"name": ""})
        assert False, "expected ValidationError for empty material name"
    except ValidationError as e:
        assert any(err["loc"][-1] == "name" for err in e.errors())


def test_models_reject_zero_steps():
    try:
        EmittedProtocol.model_validate({"title": "T", "steps": []})
        assert False, "expected ValidationError for zero steps"
    except ValidationError:
        pass


def test_models_are_lenient_on_numeric_and_extra_keys():
    # numeric fields accept str|number with no write-back; host attestation keys survive.
    s = Step.model_validate({"instruction": "add", "number": "1.1",
                             "_id": "step:0", "quote_verified": True,
                             "step_id": "s_deadbeef"})
    d = s.model_dump()
    assert d["number"] == "1.1"          # not coerced to a number
    assert d["_id"] == "step:0"          # extra host key preserved
    assert d["quote_verified"] is True
    assert d["step_id"] == "s_deadbeef"


# --- coerce_emit_payload: FATAL class only, pure -------------------------

def test_coerce_flags_only_the_fatal_class():
    # not a dict / empty title / steps-not-a-list are the ONLY fatal payloads.
    assert coerce_emit_payload("not a dict")[0] is False
    assert coerce_emit_payload({"title": "", "steps": [{"instruction": "x"}]})[0] is False
    assert coerce_emit_payload({"steps": [{"instruction": "x"}]})[0] is False
    assert coerce_emit_payload({"title": "T", "steps": "nope"})[0] is False
    assert coerce_emit_payload({"title": "T"})[0] is False  # steps missing -> not a list


def test_coerce_empty_steps_is_usable_not_fatal():
    # Deliberate scoping: an empty steps list is a usable, iterable structure — the
    # per-entry repair + STRUCT_* blocker handle the gaps, so it is NOT a re-emit trigger.
    usable, fatal, _ = coerce_emit_payload({"title": "T", "steps": []})
    assert usable is True and fatal == []


def test_coerce_lists_repairable_gaps_without_being_fatal():
    raw = {"title": "T", "steps": [{"instruction": "", "critical_parameters": [
        {"name": "", "value": ""}]}], "materials": [{"name": ""}]}
    usable, fatal, repairable = coerce_emit_payload(raw)
    assert usable is True and fatal == []
    assert "steps[0].instruction" in repairable
    assert "materials[0].name" in repairable


def test_coerce_does_not_mutate():
    raw = {"title": "T", "steps": [{"instruction": ""}]}
    snapshot = copy.deepcopy(raw)
    coerce_emit_payload(raw)
    assert raw == snapshot


# --- repair_structure: repair, never reject; idempotent -------------------

def test_repair_fills_sentinels_and_blocks():
    protocol = {
        "materials": [{"name": ""}],
        "steps": [{"instruction": "", "critical_parameters": [{"name": "", "value": ""}]}],
    }
    oq: list = []
    repair_structure(protocol, oq)
    assert protocol["materials"][0]["name"] == MATERIAL_NAME_PLACEHOLDER
    assert protocol["steps"][0]["instruction"] == STEP_INSTR_PLACEHOLDER
    cp = protocol["steps"][0]["critical_parameters"][0]
    assert cp["name"] == PARAM_NAME_PLACEHOLDER
    assert cp["value"] == PARAM_VALUE_PLACEHOLDER
    # every gap raised a loud [BLOCKED] open_question (never silent).
    assert oq and all(q.startswith("[BLOCKED] ") for q in oq)
    assert len(oq) == 4


def test_repair_is_idempotent():
    protocol = {"steps": [{"instruction": ""}]}
    oq: list = []
    repair_structure(protocol, oq)
    snapshot = copy.deepcopy(protocol)
    n = len(oq)
    repair_structure(protocol, oq)          # second run adds nothing
    assert protocol == snapshot
    assert len(oq) == n


def test_repair_does_not_touch_good_entries():
    protocol = {"materials": [{"name": "Tris"}],
                "steps": [{"instruction": "mix well"}]}
    oq: list = []
    repair_structure(protocol, oq)
    assert protocol["materials"][0]["name"] == "Tris"
    assert protocol["steps"][0]["instruction"] == "mix well"
    assert oq == []


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
