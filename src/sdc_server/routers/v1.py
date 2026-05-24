import logging

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from fhir_sdc import extract as sdc_extract

from sdc_server.fhir_parameters import (
    load_questionnaire,
    load_questionnaire_response,
)
from sdc_server.structure_definitions import get_structure_definition_loader
from sdc_server.utils import (
    FhirJSONResponse,
    OperationOutcomeException,
    RawJSONResponse,
    binary_wrap_json,
    bundle_transaction,
    client_preferred_content_type,
)

LOGGER = logging.getLogger(__name__)

router = APIRouter()


@router.get("/metadata", response_class=FhirJSONResponse)
def capability_statement():
    """FHIR conformance endpoint — describes this server's capabilities."""
    return {
        "resourceType": "CapabilityStatement",
        "status": "active",
        "date": "2026-05-17",
        "kind": "instance",
        "software": {"name": "tiro-sdc-extract", "version": "0.1.0"},
        "fhirVersion": "4.0.1",
        "format": ["application/fhir+json", "application/json"],
        "rest": [
            {
                "mode": "server",
                "resource": [
                    {
                        "type": "QuestionnaireResponse",
                        "operation": [
                            {
                                "name": "extract",
                                "definition": "http://hl7.org/fhir/uv/sdc/OperationDefinition/QuestionnaireResponse-extract",
                            }
                        ],
                    }
                ],
            }
        ],
    }


@router.post("/QuestionnaireResponse/$extract")
def questionnaire_response_extract(
    parameters: dict,
    response_content_type: str = Depends(client_preferred_content_type(
        "application/fhir+json",
        "application/json",
    )),
) -> Response:
    """
    FHIR SDC `$extract` operation — definition-based extraction.

    Body must be a FHIR `Parameters` resource with:
      - `questionnaire-response` (required, inline resource)
      - `questionnaire`          (required, inline resource)

    StructureDefinitions are loaded from the server-side folder configured via
    `STRUCTURE_DEFINITIONS_DIR` (default `<app>/data/structure-definitions/`).

    Response shape depends on what the Questionnaire extracts to:

    - FHIR resources only → a `transaction` Bundle
      (`application/fhir+json`).
    - Logical-model instance(s) only → content-negotiated:
        - `Accept: application/json` → raw JSON of the instance(s).
        - anything else → a FHIR `Binary` wrapping the JSON in base64.
    - Mixed (both FHIR resources and logical-model instances) → 422
      `OperationOutcome`. Split the Questionnaire so each extraction context
      yields one shape.
    """
    try:
        qr = load_questionnaire_response(parameters)
        q = load_questionnaire(parameters)
    except ValueError as e:
        raise OperationOutcomeException(
            status_code=400,
            issues=[{"severity": "error", "code": "structure", "diagnostics": str(e)}],
        )

    missing = [
        name
        for name, val in (("questionnaire-response", qr), ("questionnaire", q))
        if val is None
    ]
    if missing:
        raise OperationOutcomeException(
            status_code=400,
            issues=[
                {
                    "severity": "error",
                    "code": "required",
                    "diagnostics": f"Missing required parameter: {name}",
                }
                for name in missing
            ],
        )
    assert qr is not None and q is not None, (
        "questionnaire-response and questionnaire should be known here"
    )

    loader = get_structure_definition_loader()
    extractor = sdc_extract.DefinitionBasedExtractor(loader, allow_logical_models=True)

    result = extractor.extract(q, qr)

    fatals = [i for i in result.get("issues", []) if i.get("severity") == "fatal"]
    if fatals:
        raise OperationOutcomeException(status_code=422, issues=fatals)

    resources = result["resources"]
    fhir_entries = [r for r in resources if "resourceType" in r]
    lm_entries = [r for r in resources if "resourceType" not in r]

    if fhir_entries and lm_entries:
        raise OperationOutcomeException(
            status_code=422,
            issues=[
                {
                    "severity": "error",
                    "code": "invariant",
                    "diagnostics": (
                        "Extraction produced both FHIR resources and "
                        "logical-model instances. This server returns one "
                        "shape per request — split the Questionnaire so "
                        "each extraction context yields a single shape."
                    ),
                }
            ],
        )

    if lm_entries:
        payload = lm_entries[0] if len(lm_entries) == 1 else lm_entries
        if response_content_type == "application/json":
            return RawJSONResponse(payload)
        return FhirJSONResponse(binary_wrap_json(payload))

    return FhirJSONResponse(bundle_transaction(fhir_entries))
