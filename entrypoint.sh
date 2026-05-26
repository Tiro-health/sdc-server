#!/bin/sh
# Thin uvicorn launcher. The bytecode integrity check and the JWT license
# check both run from the FastAPI lifespan (see sdc_server.app) — keeping
# them in bytecode (covered by the signed integrity manifest) means a
# tampered entrypoint cannot silently skip them.
set -eu

exec uvicorn sdc_server.app:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}"
