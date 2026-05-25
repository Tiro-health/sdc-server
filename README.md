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

The production signing key lives in Google Secret Manager:

| Field | Value |
|---|---|
| Project | `tiroapp-4cb17` |
| Secret name | `atticus-license-signing-key` |
| Location | `europe-west1` (user-managed replication) |

The matching public key is embedded in
[`src/sdc_server/license_gate.py`](src/sdc_server/license_gate.py) as
`EMBEDDED_PUBKEY_PEM` and baked into every image at build time.

The secret is named `atticus-` rather than `sdc-server-` on purpose: it's
the umbrella signing key for all Tiro BUSL-gated products. Per-product
scoping happens through the JWT `aud` claim (sdc-server requires
`aud="sdc-server"`), so a token minted for one product can't be used to
unlock another even though they share signing material.

### Mint a customer license

The signing key is fetched from Secret Manager into a temp file, used to
sign one token, then shredded. The key never persists on disk longer than
the duration of `mint_license.py`.

```bash
KEY=$(mktemp)
trap 'shred -u "$KEY" 2>/dev/null || rm -f "$KEY"' EXIT
gcloud secrets versions access latest \
    --secret=atticus-license-signing-key \
    --project=tiroapp-4cb17 > "$KEY"

uv run --no-project --with cryptography --with pyjwt \
    python scripts/mint_license.py \
        --private-key "$KEY" \
        --subject "acme-hospital" \
        --days 90 \
        --out acme.jwt
```

Claims minted:
- `iss = tiro.health`
- `aud = sdc-server`
- `sub` = whatever you pass to `--subject`
- `iat`, `exp` populated from `--days`

### Rotating the signing key

There is **no revocation list** — a minted token is valid until `exp`. The
only way to invalidate outstanding tokens before they expire is to rotate
the keypair:

1. Generate a new keypair locally with
   [`scripts/gen_license_keypair.py`](scripts/gen_license_keypair.py).
2. Add the new private key as a new version of the Secret Manager secret:
   `gcloud secrets versions add atticus-license-signing-key --data-file=…`.
3. Disable old versions:
   `gcloud secrets versions disable 1 --secret=atticus-license-signing-key`.
4. Paste the new public PEM into `EMBEDDED_PUBKEY_PEM`, commit, push. Cloud
   Build rebuilds the image; every outstanding token now fails verification.
5. Re-mint every active customer's license with the new private key.

Plan rotations carefully — step 4 is a hard cutover.

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

---

## CI / publishing

The image is built and published by Cloud Build (config in
[`cloudbuild.yaml`](cloudbuild.yaml)). Published location:
`europe-west1-docker.pkg.dev/tiroapp-4cb17/public/tiro-sdc-server`. The
`public` GAR repo is granted `allUsers` reader so customers can `docker pull`
without a Tiro identity.

Tagging:

| Event | Tags applied |
|---|---|
| Push to `main` | `:SHORT_SHA`, `:main` |
| Push of a `v*` git tag | `:SHORT_SHA`, `:TAG_NAME`, `:latest` |

Manual invocation (for testing, no GitHub trigger needed):

```bash
gcloud builds submit --config=cloudbuild.yaml \
    --substitutions=SHORT_SHA=$(git rev-parse --short HEAD),BRANCH_NAME=manual \
    .
```

The destination repo's retention policy (see
[`cleanup-policy.json`](cleanup-policy.json)):

- Keep forever: any tag starting with `v` or `latest`
- Delete: untagged versions older than 7 days
- Delete: tagged versions older than 90 days (e.g. SHA, `main`)

### One-time admin setup

Run once from a Tiro admin's shell with `gcloud` configured for
`tiroapp-4cb17`:

```bash
# 1. Public pull access on the destination repo
gcloud artifacts repositories add-iam-policy-binding public \
    --location=europe-west1 --project=tiroapp-4cb17 \
    --member=allUsers --role=roles/artifactregistry.reader

# 2. Cloud Build SA → reader on atticus, writer on public
PROJECT_NUMBER=$(gcloud projects describe tiroapp-4cb17 --format='value(projectNumber)')
CB_SA="${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"
gcloud artifacts repositories add-iam-policy-binding atticus \
    --location=europe-west1 --project=tiroapp-4cb17 \
    --member="serviceAccount:${CB_SA}" --role=roles/artifactregistry.reader
gcloud artifacts repositories add-iam-policy-binding public \
    --location=europe-west1 --project=tiroapp-4cb17 \
    --member="serviceAccount:${CB_SA}" --role=roles/artifactregistry.writer

# 3. Apply the cleanup policy
gcloud artifacts repositories set-cleanup-policies public \
    --location=europe-west1 --project=tiroapp-4cb17 \
    --policy=cleanup-policy.json

# 4. Connect the GitHub repo (2nd-gen Cloud Build repos). One-time in browser:
#    https://console.cloud.google.com/cloud-build/triggers/connect?project=tiroapp-4cb17
#    Then map the repo under the existing `atticus-frontend` connection:
gcloud builds repositories create Tiro-health-sdc-server \
    --connection=atticus-frontend --region=europe-west1 --project=tiroapp-4cb17 \
    --remote-uri=https://github.com/Tiro-health/sdc-server.git

# 5. Create the triggers (2nd-gen syntax: --repository, not --repo-owner)
REPO=projects/tiroapp-4cb17/locations/europe-west1/connections/atticus-frontend/repositories/Tiro-health-sdc-server
gcloud builds triggers create github \
    --name=sdc-server-main --region=europe-west1 --project=tiroapp-4cb17 \
    --repository="$REPO" \
    --branch-pattern='^main$' --build-config=cloudbuild.yaml
gcloud builds triggers create github \
    --name=sdc-server-tag --region=europe-west1 --project=tiroapp-4cb17 \
    --repository="$REPO" \
    --tag-pattern='^v.*$' --build-config=cloudbuild.yaml
```
