"""Shared pytest fixtures. Stage tests import these — do not redefine them.

The toy structure is the geometrically-controlled hairpin in tests/fixtures/
(hand-verifiable Cβ distances; see tests/fixtures/README.md).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tessera.config import TesseraConfig
from tessera.io.pdb import Structure, load_structure

FIXTURES = Path(__file__).parent / "fixtures"
TOY_PDB = FIXTURES / "toy.pdb"
TOY_SEQUENCE = "ACDEFGHIKLMNPQRSTVWYACDEFG"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def toy_pdb_path() -> Path:
    return TOY_PDB


@pytest.fixture
def toy_structure() -> Structure:
    return load_structure(TOY_PDB)


@pytest.fixture
def toy_sequence() -> str:
    return TOY_SEQUENCE


@pytest.fixture
def base_config() -> TesseraConfig:
    return TesseraConfig(backbone=str(TOY_PDB), seq=TOY_SEQUENCE, out="runs/test")
