"""Tessera command-line interface (spec §8).

    tessera run    --backbone design.pdb --seq design.fasta --index <dir> --oracle thermompnn-d
                   --out runs/design_042 [--from S3 --to S6] [--preset strict] [--no-retrieve]
    tessera contacts | query | search | stats | candidates | score | report   # each stage standalone
    tessera triage --backbone design.pdb --out runs/triage
    tessera index  --db afdb50 --index-dir <dir>                               # wraps folddisco index
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

from .config import TesseraConfig, merge_overrides
from .orchestrator import RunResult, run

# subcommand -> (from_stage, to_stage) for standalone stage runs
_STAGE_ALIAS = {
    "contacts": ("S1", "S1"),
    "query": ("S2", "S2"),
    "search": ("S3", "S3"),
    "stats": ("S4", "S4"),
    "candidates": ("S5", "S5"),
    "score": ("S6", "S6"),
    "report": ("S7", "S7"),
}


def _coerce(v: str) -> Any:
    low = v.lower()
    if low in ("true", "false"):
        return low == "true"
    for cast in (int, float):
        try:
            return cast(v)
        except ValueError:
            pass
    return v


def _common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--backbone", help="input backbone PDB/mmCIF (fixed input, §S1)")
    p.add_argument("--seq", help="companion sequence (FASTA path or inline); enables sequenced feature mode")
    p.add_argument("--out", help="run output directory")
    p.add_argument("--config", help="tessera.yaml to load before applying flags")
    p.add_argument("--index", dest="index_dir", help="Folddisco index dir (search.index_dir)")
    p.add_argument("--oracle", help="ΔΔG oracle: mock | thermompnn-d | foldx | rosetta")
    p.add_argument("--folddisco", dest="folddisco_backend", help="search backend: mock | folddisco")
    p.add_argument("--clustering", help="reweighting backend: mock | mmseqs2")
    p.add_argument("--source-db", dest="source_db", help="afdb50 | swissprot | pdb")
    p.add_argument("--preset", help="e.g. 'strict' (sep_min=24)")
    p.add_argument("--d-contact", dest="d_contact", type=float)
    p.add_argument("--sep-min", dest="sep_min", type=int)
    p.add_argument("--plddt-min", dest="plddt_min", type=float)
    p.add_argument("--n-eff-min", dest="n_eff_min", type=float)
    p.add_argument("--top-k", dest="top_k", type=int)
    p.add_argument("--candidate-cap", dest="candidate_cap", type=int)
    p.add_argument("--ddg-min-effect", dest="ddg_min_effect", type=float)
    p.add_argument("--no-retrieve", action="store_true", help="oracle-only mode: skip S2-S5 retrieval (§3.7)")
    p.add_argument("-O", dest="overrides", action="append", default=[],
                   metavar="dotted.key=value", help="arbitrary config override (repeatable)")


_ARG_TO_KEY = {
    "backbone": "backbone", "seq": "seq", "out": "out",
    "index_dir": "search.index_dir", "oracle": "oracle.primary",
    "folddisco_backend": "search.backend", "clustering": "stats.clustering_backend",
    "source_db": "search.source_db", "preset": "preset",
    "d_contact": "contact.d_contact", "sep_min": "contact.sep_min",
    "plddt_min": "stats.plddt_min", "n_eff_min": "stats.n_eff_min",
    "top_k": "candidate.top_k", "candidate_cap": "candidate.candidate_cap",
    "ddg_min_effect": "report.ddg_min_effect",
}


def build_config(args: argparse.Namespace) -> TesseraConfig:
    cfg = TesseraConfig.load(getattr(args, "config", None))
    overrides: dict[str, Any] = {}
    for arg_name, key in _ARG_TO_KEY.items():
        val = getattr(args, arg_name, None)
        if val is not None:
            overrides[key] = val
    if getattr(args, "no_retrieve", False):
        overrides["retrieve"] = False
    if getattr(args, "triage", False):
        overrides["triage"] = True
    for item in getattr(args, "overrides", []) or []:
        if "=" not in item:
            raise SystemExit(f"bad -O override (need key=value): {item!r}")
        k, v = item.split("=", 1)
        overrides[k] = _coerce(v)
    return merge_overrides(cfg, overrides)


def _print_result(res: RunResult) -> None:
    if res.triage_regions is not None:
        print(f"triage: {res.triage_regions} weak region(s) -> {res.paths.get('weak_regions')}")
        print(f"        report: {res.paths.get('triage_report')}")
        return
    print(f"run: stages {res.stage_from}..{res.stage_to} -> {res.layout.root}")
    if res.status_counts:
        counts = ", ".join(f"{k}={v}" for k, v in sorted(res.status_counts.items()))
        print(f"     contact status: {counts}")
    print(f"     candidates={res.n_candidates}  recommended={res.n_recommended}")
    if res.provenance.mock_mode:
        print("     [mock mode] — external tools faked; not comparable to real-stack runs (§14.2)")
    for name, path in res.paths.items():
        print(f"     {name}: {path}")


def cmd_index(args: argparse.Namespace) -> int:
    """Wrap `folddisco index` with sane defaults (spec §S3 index block)."""
    threads = args.threads
    cmd = ["folddisco", "index", "-p", args.db, "-i", args.index_dir, "-t", str(threads)]
    if args.big:
        cmd += ["-m", "big"]
    print("would run:", " ".join(cmd))
    print("(build indices locally for IP-sensitive work so no query structures leave the boundary, §10)")
    if args.execute:
        import subprocess  # noqa: PLC0415
        return subprocess.run(cmd, check=False).returncode
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="tessera", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run the full pipeline (or triage)")
    _common_args(p_run)
    p_run.add_argument("--from", dest="stage_from", default="S1", help="first stage (S1..S7)")
    p_run.add_argument("--to", dest="stage_to", default="S7", help="last stage (S1..S7)")
    p_run.add_argument("--triage", action="store_true", help="run the 7A stability-triage front-end")

    for name in _STAGE_ALIAS:
        ps = sub.add_parser(name, help=f"run stage {name} standalone")
        _common_args(ps)

    p_tri = sub.add_parser("triage", help="7A stability-triage front-end")
    _common_args(p_tri)

    p_idx = sub.add_parser("index", help="wrap `folddisco index` with sane defaults")
    p_idx.add_argument("--db", required=True, help="structures dir or foldcomp db")
    p_idx.add_argument("--index-dir", dest="index_dir", required=True)
    p_idx.add_argument("--threads", type=int, default=8)
    p_idx.add_argument("--big", action="store_true")
    p_idx.add_argument("--execute", action="store_true", help="actually run folddisco (default: print only)")

    args = parser.parse_args(argv)

    if args.command == "index":
        return cmd_index(args)

    cfg = build_config(args)
    if args.command == "run":
        res = run(cfg, args.stage_from, args.stage_to)
    elif args.command == "triage":
        cfg.triage = True
        res = run(cfg)
    else:
        frm, to = _STAGE_ALIAS[args.command]
        res = run(cfg, frm, to)
    _print_result(res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
