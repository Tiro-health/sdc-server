# Maintainers' guide

Internal Tiro documentation for `sdc-server` — license minting, key
rotation, local development, and the Cloud Build pipeline. Customers
don't need any of this; see [README.md](./README.md) for the external
view.

## Issuing customer licenses

### Signing key

The production signing key lives in Google Secret Manager:

| Field | Value |
|---|---|
| Project | `tiroapp-4cb17` |
| Secret | `atticus-license-signing-key` |
| Location | `europe-west1` (user-managed replication) |
| Access | `group:engineering@tiro.health` has `roles/secretmanager.secretAccessor` |

The matching public key is embedded in
[`src/sdc_server/license_gate.py`](src/sdc_server/license_gate.py) as
`EMBEDDED_PUBKEY_PEM` and baked into every image.

The secret is named `atticus-` (not `sdc-server-`) on purpose: it's the
umbrella signing key for **all** Tiro BUSL-gated products. Per-product
scoping happens through the JWT `aud` claim (sdc-server requires
`aud="sdc-server"`), so a token minted for one product can't unlock
another even though they share signing material.

### Mint a customer license

The signing key is fetched into a temp file via `mktemp`, used to sign
one token, then shredded by the `trap`. The key never persists on disk
longer than the duration of `mint_license.py`.

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
- Extra claims via `--claim key=value` (repeatable) — useful for tracing leaks.

### Rotating the signing key

There is **no revocation list** — a minted token is valid until `exp`.
The only way to invalidate outstanding tokens before they expire is to
rotate the keypair:

1. Generate a new keypair locally with
   [`scripts/gen_license_keypair.py`](scripts/gen_license_keypair.py).
2. Add the new private key as a new version of the Secret Manager secret:
   `gcloud secrets versions add atticus-license-signing-key --data-file=…`.
3. Disable old versions:
   `gcloud secrets versions disable 1 --secret=atticus-license-signing-key`.
4. Paste the new public PEM into `EMBEDDED_PUBKEY_PEM`, commit, push.
   Cloud Build rebuilds the image; every outstanding token now fails
   verification.
5. Re-mint every active customer's license with the new private key.

Plan rotations carefully — step 4 is a hard cutover.

## Internal env vars (not customer-facing)

Customers see the env vars documented in the [README](./README.md). These
additional ones exist for internal/dev use:

| Env var | Description |
|---|---|
| `FHIR_SDC_LICENSE_PUBKEY` | Override the verification pubkey (PEM string). Used in dev to test against a non-production key. |
| `FHIR_SDC_LICENSE_PUBKEY_FILE` | Override the verification pubkey (PEM file path). |
| `FHIR_SDC_LICENSE_SKIP` | `1` bypasses the license gate. **Dev only** — the published image bakes `ALLOW_LICENSE_SKIP=False` into bytecode, so this env var is a no-op in the image regardless of what's set. |

## Tamper hardening (detailed)

The published image hardens against casual tampering in three layers:

1. **Bytecode only.** `compileall` produces `.pyc` for `sdc_server` +
   `fhir_sdc`; the `.py` sources are deleted. Decompilers (`decompyle3`)
   can still reverse it, but "open file, change one line" doesn't work.
2. **Read-only install.** The install dir, `/app/data/`, and
   `entrypoint.sh` are `chmod -R a-w` after compilation. The non-root
   `sdc` user (uid 10001) can't modify them.
3. **Signed integrity manifest.** Every `.pyc` + `entrypoint.sh` is
   SHA-256 hashed at build time; the manifest is signed with the atticus
   key. At startup, [`sdc_server.integrity_check`](src/sdc_server/integrity_check.py)
   verifies the signature and recomputes every hash before the license
   gate runs. Forging the manifest needs the signing key; patching files
   breaks the hashes.

This stops casual tampering. It does **not** stop a customer with root
inside their container who is determined to bypass the gate (they can
`docker run --user 0`, patch `entrypoint.sh`, `docker commit` a new
image). For that threat model the enforcement layer is contractual —
the BUSL license terms + audit clauses + short expiries.

## Development

`fhir-sdc` is consumed as a prebuilt wheel from Tiro's private Google
Artifact Registry repo (`atticus`). One-time setup so `uv sync` can
authenticate:

```bash
gcloud auth login
gcloud auth application-default login
uv tool install keyrings.google-artifactregistry-auth
```

Then:

```bash
uv sync
FHIR_SDC_LICENSE_SKIP=1 uv run fastapi dev main.py
```

`uv sync` reads the index entry in [`pyproject.toml`](pyproject.toml) and
fetches `fhir-sdc==X.Y.Z` from atticus through the keyring. Bumping the
version is a one-line change to `dependencies = [...]`.

Tests (the conftest sets `FHIR_SDC_LICENSE_SKIP=1` automatically):

```bash
uv run pytest
```

### Build the image locally

```bash
gcloud auth print-access-token > /tmp/gar-token
gcloud secrets versions access latest \
    --secret=atticus-license-signing-key \
    --project=tiroapp-4cb17 > /tmp/license-key.pem

docker build \
    --secret id=gar_token,src=/tmp/gar-token \
    --secret id=license_key,src=/tmp/license-key.pem \
    -t sdc-server:dev .

shred -u /tmp/gar-token /tmp/license-key.pem
```

The `gar_token` is a short-lived OAuth token for pulling `fhir-sdc` from
atticus; `license_key` is the atticus signing key used to sign the
bytecode integrity manifest. Neither lands in any image layer (BuildKit
secret mounts).

## CI / publishing

The image is built and published by Cloud Build (config in
[`cloudbuild.yaml`](cloudbuild.yaml)).

**Published location:**
`europe-west1-docker.pkg.dev/tiroapp-4cb17/public/tiro-sdc-server` —
multi-arch (`linux/amd64` + `linux/arm64`), `allUsers` has reader on the
`public` GAR repo so customers can `docker pull` anonymously.

**Tagging:**

| Event | Tags applied |
|---|---|
| Push to `main` | `:SHORT_SHA`, `:main` |
| Push of a `v*` git tag | `:SHORT_SHA`, `:TAG_NAME`, `:latest` |

**Manual invocation** (no GitHub trigger needed):

```bash
gcloud builds submit --config=cloudbuild.yaml \
    --substitutions=SHORT_SHA=$(git rev-parse --short HEAD),BRANCH_NAME=manual \
    .
```

**Destination repo retention** (see
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

# 2. Cloud Build SA → reader on atticus, writer on public, accessor on
#    the signing key
PROJECT_NUMBER=$(gcloud projects describe tiroapp-4cb17 --format='value(projectNumber)')
CB_SA="${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"
gcloud artifacts repositories add-iam-policy-binding atticus \
    --location=europe-west1 --project=tiroapp-4cb17 \
    --member="serviceAccount:${CB_SA}" --role=roles/artifactregistry.reader
gcloud artifacts repositories add-iam-policy-binding public \
    --location=europe-west1 --project=tiroapp-4cb17 \
    --member="serviceAccount:${CB_SA}" --role=roles/artifactregistry.writer
gcloud secrets add-iam-policy-binding atticus-license-signing-key \
    --project=tiroapp-4cb17 \
    --member="serviceAccount:${CB_SA}" --role=roles/secretmanager.secretAccessor

# 3. Apply the cleanup policy
gcloud artifacts repositories set-cleanup-policies public \
    --location=europe-west1 --project=tiroapp-4cb17 \
    --policy=cleanup-policy.json

# 4. Connect the GitHub repo (2nd-gen Cloud Build repos). One-time, in
#    browser: https://console.cloud.google.com/cloud-build/triggers/connect?project=tiroapp-4cb17
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
