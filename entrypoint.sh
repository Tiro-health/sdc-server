#!/bin/sh
# Boot sequence (in order):
#   1. Bytecode integrity check (Level 2 tamper hardening): verifies that
#      the signed manifest's signature is good *and* every file's current
#      hash still matches. Catches `.pyc` patches and manifest forgery —
#      forging the manifest requires the atticus signing key, which lives
#      in Google Secret Manager.
#   2. JWT license check (entrypoint pre-check; lifespan also re-checks).
#   3. exec uvicorn.
set -eu

INTEGRITY_DIR=/app/integrity

openssl pkeyutl -verify -pubin -inkey "$INTEGRITY_DIR/pubkey.pem" \
    -rawin -in "$INTEGRITY_DIR/manifest.sha256" \
    -sigfile "$INTEGRITY_DIR/manifest.sig" \
    > /dev/null 2>&1 \
  || { echo "[integrity] manifest signature is invalid — image has been tampered with" >&2; exit 2; }

sha256sum --quiet -c "$INTEGRITY_DIR/manifest.sha256" \
  || { echo "[integrity] file hashes do not match the signed manifest — image has been tampered with" >&2; exit 2; }

echo "[integrity] manifest verified" >&2

python -m sdc_server.license_gate

exec uvicorn sdc_server.app:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}"
