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
    """Compact, model-facing rendering of the PubMed hits."""
    if not results:
        return "No PubMed results. Fill this value from best_practice or default_verify instead."
    lines = ["PubMed results (cite a PMID or DOI for literature_grounded values):"]
    for r in results:
        doi = f" doi:{r['doi']}" if r.get("doi") else ""
        lines.append(
            f"- PMID {r['pmid']}{doi} | {r.get('authors','')} ({r.get('year','')}) — {r['title']}"
        )
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
                "resultType": "lite",
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
            out.append(
                {
                    "source": h.get("source", "PPR"),
                    "title": h["title"].strip().rstrip("."),
                    "authors": _first_author_etal(h.get("authorString", "")),
                    "year": year,
                    "doi": doi,
                    "pmid": h.get("pmid"),
                    "url": f"https://doi.org/{doi}" if doi else None,
                }
            )
        return out
    finally:
        if own:
            client.close()


def format_preprints(results: list[dict]) -> str:
    if not results:
        return "No preprint results. Fill from best_practice or default_verify instead."
    lines = ["Preprint results — bioRxiv/medRxiv via Europe PMC (cite the DOI):"]
    for r in results:
        ident = f"doi:{r['doi']}" if r.get("doi") else (f"PMID {r['pmid']}" if r.get("pmid") else "no id")
        lines.append(f"- {ident} | {r.get('authors','')} ({r.get('year','')}) — {r['title']}")
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
            out.append(
                {
                    "title": it["title"].strip(),
                    "authors": (names[0] + (" et al." if len(names) > 1 else "")) if names else "",
                    "year": year,
                    "doi": it.get("doi"),
                    "url": it.get("url") or (f"https://www.protocols.io/view/{uri}" if uri else None),
                }
            )
        return out
    finally:
        if own:
            client.close()


def format_protocols(results: list[dict]) -> str:
    if not results:
        return "No protocols.io results. Fill from best_practice or default_verify instead."
    lines = ["protocols.io results — published protocols (cite the protocol DOI):"]
    for r in results:
        ident = f"doi:{r['doi']}" if r.get("doi") else (r.get("url") or "no id")
        lines.append(f"- {ident} | {r.get('authors','')} ({r.get('year','')}) — {r['title']}")
    return "\n".join(lines)
