"""Server utilities."""
from __future__ import annotations

import base64
import json
import uuid
from typing import Any, Callable

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse


class FhirJSONResponse(JSONResponse):
    """JSONResponse with the FHIR media type."""

    media_type = "application/fhir+json"


class RawJSONResponse(JSONResponse):
    """JSONResponse with the plain JSON media type — used for the raw
    logical-model output branch of the `$extract` endpoint."""

    media_type = "application/json"


class OperationOutcomeException(HTTPException):
    """HTTPException whose `detail` is a FHIR OperationOutcome resource."""

    def __init__(self, status_code: int, issues: list[dict]):
        super().__init__(
            status_code=status_code,
            detail={"resourceType": "OperationOutcome", "issue": issues},
        )


def bundle_transaction(resources: list[dict]) -> dict:
    """Wrap concrete FHIR resources in a `transaction` Bundle ready to be
    POSTed back to a FHIR server (per the SDC `$extract` operation spec).

    Each entry gets a `urn:uuid:` fullUrl so internal references between
    extracted resources resolve once the server creates them, plus a
    `request.method = POST` + `request.url = <resourceType>` so the server
    can execute the transaction.

    Callers must filter logical-model instances out beforehand: every entry
    is assumed to have a `resourceType`.
    """
    entries = []
    for r in resources:
        full_url = f"urn:uuid:{uuid.uuid4()}"
        entries.append({
            "fullUrl": full_url,
            "resource": r,
            "request": {"method": "POST", "url": r["resourceType"]},
        })
    return {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": entries,
    }


def _structured_suffix_supertype(media_type: str) -> str | None:
    """For an RFC 6838 structured-suffix media type, return the implied
    supertype (`application/fhir+json` → `application/json`). Otherwise
    return None."""
    if "/" not in media_type:
        return None
    type_, sub = media_type.split("/", 1)
    if "+" not in sub:
        return None
    _, suffix = sub.rsplit("+", 1)
    return f"{type_}/{suffix}"


def client_preferred_content_type(*server_offers: str) -> Callable[[Request], str]:
    """FastAPI dependency factory for Accept-header content negotiation.

    The returned dependency resolves to the media type the client wants from
    the given server-offered list. `server_offers` is the menu this endpoint
    can produce, in order of *server* preference — the first entry is the
    fallback when the client expresses no usable preference.

    Negotiation rules:

    1. If the client's `Accept` header explicitly lists one or more
       `server_offers`, the most specific listed entry wins. Specificity
       follows RFC 6838's structured-suffix rule: `application/fhir+json`
       refines `application/json`, so when both are listed the structured-
       suffix form is picked.
    2. Otherwise (no header, `* / *` only, or no overlap), the first entry of
       `server_offers` is returned.

    Case-insensitive. Media-type parameters and q-values are ignored.

    Example:

        @router.post("/...")
        def endpoint(
            content_type: str = Depends(client_preferred_content_type(
                "application/fhir+json",  # server default
                "application/json",
            )),
        ):
            if content_type == "application/json":
                return RawJSON(...)
            return FhirBinary(...)
    """
    if not server_offers:
        raise ValueError("at least one server offer must be provided")
    default = server_offers[0]
    offers_lower = [o.lower() for o in server_offers]

    def _dep(request: Request) -> str:
        accept = request.headers.get("accept", "")
        listed = {part.split(";", 1)[0].strip().lower() for part in accept.split(",")}
        candidates = [
            server_offers[i] for i, lo in enumerate(offers_lower) if lo in listed
        ]
        if not candidates:
            return default
        specific = [
            c for c in candidates
            if not any(_structured_suffix_supertype(other) == c.lower() for other in listed)
        ]
        return specific[0] if specific else candidates[0]

    return _dep


def binary_wrap_json(payload: Any) -> dict:
    """Wrap a JSON-serialisable payload in a FHIR `Binary` resource.

    The payload is serialised to compact JSON and base64-encoded into
    `Binary.data`. `Binary.contentType` is `application/json` so consumers
    know how to interpret the decoded bytes.
    """
    encoded = base64.standard_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return {
        "resourceType": "Binary",
        "contentType": "application/json",
        "data": encoded,
    }
