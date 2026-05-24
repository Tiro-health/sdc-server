#!/bin/sh
# Verify the JWT license before serving traffic. The gate prints either a
# success line or an error and exits non-zero, in which case we abort so the
# container restarts (and surfaces the error to whoever's watching logs).
set -e

python -m sdc_server.license_gate

exec uvicorn sdc_server.app:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}"
