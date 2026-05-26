# sdc-server

> [!WARNING]
> **Work in progress — building in public.** This repository is a public
> reconstruction of Tiro.health's internal FHIR SDC server. We're rebuilding
> it in the open so the community can follow along, contribute, and depend
> on a transparent reference implementation. APIs, image tags, and behavior
> may change without notice until a 1.0 release is cut.

A FHIR SDC `$extract` service — HTTP front-end for the `fhir-sdc` Rust core,
distributed as a public Docker image under [BUSL-1.1](./LICENSE).

`POST /api/v1/QuestionnaireResponse/$extract` takes a FHIR `Parameters`
resource containing a `Questionnaire` and a `QuestionnaireResponse`, and
returns either a `transaction` Bundle of extracted FHIR resources or, for
logical-model targets, the model instance itself (raw JSON or a `Binary`
envelope, depending on `Accept`).

The container is gated by a **JWT license**. On start, the entrypoint
verifies the token; if it's missing, expired, or signed by an untrusted
key, the container exits with code 2 and logs the reason. To obtain a
license, contact Tiro.health.

## Running the image

```bash
docker run -p 8000:8000 \
    -e FHIR_SDC_LICENSE="$(cat my-license.jwt)" \
    europe-west1-docker.pkg.dev/tiroapp-4cb17/public/tiro-sdc-server:latest
```

Or mount the token file:

```bash
docker run -p 8000:8000 \
    -v $(pwd)/my-license.jwt:/etc/sdc-server/license.jwt:ro \
    europe-west1-docker.pkg.dev/tiroapp-4cb17/public/tiro-sdc-server:latest
```

The image ships **no** `StructureDefinition`s — bring your own:

```bash
docker run -p 8000:8000 \
    -e FHIR_SDC_LICENSE="$(cat my-license.jwt)" \
    -v $(pwd)/structure-definitions:/app/data/structure-definitions:ro \
    europe-west1-docker.pkg.dev/tiroapp-4cb17/public/tiro-sdc-server:latest
```

The image is multi-arch (`linux/amd64` + `linux/arm64`) and can be pulled
anonymously.

## Configuration

| Env var | Description | Default |
|---|---|---|
| `FHIR_SDC_LICENSE` | Signed JWT, inline | — |
| `FHIR_SDC_LICENSE_FILE` | Path to a file containing the JWT | `/etc/sdc-server/license.jwt` |
| `STRUCTURE_DEFINITIONS_DIR` | Directory of FHIR `StructureDefinition` JSON files | `/app/data/structure-definitions/` |
| `HOST` | uvicorn bind host | `0.0.0.0` |
| `PORT` | uvicorn bind port | `8000` |

## API

### `GET /api/v1/metadata`

Returns a FHIR `CapabilityStatement` describing this server.

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

Response shape is content-negotiated and depends on what the
`Questionnaire` extracts to:

| Extracted result | `Accept` | Response body |
|---|---|---|
| FHIR resources only | `application/fhir+json` (default) | `transaction` Bundle |
| Logical-model instance(s) | `application/fhir+json` (default) | FHIR `Binary` wrapping JSON in base64 |
| Logical-model instance(s) | `application/json` | Raw logical-model JSON |
| Mixed FHIR + logical-model | any | `422 OperationOutcome` (split the Questionnaire) |

Any other error returns an `OperationOutcome` with a meaningful issue
code.

## Support and licensing

This software is licensed under [BUSL-1.1](./LICENSE). For production
use, license inquiries, source access, or to report issues, contact
Tiro.health.

---

Internal Tiro maintainers — see [MAINTAINERS.md](./MAINTAINERS.md) for
license minting, key rotation, dev setup, and CI/publishing details.
