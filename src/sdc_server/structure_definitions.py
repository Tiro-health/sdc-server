"""Load FHIR StructureDefinitions from a configured folder for SDC extraction.

The folder is resolved from the `STRUCTURE_DEFINITIONS_DIR` environment variable,
or defaults to the `data/structure-definitions/` directory shipped with this app.

A fresh `DictLoader` is built on each call because `DefinitionBasedExtractor`
consumes the loader (Rust ownership semantics) — a cached one would be drained
after its first use.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from fhir_sdc import extract as sdc_extract

LOGGER = logging.getLogger(__name__)

_PACKAGE_DIR = Path(__file__).resolve().parent
_DEFAULT_DIR = _PACKAGE_DIR.parent.parent / "data" / "structure-definitions"


def _resolve_dir() -> Path:
    override = os.environ.get("STRUCTURE_DEFINITIONS_DIR")
    return Path(override) if override else _DEFAULT_DIR


def get_structure_definition_loader() -> sdc_extract.DictLoader:
    sd_dir = _resolve_dir()
    if not sd_dir.is_dir():
        LOGGER.warning("StructureDefinition dir %s does not exist; using empty loader", sd_dir)
        return sdc_extract.DictLoader()
    return sdc_extract.DictLoader.from_directory(str(sd_dir))
