#!/bin/sh
# Boot sequence (in order):
#   1. Bytecode integrity check — verifies the signed manifest and every
#      file's current hash. Forging the manifest needs the atticus signing
#      key (Secret Manager). Patching files breaks hashes.
#   2. JWT license check — entrypoint pre-check (the FastAPI lifespan also
#      re-checks once uvicorn starts).
#   3. exec uvicorn.
set -eu

python -m sdc_server.integrity_check
python -m sdc_server.license_gate

exec uvicorn sdc_server.app:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}"
