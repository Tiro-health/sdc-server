# syntax=docker/dockerfile:1.7
# Build the fhir-sdc wheel with maturin from a pinned git ref, then assemble
# a slim runtime image that ships sdc-server behind the JWT license gate.
#
# fhir-sdc-rs is currently private, so the build needs BuildKit SSH forwarding
# (the key never lands in the image — it's only borrowed during the clone
# step). The published binary image does NOT need SSH access; customers pull
# the prebuilt image normally.
#
# Build:
#     docker build --ssh default -t sdc-server .
#
# To target a different fhir-sdc ref:
#     docker build --ssh default --build-arg FHIR_SDC_REF=v0.1.0-rc9 -t sdc-server .

ARG FHIR_SDC_REF=v0.1.0-rc8

# --- Stage 1: build the fhir-sdc wheel ------------------------------------
FROM rust:1.86-slim-bookworm AS wheel-builder
ARG FHIR_SDC_REF

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-dev python3-pip python3-venv \
        git openssh-client pkg-config build-essential ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/maturin-venv \
    && /opt/maturin-venv/bin/pip install --no-cache-dir maturin
ENV PATH="/opt/maturin-venv/bin:${PATH}"

WORKDIR /build
RUN --mount=type=ssh \
    mkdir -p -m 0700 /root/.ssh \
    && ssh-keyscan github.com >> /root/.ssh/known_hosts 2>/dev/null \
    && git clone --depth 1 --branch "${FHIR_SDC_REF}" \
        ssh://git@github.com/Tiro-health/fhir-sdc-rs.git fhir-sdc-rs

RUN cd fhir-sdc-rs \
    && maturin build --release --out /wheels \
        --manifest-path crates/fhir-sdc-py/Cargo.toml

# --- Stage 2: runtime -----------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=wheel-builder /wheels /wheels
COPY pyproject.toml README.md ./
COPY src ./src
COPY data ./data
COPY entrypoint.sh /usr/local/bin/entrypoint.sh

# Install the fhir-sdc wheel first so the resolver finds it before reading
# pyproject's git source (which it would otherwise try to clone over SSH).
RUN chmod +x /usr/local/bin/entrypoint.sh \
    && pip install --no-cache-dir /wheels/*.whl \
    && pip install --no-cache-dir --no-deps . \
    && pip install --no-cache-dir 'fastapi[standard]>=0.112.0' 'pyjwt>=2.8' 'cryptography>=42' \
    && rm -rf /wheels

# Tamper hardening: replace .py sources with .pyc bytecode in the installed
# packages and lock the install dir read-only. Casual edits ("docker exec, vim
# license_gate.py, restart") stop working — bytecode can still be decompiled,
# but raises the bar. See Level 2 (signed-manifest startup check) for the
# follow-up if tampering shows up in practice.
RUN SITE_PACKAGES="$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')" \
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
