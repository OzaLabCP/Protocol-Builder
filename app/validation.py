"""Host-side validation — the step that turns "the model says it grounded this"
into "the tool verified it." Runs after emit_protocol, before rendering.

Three checks from the spec:
  1. Citation resolution: every literature_grounded citation is resolved against a
     real source; unresolvable/mismatched ones are downgraded to default_verify.
  2. Provenance/citation invariant: literature_grounded with a null citation is a
     fabrication guard -> downgrade; a citation on a non-literature_grounded value
     that the user did not select is stripped.
  3. assumptions_log <-> inline consistency: parameters present in one but not the
     other are surfaced (not silently trusted).

The resolver is injected so this module is unit-testable without the network.
"""

from __future__ import annotations

import re
from typing import Callable, Iterator, Optional

from .resolvers import ResolvedCitation, resolve_citation

Resolver = Callable[[str], Optional[ResolvedCitation]]

_WORD_RE = re.compile(r"[a-z0-9]+")


def _title_overlap(a: str, b: str) -> float:
    ta = set(_WORD_RE.findall((a or "").lower()))
    tb = set(_WORD_RE.findall((b or "").lower()))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _norm_param(name: str) -> str:
    return " ".join(_WORD_RE.findall((name or "").lower()))


def _iter_citation_entries(protocol: dict) -> Iterator[tuple[dict, str]]:
    """Yield (entry, human-readable location) for every value that carries a
    provenance tier and may carry a citation."""
    for i, mat in enumerate(protocol.get("materials") or []):
        yield mat, f"materials[{i}] '{mat.get('name', '?')}'"
    for step in protocol.get("steps") or []:
        snum = step.get("number", "?")
        for j, cp in enumerate(step.get("critical_parameters") or []):
            yield cp, f"step {snum} critical_parameters[{j}] '{cp.get('name', '?')}'"
    for k, a in enumerate(protocol.get("assumptions_log") or []):
        yield a, f"assumptions_log[{k}] '{a.get('parameter', '?')}'"


def _downgrade(entry: dict, note: str) -> None:
    entry["provenance"] = "default_verify"
    entry["citation"] = None
    entry["citation_verified"] = False
    if "verify" in entry or "basis" in entry:  # assumptions_log shape
        entry["verify"] = True
    existing = entry.get("provenance_note") or ""
    entry["provenance_note"] = (existing + " " if existing else "") + f"[VALIDATION] {note}"


def validate_and_finalize(
    protocol: dict, resolver: Resolver = resolve_citation
) -> dict:
    """Validate and repair the protocol in place. Returns a report dict; the
    protocol argument is mutated (citations verified/nulled, tiers downgraded,
    open_questions appended)."""
    report = {
        "citations_checked": 0,
        "verified": [],
        "downgraded": [],
        "invariant_fixes": [],
        "consistency": {"inline_missing_from_log": [], "log_missing_from_inline": []},
    }
    open_questions = list(protocol.get("open_questions") or [])

    for entry, location in _iter_citation_entries(protocol):
        prov = entry.get("provenance")
        citation = entry.get("citation")
        selected = bool(entry.get("selected_by_user"))

        # --- Invariant: literature_grounded MUST carry a citation. ---
        if prov == "literature_grounded" and not citation:
            _downgrade(entry, "claimed literature_grounded but carried no citation.")
            report["invariant_fixes"].append(
                {"location": location, "fix": "downgraded (no citation)"}
            )
            open_questions.append(
                f"{location}: claimed literature-grounded with no citation; "
                f"downgraded to default_verify."
            )
            continue

        # --- Invariant: a citation on a non-literature_grounded value the user
        # did not select is illegitimate -> strip it. ---
        if prov != "literature_grounded" and citation and not selected:
            entry["citation"] = None
            report["invariant_fixes"].append(
                {"location": location, "fix": "stripped stray citation"}
            )
            continue

        if not citation:
            continue

        # --- Resolution: verify the identifier against a real source. ---
        identifier = str(citation.get("identifier", "")).strip()
        report["citations_checked"] += 1
        try:
            resolved = resolver(identifier)
        except Exception as exc:  # network failure -> treat as unresolved
            resolved = None
            resolve_error = str(exc)
        else:
            resolve_error = None

        if resolved is None:
            reason = (
                "citation identifier did not resolve"
                + (f" ({resolve_error})" if resolve_error else "")
            )
            _downgrade(entry, reason + "; downgraded to default_verify.")
            report["downgraded"].append(
                {"location": location, "identifier": identifier, "reason": reason}
            )
            open_questions.append(
                f"{location}: citation '{identifier}' could not be verified "
                f"({reason}); value downgraded to default_verify."
            )
            continue

        # Resolved — confirm year and title are consistent.
        claimed_year = citation.get("year")
        year_mismatch = (
            claimed_year is not None
            and resolved.year is not None
            and int(claimed_year) != int(resolved.year)
        )
        overlap = _title_overlap(citation.get("title", ""), resolved.title)
        title_mismatch = overlap < 0.34

        if year_mismatch or title_mismatch:
            bits = []
            if year_mismatch:
                bits.append(f"year {claimed_year} vs source {resolved.year}")
            if title_mismatch:
                bits.append(f"title overlap {overlap:.0%} with source '{resolved.title}'")
            reason = "citation metadata mismatch (" + "; ".join(bits) + ")"
            _downgrade(entry, reason + "; downgraded to default_verify.")
            report["downgraded"].append(
                {"location": location, "identifier": identifier, "reason": reason}
            )
            open_questions.append(
                f"{location}: citation '{identifier}' resolved but metadata "
                f"mismatched ({'; '.join(bits)}); downgraded to default_verify."
            )
            continue

        # Verified.
        entry["citation_verified"] = True
        entry["citation"]["url"] = entry["citation"].get("url") or _canonical_url(resolved)
        report["verified"].append(
            {
                "location": location,
                "identifier": identifier,
                "title": resolved.title,
                "year": resolved.year,
                "source": resolved.source,
            }
        )

    _consistency_check(protocol, report, open_questions)
    protocol["open_questions"] = open_questions
    protocol["validation_report"] = report
    return report


def _canonical_url(resolved: ResolvedCitation) -> Optional[str]:
    if resolved.kind == "pmid":
        return f"https://pubmed.ncbi.nlm.nih.gov/{resolved.identifier}/"
    if resolved.kind == "doi":
        return f"https://doi.org/{resolved.identifier}"
    return None


def _consistency_check(protocol: dict, report: dict, open_questions: list) -> None:
    """Surface parameters that appear inline but not in the assumptions_log (or
    vice versa). Informational — we flag, we don't silently trust either copy."""
    inline_names: set[str] = set()
    for mat in protocol.get("materials") or []:
        if mat.get("provenance") not in (None, "stated"):
            inline_names.add(_norm_param(mat.get("name", "")))
    for step in protocol.get("steps") or []:
        for cp in step.get("critical_parameters") or []:
            if cp.get("provenance") not in (None, "stated"):
                inline_names.add(_norm_param(cp.get("name", "")))

    log_names = {
        _norm_param(a.get("parameter", "")) for a in protocol.get("assumptions_log") or []
    }

    inline_names.discard("")
    log_names.discard("")

    inline_missing = sorted(inline_names - log_names)
    log_missing = sorted(log_names - inline_names)
    report["consistency"]["inline_missing_from_log"] = inline_missing
    report["consistency"]["log_missing_from_inline"] = log_missing

    for name in inline_missing:
        open_questions.append(
            f"Consistency: '{name}' is filled inline but missing from the "
            f"assumptions_log (the log must be exhaustive)."
        )
    # log-only entries are allowed (step-level assumptions) but still worth noting
    for name in log_missing:
        open_questions.append(
            f"Consistency: assumptions_log lists '{name}' with no matching inline "
            f"material or critical parameter — confirm it is applied somewhere."
        )
