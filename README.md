# sdc-server

FHIR SDC `$extract` service — HTTP front-end for the `fhir-sdc` Rust core,
distributed as a public Docker image under [BUSL-1.1](./LICENSE).

It exposes `POST /api/v1/QuestionnaireResponse/$extract` and accepts a FHIR
`Parameters` resource containing a `Questionnaire` and a
`QuestionnaireResponse`. It returns a `transaction` Bundle of extracted FHIR
resources or, for logical-model targets, the model instance directly (raw JSON
or a `Binary` envelope depending on `Accept`).

The container is gated by a JWT license: on start, the entrypoint verifies the
token, refuses to run if it's missing/expired/invalid, and only then exec's
uvicorn.

---

## Running the image

```bash
docker run -p 8000:8000 \
    -e FHIR_SDC_LICENSE="$(cat my-license.jwt)" \
    ghcr.io/tiro-health/sdc-server:latest
```

Or mount the token file:

```bash
docker run -p 8000:8000 \
    -v $(pwd)/my-license.jwt:/etc/sdc-server/license.jwt:ro \
    ghcr.io/tiro-health/sdc-server:latest
```

If the token is missing, expired, or signed by a key the image doesn't trust,
the container exits with code 2 and logs the reason.

---

## Configuration

| Env var | Description | Default |
|---|---|---|
| `FHIR_SDC_LICENSE` | Signed JWT, inline | — |
| `FHIR_SDC_LICENSE_FILE` | Path to file containing the JWT | `/etc/sdc-server/license.jwt` |
| `FHIR_SDC_LICENSE_PUBKEY` | Override the verification pubkey (PEM string) | embedded |
| `FHIR_SDC_LICENSE_PUBKEY_FILE` | Override the verification pubkey (PEM file) | embedded |
| `FHIR_SDC_LICENSE_SKIP` | `1` bypasses the gate. **Dev/source only** — the published Docker image bakes `ALLOW_LICENSE_SKIP=False` into bytecode at build time, so the env var is a no-op in the image regardless of what the customer sets. | unset |
| `STRUCTURE_DEFINITIONS_DIR` | Folder of FHIR `StructureDefinition` JSON files | `/app/data/structure-definitions/` (in image) / `./data/structure-definitions/` (dev) |
| `HOST` | uvicorn bind host | `0.0.0.0` |
| `PORT` | uvicorn bind port | `8000` |

### Tamper hardening (image only)

The published Docker image installs `sdc_server` and `fhir_sdc` as bytecode
(`.pyc`) and strips the `.py` sources, then `chmod a-w`s the install dirs and
bundled data. The runtime container user can't edit the license check (or any
shipped code) in place. Bytecode decompilers still exist — this stops casual
edits, not a determined attacker. Customers wanting source must contact us.

---

## Issuing a license (internal)

One-time setup — generate the signing keypair, keep the private key safe,
paste the public key into [`src/sdc_server/license_gate.py`](src/sdc_server/license_gate.py)
as `EMBEDDED_PUBKEY_PEM`, then rebuild the image.

```bash
uv run python scripts/gen_license_keypair.py \
    --out-private ./private.pem \
    --out-public ./public.pem
```

Mint a token for a customer:

```bash
uv run python scripts/mint_license.py \
    --private-key ./private.pem \
    --subject "acme-hospital" \
    --days 90 \
    --out acme.jwt
```

Claims minted:
- `iss = tiro.health`
- `aud = sdc-server`
- `sub` = whatever you pass to `--subject`
- `iat`, `exp` populated from `--days`

Extra claims via `--claim key=value` (repeatable) — useful for tracing leaks.

---

## API

### `GET /api/v1/metadata`

FHIR `CapabilityStatement`.

### `POST /api/v1/QuestionnaireResponse/$extract`

Body — a FHIR `Parameters` resource:

```json
{
  "resourceType": "Parameters",
  "parameter": [
    { "name": "questionnaire",          "resource": {  } },
    { "name": "questionnaire-response", "resource": {  } }
  ]
}
```

Response — content-negotiated:

| Extracted result          | `Accept`                          | Response body |
|---|---|---|
| FHIR resources only       | `application/fhir+json` (default) | `transaction` Bundle |
| Logical-model instance(s) | `application/fhir+json` (default) | FHIR `Binary` wrapping JSON in base64 |
| Logical-model instance(s) | `application/json`                | Raw logical-model JSON |
| Mixed FHIR + logical-model | any                              | `422 OperationOutcome` |

Any other error returns an `OperationOutcome`.

---

## Development

`fhir-sdc` is consumed as a prebuilt wheel from Tiro's private Google Artifact
Registry repo (`atticus`). One-time setup so `uv sync` can authenticate:

```bash
# Authenticate gcloud (project must be tiroapp-4cb17 or a tiro.health account)
gcloud auth login
gcloud auth application-default login

# Install the keyring backend that hands GAR credentials to uv/pip
uv tool install keyrings.google-artifactregistry-auth
```

Then:

```bash
uv sync
FHIR_SDC_LICENSE_SKIP=1 uv run fastapi dev main.py
```

`uv sync` reads the index entry in [`pyproject.toml`](pyproject.toml) and
fetches `fhir-sdc==0.1.0` from atticus through the keyring. Bumping the
version is a one-line change to `dependencies = [...]`.

Tests (the conftest sets `FHIR_SDC_LICENSE_SKIP=1` automatically):

```bash
uv run pytest
```

Build the image locally (needs a short-lived GAR access token mounted as a
BuildKit secret — the token does **not** land in the published image):

```bash
gcloud auth print-access-token > /tmp/gar-token
docker build --secret id=gar_token,src=/tmp/gar-token -t sdc-server:dev .
shred -u /tmp/gar-token   # tokens expire in ~1h anyway, but tidy up
```
