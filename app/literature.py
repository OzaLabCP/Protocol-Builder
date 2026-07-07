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


def search_pubmed(
    query: str, retmax: int = 5, client: Optional[httpx.Client] = None
) -> list[dict]:
    """Return up to `retmax` PubMed hits as dicts:
    {pmid, title, authors, year, doi}. Raises on network error."""
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
        out = []
        for pmid in idlist:
            rec = result.get(pmid)
            if not rec or not rec.get("title"):
                continue
            out.append(
                {
                    "pmid": pmid,
                    "title": rec.get("title", "").strip().rstrip("."),
                    "authors": _authors(rec),
                    "year": _year(rec.get("pubdate") or rec.get("epubdate") or ""),
                    "doi": _doi(rec),
                }
            )
        return out
    finally:
        if own:
            client.close()


def format_results(results: list[dict]) -> str:
    """Compact, model-facing rendering of the hits."""
    if not results:
        return "No PubMed results. Fill this value from best_practice or default_verify instead."
    lines = ["PubMed results (cite a PMID or DOI for literature_grounded values):"]
    for r in results:
        doi = f" doi:{r['doi']}" if r.get("doi") else ""
        lines.append(
            f"- PMID {r['pmid']}{doi} | {r.get('authors','')} ({r.get('year','')}) — {r['title']}"
        )
    return "\n".join(lines)
