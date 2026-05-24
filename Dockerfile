# Build the fhir-sdc wheel with maturin from a pinned git ref, then assemble
# a slim runtime image that ships sdc-server behind the JWT license gate.
#
# Build context = this repo root:
#     docker build -t sdc-server .
#
# To target a different fhir-sdc ref:
#     docker build --build-arg FHIR_SDC_REF=v0.1.0-rc9 -t sdc-server .

ARG FHIR_SDC_REF=v0.1.0-rc8

# --- Stage 1: build the fhir-sdc wheel ------------------------------------
FROM rust:1.83-slim-bookworm AS wheel-builder
ARG FHIR_SDC_REF

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-dev python3-pip python3-venv \
        git pkg-config build-essential ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/maturin-venv \
    && /opt/maturin-venv/bin/pip install --no-cache-dir maturin
ENV PATH="/opt/maturin-venv/bin:${PATH}"

WORKDIR /build
RUN git clone --depth 1 --branch "${FHIR_SDC_REF}" \
        https://github.com/tiro-health/fhir-sdc-rs.git fhir-sdc-rs

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

# Non-root user
RUN useradd --create-home --uid 10001 sdc \
    && mkdir -p /etc/sdc-server \
    && chown -R sdc /app /etc/sdc-server
USER sdc

ENV PORT=8000 \
    HOST=0.0.0.0

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
