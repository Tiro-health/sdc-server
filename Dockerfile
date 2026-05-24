# syntax=docker/dockerfile:1.7
# Slim runtime image that ships sdc-server behind the JWT license gate.
#
# fhir-sdc is consumed as a prebuilt wheel from Tiro's private GAR (atticus),
# so the build no longer needs a Rust toolchain. The access token is mounted
# as a BuildKit secret and never lands in any layer.
#
# Build:
#     gcloud auth print-access-token > /tmp/gar-token
#     docker build --secret id=gar_token,src=/tmp/gar-token -t sdc-server .
#
# (See README for the keypair / license-minting workflow before publishing.)

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

# Tamper hardening: bake ALLOW_LICENSE_SKIP=False into the bytecode (so the
# FHIR_SDC_LICENSE_SKIP env var is a no-op in the published image), then
# replace .py sources with .pyc and lock the install dir read-only. Bytecode
# can still be decompiled — this defeats casual edits, not a determined
# attacker. See Level 2 (signed-manifest startup check) for the follow-up if
# tampering shows up in practice.
RUN SITE_PACKAGES="$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')" \
    && printf 'ALLOW_LICENSE_SKIP = False\n' > "$SITE_PACKAGES/sdc_server/_build_flags.py" \
    && python -m compileall -q -b "$SITE_PACKAGES/sdc_server" "$SITE_PACKAGES/fhir_sdc" \
    && find "$SITE_PACKAGES/sdc_server" "$SITE_PACKAGES/fhir_sdc" \
            \( -name '*.py' -o -name '__pycache__' \) -exec rm -rf {} + \
    && rm -rf /app/src /app/pyproject.toml /app/README.md

# Non-root user. Install dirs and bundled data are chmod'd read-only so the
# runtime user cannot modify code or shipped StructureDefinitions in place.
RUN useradd --create-home --uid 10001 sdc \
    && mkdir -p /etc/sdc-server \
    && chown -R sdc /etc/sdc-server \
    && SITE_PACKAGES="$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')" \
    && chmod -R a-w "$SITE_PACKAGES/sdc_server" "$SITE_PACKAGES/fhir_sdc" /app/data /usr/local/bin/entrypoint.sh
USER sdc

ENV PORT=8000 \
    HOST=0.0.0.0 \
    STRUCTURE_DEFINITIONS_DIR=/app/data/structure-definitions

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
