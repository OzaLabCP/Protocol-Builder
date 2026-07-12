"""Resolve a citation identifier (DOI or PMID) against a real bibliographic
source so the host can verify — not just trust — every literature_grounded value.

- PMID (digits only) -> NCBI E-utilities esummary (PubMed).
- DOI ('10.' ...)   -> Crossref works API.

Network calls go through the environment's HTTPS proxy automatically (httpx
honours HTTPS_PROXY). Kept dependency-light and fully injectable so validation
can be unit-tested without hitting the network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import httpx

_PMID_RE = re.compile(r"^\d+$")
_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE)

_TIMEOUT = httpx.Timeout(15.0)
_USER_AGENT = "MethodsGapFiller/0.1 (mailto:noreply@example.com)"


@dataclass
class ResolvedCitation:
    """Metadata fetched from the bibliographic source for a given identifier."""

    identifier: str
    kind: str  # "pmid" | "doi"
    title: str
    year: Optional[int]
    source: str  # "pubmed" | "crossref"


def classify_identifier(identifier: str) -> Optional[str]:
    """Return 'pmid', 'doi', or None for a raw identifier string."""
    ident = (identifier or "").strip()
    # Tolerate a leading "doi:" / "PMID:" prefix.
    lower = ident.lower()
    if lower.startswith("doi:"):
        ident = ident[4:].strip()
    elif lower.startswith("pmid:"):
        ident = ident[5:].strip()
    if _PMID_RE.match(ident):
        return "pmid"
    if _DOI_RE.match(ident):
        return "doi"
    return None


def _strip_prefix(identifier: str) -> str:
    ident = identifier.strip()
    low = ident.lower()
    if low.startswith("doi:"):
        return ident[4:].strip()
    if low.startswith("pmid:"):
        return ident[5:].strip()
    return ident


def resolve_pmid(pmid: str, client: httpx.Client) -> Optional[ResolvedCitation]:
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
    params = {"db": "pubmed", "id": pmid, "retmode": "json"}
    resp = client.get(url, params=params)
    resp.raise_for_status()
    data = resp.json()
    result = data.get("result", {})
    record = result.get(pmid)
    if not record or "error" in record or not record.get("title"):
        return None
    year = None
    pubdate = record.get("pubdate") or record.get("epubdate") or ""
    m = re.search(r"\b(\d{4})\b", pubdate)
    if m:
        year = int(m.group(1))
    return ResolvedCitation(
        identifier=pmid,
        kind="pmid",
        title=record.get("title", "").strip().rstrip("."),
        year=year,
        source="pubmed",
    )


def resolve_doi(doi: str, client: httpx.Client) -> Optional[ResolvedCitation]:
    """Resolve a DOI via Crossref, falling back to DataCite. Crossref covers most
    journal DOIs; DataCite covers protocols.io, Zenodo, figshare, and other
    data/protocol DOIs that Crossref returns 404 for."""
    resp = client.get(f"https://api.crossref.org/works/{doi}")
    if resp.status_code != 404:
        resp.raise_for_status()
        message = resp.json().get("message", {})
        titles = message.get("title") or []
        if titles:
            year = None
            for key in ("published", "published-print", "published-online", "issued"):
                parts = (message.get(key) or {}).get("date-parts") or []
                if parts and parts[0] and parts[0][0]:
                    year = int(parts[0][0])
                    break
            return ResolvedCitation(doi, "doi", str(titles[0]).strip(), year, "crossref")
    return _resolve_doi_datacite(doi, client)


def _resolve_doi_datacite(doi: str, client: httpx.Client) -> Optional[ResolvedCitation]:
    resp = client.get(f"https://api.datacite.org/dois/{doi}")
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    attrs = (resp.json().get("data") or {}).get("attributes") or {}
    titles = attrs.get("titles") or []
    title = (titles[0].get("title") if titles else "") or ""
    if not title:
        return None
    year = attrs.get("publicationYear")
    return ResolvedCitation(
        identifier=doi,
        kind="doi",
        title=str(title).strip(),
        year=int(year) if year else None,
        source="datacite",
    )


def resolve_citation(
    identifier: str, client: Optional[httpx.Client] = None
) -> Optional[ResolvedCitation]:
    """Resolve one identifier. Returns None if it doesn't resolve or isn't a
    recognizable DOI/PMID. Never raises for a normal "not found"; network errors
    propagate to the caller, which treats them as unresolved."""
    kind = classify_identifier(identifier)
    if kind is None:
        return None
    ident = _strip_prefix(identifier)

    own_client = client is None
    if own_client:
        client = httpx.Client(
            timeout=_TIMEOUT, headers={"User-Agent": _USER_AGENT}, follow_redirects=True
        )
    try:
        if kind == "pmid":
            return resolve_pmid(ident, client)
        return resolve_doi(ident, client)
    finally:
        if own_client:
            client.close()
