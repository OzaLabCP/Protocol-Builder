"""PubMed literature search for Phase-1 scoping and Phase-2 grounding.

The agent calls `search_pubmed` as a client-side tool; the app runs the NCBI
E-utilities query itself (esearch -> esummary) and hands back real PMIDs, titles,
authors, years, and DOIs. The model then cites a returned PMID/DOI for a
literature_grounded value, and the host later resolves it (resolvers.py) to verify.

This mirrors what the PubMed connector returns, but over the app's own HTTP so it
works without MCP at runtime. Network calls honour HTTPS_PROXY.
"""

from __future__ import annotations

import re
from typing import Optional

import httpx

from . import config

_EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_TIMEOUT = httpx.Timeout(15.0)
_UA = "MethodsGapFiller/0.1 (mailto:noreply@example.com)"

# Max length of an evidence excerpt surfaced to / copyable by the model.
EVIDENCE_EXCERPT_CAP = 600


def _clip(text: str) -> str:
    """Collapse whitespace and truncate to EVIDENCE_EXCERPT_CAP, appending an
    ellipsis if the source was longer. Never fabricates text."""
    s = " ".join((text or "").split())
    if len(s) > EVIDENCE_EXCERPT_CAP:
        return s[:EVIDENCE_EXCERPT_CAP] + "…"
    return s


def _params(extra: dict) -> dict:
    p = {"db": "pubmed", "retmode": "json", **extra}
    if config.NCBI_API_KEY:
        p["api_key"] = config.NCBI_API_KEY
    return p


def _year(pubdate: str) -> Optional[int]:
    m = re.search(r"\b(\d{4})\b", pubdate or "")
    return int(m.group(1)) if m else None


def _authors(record: dict) -> str:
    names = [a.get("name", "") for a in record.get("authors", []) if a.get("name")]
    if not names:
        return ""
    return names[0] + (" et al." if len(names) > 1 else "")


def _doi(record: dict) -> Optional[str]:
    for aid in record.get("articleids", []):
        if aid.get("idtype") == "doi" and aid.get("value"):
            return aid["value"]
    return None


def _pubmed_abstracts(client: httpx.Client, idlist: list[str]) -> dict:
    """Fetch abstracts for the given PMIDs via efetch and return {pmid: excerpt}.

    Wrapped so any failure (network, non-XML body, parse error) degrades cleanly
    to {} — the caller then emits metadata_only rather than fabricating text."""
    try:
        import xml.etree.ElementTree as ET

        resp = client.get(
            f"{_EUTILS}/efetch.fcgi",
            params=_params({"id": ",".join(idlist), "rettype": "abstract", "retmode": "xml"}),
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        out: dict = {}
        for art in root.iter("PubmedArticle"):
            pmid_el = art.find(".//MedlineCitation/PMID")
            if pmid_el is None or not (pmid_el.text or "").strip():
                continue
            pmid = pmid_el.text.strip()
            texts = [
                "".join(node.itertext())
                for node in art.iter("AbstractText")
            ]
            joined = " ".join(t for t in texts if t and t.strip())
            if joined.strip():
                out[pmid] = _clip(joined)
        return out
    except Exception:
        return {}


def search_pubmed(
    query: str, retmax: int = 5, client: Optional[httpx.Client] = None
) -> list[dict]:
    """Return up to `retmax` PubMed hits as dicts:
    {pmid, title, authors, year, doi, evidence}. Raises on network error."""
    retmax = max(1, min(int(retmax or 5), config.PUBMED_RETMAX_CAP))
    own = client is None
    if own:
        client = httpx.Client(timeout=_TIMEOUT, headers={"User-Agent": _UA}, follow_redirects=True)
    try:
        es = client.get(
            f"{_EUTILS}/esearch.fcgi",
            params=_params({"term": query, "retmax": retmax, "sort": "relevance"}),
        )
        es.raise_for_status()
        idlist = es.json().get("esearchresult", {}).get("idlist", [])
        if not idlist:
            return []
        su = client.get(
            f"{_EUTILS}/esummary.fcgi", params=_params({"id": ",".join(idlist)})
        )
        su.raise_for_status()
        result = su.json().get("result", {})
        abstracts = _pubmed_abstracts(client, idlist)
        out = []
        for pmid in idlist:
            rec = result.get(pmid)
            if not rec or not rec.get("title"):
                continue
            abstract = abstracts.get(pmid, "")
            evidence = {
                "excerpt": abstract,
                "section": "Abstract" if abstract else None,
                "evidence_type": "abstract" if abstract else "metadata_only",
                "source_type": "peer_reviewed",
            }
            out.append(
                {
                    "pmid": pmid,
                    "title": rec.get("title", "").strip().rstrip("."),
                    "authors": _authors(rec),
                    "year": _year(rec.get("pubdate") or rec.get("epubdate") or ""),
                    "doi": _doi(rec),
                    "evidence": evidence,
                }
            )
        return out
    finally:
        if own:
            client.close()


_EVIDENCE_INSTR = (
    "Attach the evidence line as citation.evidence.excerpt on any value you ground "
    "from this result; a citation with no relevant evidence is metadata_only — do not "
    "tag it supported."
)


def _evidence_lines(r: dict) -> list[str]:
    """Render a hit's evidence as extra indented model-facing line(s)."""
    ev = r.get("evidence") or {}
    excerpt = ev.get("excerpt") or ""
    if ev.get("evidence_type") == "metadata_only" or not excerpt:
        return ["  (metadata only — no abstract retrieved; excerpt must stay empty)"]
    return [f'  evidence ({ev.get("evidence_type")}): "{excerpt}"']


def format_results(results: list[dict]) -> str:
    """Compact, model-facing rendering of the PubMed hits."""
    if not results:
        return "No PubMed results. Fill this value from best_practice or default_verify instead."
    lines = [
        "PubMed results (cite a PMID or DOI for literature_grounded values):",
        _EVIDENCE_INSTR,
    ]
    for r in results:
        doi = f" doi:{r['doi']}" if r.get("doi") else ""
        lines.append(
            f"- PMID {r['pmid']}{doi} | {r.get('authors','')} ({r.get('year','')}) — {r['title']}"
        )
        lines.extend(_evidence_lines(r))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Europe PMC — preprints (bioRxiv / medRxiv / Research Square, not yet in PubMed)
# ---------------------------------------------------------------------------

_EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


def _first_author_etal(author_string: str) -> str:
    if not author_string:
        return ""
    first = author_string.split(",")[0].strip().rstrip(".")
    return first + (" et al." if "," in author_string else "")


def search_preprints(
    query: str, retmax: int = 5, client: Optional[httpx.Client] = None
) -> list[dict]:
    """Search Europe PMC restricted to preprint sources (bioRxiv/medRxiv/etc.).
    Returns {source, title, authors, year, doi, pmid, url}. Raises on network error."""
    retmax = max(1, min(int(retmax or 5), config.PUBMED_RETMAX_CAP))
    own = client is None
    if own:
        client = httpx.Client(timeout=_TIMEOUT, headers={"User-Agent": _UA}, follow_redirects=True)
    try:
        resp = client.get(
            _EPMC,
            params={
                "query": f"({query}) AND (SRC:PPR)",
                "format": "json",
                "resultType": "core",
                "pageSize": retmax,
            },
        )
        resp.raise_for_status()
        hits = resp.json().get("resultList", {}).get("result", [])
        out = []
        for h in hits:
            if not h.get("title"):
                continue
            doi = h.get("doi")
            year = int(h["pubYear"]) if str(h.get("pubYear", "")).isdigit() else None
            abs_ = _clip(h.get("abstractText") or "")
            evidence = {
                "excerpt": abs_,
                "section": "Abstract" if abs_ else None,
                "evidence_type": "abstract" if abs_ else "metadata_only",
                "source_type": "preprint",
            }
            out.append(
                {
                    "source": h.get("source", "PPR"),
                    "title": h["title"].strip().rstrip("."),
                    "authors": _first_author_etal(h.get("authorString", "")),
                    "year": year,
                    "doi": doi,
                    "pmid": h.get("pmid"),
                    "url": f"https://doi.org/{doi}" if doi else None,
                    "evidence": evidence,
                }
            )
        return out
    finally:
        if own:
            client.close()


def format_preprints(results: list[dict]) -> str:
    if not results:
        return "No preprint results. Fill from best_practice or default_verify instead."
    lines = [
        "Preprint results — bioRxiv/medRxiv via Europe PMC (cite the DOI):",
        _EVIDENCE_INSTR,
    ]
    for r in results:
        ident = f"doi:{r['doi']}" if r.get("doi") else (f"PMID {r['pmid']}" if r.get("pmid") else "no id")
        lines.append(f"- {ident} | {r.get('authors','')} ({r.get('year','')}) — {r['title']}")
        lines.extend(_evidence_lines(r))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# protocols.io — published step-by-step protocols (grounds a METHOD, not a value)
# ---------------------------------------------------------------------------

_PIO = "https://www.protocols.io/api/v3/protocols"


def search_protocols(
    query: str, retmax: int = 5, client: Optional[httpx.Client] = None
) -> list[dict]:
    """Search public protocols.io protocols. Requires PROTOCOLS_IO_TOKEN.
    Returns {title, authors, year, doi, url}. Raises on network/auth error."""
    if not config.PROTOCOLS_IO_TOKEN:
        raise RuntimeError("protocols.io token not configured (set PROTOCOLS_IO_TOKEN).")
    import time

    retmax = max(1, min(int(retmax or 5), config.PUBMED_RETMAX_CAP))
    own = client is None
    if own:
        client = httpx.Client(
            timeout=_TIMEOUT,
            headers={
                "User-Agent": _UA,
                "Authorization": f"Bearer {config.PROTOCOLS_IO_TOKEN}",
            },
            follow_redirects=True,
        )
    try:
        resp = client.get(
            _PIO,
            params={
                "filter": "public",
                "key": query,
                "order_field": "relevance",
                "page_size": retmax,
            },
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
        out = []
        for it in items:
            if not it.get("title"):
                continue
            ts = it.get("published_on") or it.get("created_on")
            year = time.gmtime(ts).tm_year if isinstance(ts, (int, float)) and ts else None
            authors = it.get("authors") or []
            names = [a.get("name", "") for a in authors if a.get("name")]
            uri = it.get("uri") or ""
            desc = _clip(it.get("description") or "")
            evidence = {
                "excerpt": desc,
                "section": "Description" if desc else None,
                "evidence_type": "protocol" if desc else "metadata_only",
                "source_type": "protocol",
            }
            out.append(
                {
                    "title": it["title"].strip(),
                    "authors": (names[0] + (" et al." if len(names) > 1 else "")) if names else "",
                    "year": year,
                    "doi": it.get("doi"),
                    "url": it.get("url") or (f"https://www.protocols.io/view/{uri}" if uri else None),
                    "evidence": evidence,
                }
            )
        return out
    finally:
        if own:
            client.close()


def format_protocols(results: list[dict]) -> str:
    if not results:
        return "No protocols.io results. Fill from best_practice or default_verify instead."
    lines = [
        "protocols.io results — published protocols (cite the protocol DOI):",
        _EVIDENCE_INSTR,
    ]
    for r in results:
        ident = f"doi:{r['doi']}" if r.get("doi") else (r.get("url") or "no id")
        lines.append(f"- {ident} | {r.get('authors','')} ({r.get('year','')}) — {r['title']}")
        lines.extend(_evidence_lines(r))
    return "\n".join(lines)
