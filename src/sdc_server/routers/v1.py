import logging
from typing import Annotated, NotRequired, TypedDict

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from fhir_sdc import extract as sdc_extract

from sdc_server.fhir_parameters import Param, operation_parameters
from sdc_server.structure_definitions import get_structure_definition_loader
from sdc_server.utils import (
    FhirJSONResponse,
    OperationOutcomeException,
    RawJSONResponse,
    binary_wrap_json,
    bundle_transaction,
    client_preferred_content_type,
    operation_not_implemented,
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
                        "type": "Questionnaire",
                        "operation": [
                            {
                                "name": "populate",
                                "definition": "http://hl7.org/fhir/uv/sdc/OperationDefinition/Questionnaire-populate",
                            }
                        ],
                    },
                    {
                        "type": "QuestionnaireResponse",
                        "operation": [
                            {
                                "name": "extract",
                                "definition": "http://hl7.org/fhir/uv/sdc/OperationDefinition/QuestionnaireResponse-extract",
                            },
                            {
                                "name": "validate",
                                "definition": "http://hl7.org/fhir/uv/sdc/OperationDefinition/QuestionnaireResponse-validate",
                            },
                        ],
                    },
                ],
            }
        ],
    }


class ExtractParams(TypedDict):
    questionnaire_response: dict
    questionnaire: dict


extract_params = operation_parameters(
    Param(
        "questionnaire-response",
        required=True,
        as_body=True,
        resource_type="QuestionnaireResponse",
    ),
    Param("questionnaire", required=True, resource_type="Questionnaire"),
)


@router.post("/QuestionnaireResponse/$extract")
def questionnaire_response_extract(
    params: Annotated[ExtractParams, Depends(extract_params)],
    response_content_type: str = Depends(client_preferred_content_type(
        "application/fhir+json",
        "application/json",
    )),
) -> Response:
    """
    FHIR SDC `$extract` operation — definition-based extraction.

    Invoke with either a FHIR `Parameters` resource or a bare
    `QuestionnaireResponse` body, carrying:
      - `questionnaire-response` (required, inline resource — also accepted as
        the bare request body)
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
    q = params["questionnaire"]
    qr = params["questionnaire_response"]

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


class PopulateContext(TypedDict):
    """One SDC `$populate` `context` entry: an alias `name` and its `content`
    (an inline resource or a Reference)."""

    name: str
    content: dict


class PopulateParams(TypedDict):
    questionnaire: dict
    subject: NotRequired[dict | None]
    context: list[PopulateContext]
    local: NotRequired[bool | None]


populate_params = operation_parameters(
    Param("questionnaire", required=True, as_body=True, resource_type="Questionnaire"),
    Param("subject", kind="value"),
    Param("context", kind="part", repeats=True),
    Param("local", kind="value"),
)


@router.post("/Questionnaire/$populate")
def questionnaire_populate(
    params: Annotated[PopulateParams, Depends(populate_params)],
    response_content_type: str = Depends(client_preferred_content_type(
        "application/fhir+json",
        "application/json",
    )),
) -> Response:
    """
    FHIR SDC `$populate` operation — pre-fill a QuestionnaireResponse.

    Invoke with either a FHIR `Parameters` resource or a bare `Questionnaire`
    body, carrying:
      - `questionnaire` (required, inline resource — also accepted as the bare
        request body; this server does not resolve canonical references)
      - `subject`, `context` (repeating), `local` — accepted but currently
        ignored.

    NOTE: the population engine is not implemented yet; this endpoint is wired
    up (parameter parsing, validation, conformance) and returns a `501`
    OperationOutcome until the engine lands.
    """
    questionnaire = params["questionnaire"]

    # TODO(engine): run population (observation/expression/context based) and
    # return a `Parameters` resource whose `response` part is the populated
    # QuestionnaireResponse, alongside any `issues`.
    raise operation_not_implemented("populate")


class ValidateParams(TypedDict):
    questionnaire_response: dict
    questionnaire: NotRequired[dict | None]
    mode: NotRequired[str | None]
    profile: NotRequired[str | None]


validate_params = operation_parameters(
    Param(
        "questionnaire-response",
        required=True,
        as_body=True,
        resource_type="QuestionnaireResponse",
    ),
    Param("questionnaire", resource_type="Questionnaire"),
    Param("mode", kind="value"),
    Param("profile", kind="value"),
)


@router.post("/QuestionnaireResponse/$validate")
def questionnaire_response_validate(
    params: Annotated[ValidateParams, Depends(validate_params)],
    response_content_type: str = Depends(client_preferred_content_type(
        "application/fhir+json",
        "application/json",
    )),
) -> Response:
    """
    FHIR SDC `$validate` operation — validate a QuestionnaireResponse against
    its Questionnaire.

    Invoke with either a FHIR `Parameters` resource or a bare
    `QuestionnaireResponse` body, carrying:
      - `questionnaire-response` (required, inline resource — the resource under
        test; also accepted as the bare request body)
      - `questionnaire` (optional inline; the QR normally references it by
        canonical, which this server cannot resolve), `mode`, `profile` —
        accepted but currently ignored.

    NOTE: the validation engine is not implemented yet; this endpoint is wired
    up (parameter parsing, validation, conformance) and returns a `501`
    OperationOutcome until the engine lands.
    """
    qr = params["questionnaire_response"]

    # TODO(engine): validate the QuestionnaireResponse against its Questionnaire
    # (required items, answer cardinality/type, constraints) and return the
    # resulting OperationOutcome.
    raise operation_not_implemented("validate")
