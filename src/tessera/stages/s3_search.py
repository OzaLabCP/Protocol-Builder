"""S3 · Folddisco search (spec §S3).

A thin loop over the Folddisco backend: one discontinuous-motif query per cluster,
writing `hits/<cluster>.tsv` and returning parsed hits. The heavy lifting (and the
GPL, subprocess-only tool) lives in the adapter; this stage just orchestrates the
per-cluster calls and persistence.
"""

from __future__ import annotations

from ..adapters.search import FolddiscoBackend
from ..artifacts import RunLayout, write_hits_tsv
from ..config import SearchConfig
from ..schemas.hits import ClusterHits
from ..schemas.queries import QueryManifest


def run_search(
    manifest: QueryManifest,
    backend: FolddiscoBackend,
    cfg: SearchConfig,
    sequence: str | None,
    layout: RunLayout,
) -> dict[str, ClusterHits]:
    """Run one Folddisco query per cluster; write hits/*.tsv; return parsed hits."""
    layout.hits_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, ClusterHits] = {}
    for q in manifest.queries:
        hits = backend.search(
            q,
            index_dir=cfg.index_dir,
            sequence=sequence,
            top_n=cfg.top_n_prefilter,
            skip_match=cfg.skip_match,
            source_db=cfg.source_db,
        )
        write_hits_tsv(hits, layout.hits_dir / f"{q.cluster_id}.tsv")
        out[q.cluster_id] = hits
    return out
