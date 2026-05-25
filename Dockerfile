# syntax=docker/dockerfile:1.7
# Slim runtime image that ships sdc-server behind the JWT license gate.
#
# fhir-sdc is consumed as a prebuilt wheel from Tiro's private GAR (atticus).
# The build needs two BuildKit secrets (both borrowed at build time, never
# landing in any image layer):
#
#   gar_token       — gcloud OAuth token, used to pull fhir-sdc from atticus
#   license_key     — atticus license signing key (PEM), used to sign the
#                     bytecode integrity manifest baked into /app/integrity/
#
# Build (local dev):
#     gcloud auth print-access-token > /tmp/gar-token
#     gcloud secrets versions access latest \
#         --secret=atticus-license-signing-key --project=tiroapp-4cb17 \
#         > /tmp/license-key.pem
#     docker build \
#         --secret id=gar_token,src=/tmp/gar-token \
#         --secret id=license_key,src=/tmp/license-key.pem \
#         -t sdc-server .
#     shred -u /tmp/gar-token /tmp/license-key.pem
#
# Cloud Build does both fetches automatically — see cloudbuild.yaml.

FROM python:3.12-slim-bookworm AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY data ./data
COPY entrypoint.sh /usr/local/bin/entrypoint.sh

# Install fhir-sdc from atticus using a short-lived gcloud access token, then
# everything else (FastAPI, pyjwt, cryptography) from public PyPI, then this
# package itself (sdc-server) without re-resolving its deps.
RUN --mount=type=secret,id=gar_token,required=true \
    chmod +x /usr/local/bin/entrypoint.sh \
    && pip install --no-cache-dir \
        --index-url "https://oauth2accesstoken:$(cat /run/secrets/gar_token)@europe-west1-python.pkg.dev/tiroapp-4cb17/atticus/simple/" \
        'fhir-sdc==0.1.0' \
    && pip install --no-cache-dir 'fastapi[standard]>=0.112.0' 'pyjwt>=2.8' 'cryptography>=42' \
    && pip install --no-cache-dir --no-deps .

# Level 1 hardening: bake ALLOW_LICENSE_SKIP=False into the bytecode (so the
# FHIR_SDC_LICENSE_SKIP env var is a no-op in the published image) and
# replace .py sources with .pyc. Bytecode can still be decompiled, but
# patching it is caught by the Level 2 integrity manifest below.
RUN SITE_PACKAGES="$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')" \
    && printf 'ALLOW_LICENSE_SKIP = False\n' > "$SITE_PACKAGES/sdc_server/_build_flags.py" \
    && python -m compileall -q -b "$SITE_PACKAGES/sdc_server" "$SITE_PACKAGES/fhir_sdc" \
    && find "$SITE_PACKAGES/sdc_server" "$SITE_PACKAGES/fhir_sdc" \
            \( -name '*.py' -o -name '__pycache__' \) -exec rm -rf {} + \
    && rm -rf /app/src /app/pyproject.toml /app/README.md

# Level 2 hardening: SHA-256 every .pyc and the entrypoint, sign the manifest
# with the atticus license key, ship the manifest + signature + pubkey to
# /app/integrity/. entrypoint.sh verifies both signature and hashes before
# invoking Python. Forging the manifest needs the private key (in Secret
# Manager); patching files breaks the hashes. The signing key is mounted
# only during this RUN — never lands in an image layer.
RUN --mount=type=secret,id=license_key,required=true \
    SITE_PACKAGES="$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')" \
    && mkdir -p /app/integrity \
    && (find "$SITE_PACKAGES/sdc_server" "$SITE_PACKAGES/fhir_sdc" -name '*.pyc' -type f; \
        echo /usr/local/bin/entrypoint.sh) \
       | sort | xargs sha256sum > /app/integrity/manifest.sha256 \
    && openssl pkeyutl -sign \
        -inkey /run/secrets/license_key \
        -rawin -in /app/integrity/manifest.sha256 \
        -out /app/integrity/manifest.sig \
    && python -c "from sdc_server.license_gate import EMBEDDED_PUBKEY_PEM; import pathlib; pathlib.Path('/app/integrity/pubkey.pem').write_bytes(EMBEDDED_PUBKEY_PEM)"

# Non-root user. Install dirs, bundled data, entrypoint, and integrity files
# are chmod'd read-only so the runtime user cannot modify them in place.
RUN useradd --create-home --uid 10001 sdc \
    && mkdir -p /etc/sdc-server \
    && chown -R sdc /etc/sdc-server \
    && SITE_PACKAGES="$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')" \
    && chmod -R a-w \
        "$SITE_PACKAGES/sdc_server" \
        "$SITE_PACKAGES/fhir_sdc" \
        /app/data \
        /app/integrity \
        /usr/local/bin/entrypoint.sh
USER sdc

ENV PORT=8000 \
    HOST=0.0.0.0 \
    STRUCTURE_DEFINITIONS_DIR=/app/data/structure-definitions

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
