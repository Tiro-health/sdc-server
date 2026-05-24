"""One-shot fixture regenerator.

For every directory under `tests/fixtures/`, runs the extractor against
`(q.json, qr.json)` using the local `sd/` folder as the StructureDefinition
loader and writes the produced resource list back to `expected.json`.

Run: `uv run python tests/regen_fixtures.py` from the app root.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Ensure the app's `src/` is importable when running from tests/.
APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))

from fhir_sdc import extract as sdc_extract  # noqa: E402

FIXTURE_ROOT = APP_ROOT / "tests" / "fixtures"


def _load(p: Path):
    return json.loads(p.read_text())


def regen(fx_dir: Path, sd_dir: Path) -> int:
    """Regenerate `expected.json` inside one fixture directory. Returns # of
    resulting entries."""
    q = _load(fx_dir / "q.json")
    qr = _load(fx_dir / "qr.json")

    os.environ["STRUCTURE_DEFINITIONS_DIR"] = str(sd_dir)
    from src.server.structure_definitions import get_structure_definition_loader
    loader = get_structure_definition_loader()

    extractor = sdc_extract.DefinitionBasedExtractor(loader, allow_logical_models=True)
    result = extractor.extract(q, qr)
    resources = result["resources"]

    (fx_dir / "expected.json").write_text(json.dumps(resources, indent=2) + "\n")
    return len(resources)


def main() -> None:
    fixture_dirs = sorted(
        p for p in FIXTURE_ROOT.iterdir() if p.is_dir() and not p.name.startswith("_")
    )

    with tempfile.TemporaryDirectory() as tmp:
        sd_dir = Path(tmp)
        for fx in fixture_dirs:
            sub = fx / "sd"
            if sub.is_dir():
                for f in sub.glob("*.json"):
                    shutil.copy(f, sd_dir / f"{fx.name}__{f.name}")

        for fx in fixture_dirs:
            n = regen(fx, sd_dir)
            print(f"  {fx.name}: {n} entry(ies)")


if __name__ == "__main__":
    main()
