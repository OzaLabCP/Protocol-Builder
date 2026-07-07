"""Invariant §9: GPL / commercial external tools are subprocess-only, never imported.

Static AST scan of the whole package: no module imports a GPL binary's Python
bindings as a library. The tools are reached exclusively through the subprocess
adapters.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = pytest.mark.invariant

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "tessera"

# Names that would indicate linking a GPL / restricted tool as a library.
FORBIDDEN_IMPORTS = {"folddisco", "foldseek", "mmseqs", "mmseqs2", "pyrosetta", "rosetta", "foldx"}


def _imported_modules(tree: ast.AST) -> set[str]:
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                mods.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    return mods


def test_no_gpl_library_imports():
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        bad = _imported_modules(tree) & FORBIDDEN_IMPORTS
        if bad:
            offenders.append(f"{path.relative_to(SRC)}: imports {sorted(bad)}")
    assert not offenders, (
        "GPL/commercial tools must be subprocess'd, never imported (§9):\n" + "\n".join(offenders)
    )


def test_external_tools_reached_via_subprocess():
    # the adapters that shell out must actually use subprocess
    search = (SRC / "adapters" / "search.py").read_text()
    clustering = (SRC / "adapters" / "clustering.py").read_text()
    assert "subprocess" in search, "RealFolddisco must invoke the tool via subprocess (§9)"
    assert "subprocess" in clustering, "mmseqs2 clustering must invoke the tool via subprocess (§9)"
