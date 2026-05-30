"""Unit tests for the Parameters readers and the `OperationParams` model —
including spec-driven multi-part (`parts`) parsing used by SDC `$populate`
`context`, the FHIR-name derivation, and the generated request-body schema."""
from typing import Annotated

import pytest
from pydantic import BaseModel

from sdc_server.fhir_parameters import (
    OperationParams,
    Param,
    operation_examples,
    read_param,
)
from sdc_server.utils import OperationOutcomeException


class _NestedContext(BaseModel):
    name: Annotated[str, Param()]
    content: Annotated[dict, Param()]


class _NestedSpec(OperationParams):
    context: Annotated[list[_NestedContext], Param()] = []


def test_model_parses_nested_parts():
    """A multi-part param whose element is a Param-annotated model parses each
    occurrence into the declared sub-fields, recursing into each sub-part's
    inferred shape (value[x] / resource)."""
    body = {
        "resourceType": "Parameters",
        "parameter": [
            {
                "name": "context",
                "part": [
                    {"name": "name", "valueString": "patient"},
                    {"name": "content", "resource": {"resourceType": "Patient", "id": "p1"}},
                ],
            },
            {
                "name": "context",
                "part": [
                    {"name": "name", "valueString": "encounter"},
                    {"name": "content", "valueReference": {"reference": "Encounter/e1"}},
                ],
            },
        ],
    }
    result = _NestedSpec.model_validate(body).model_dump()
    assert result == {
        "context": [
            {"name": "patient", "content": {"resourceType": "Patient", "id": "p1"}},
            {"name": "encounter", "content": {"reference": "Encounter/e1"}},
        ]
    }


def test_read_param_repeating_no_match_returns_empty():
    parameters = {"resourceType": "Parameters", "parameter": []}
    assert read_param(parameters, "code", repeats=True) == []


def test_read_param_repeating_values():
    """The shape is inferred from the keys; `repeats` is the only axis the
    reader needs — a `value[x]` param can repeat too."""
    parameters = {
        "resourceType": "Parameters",
        "parameter": [
            {"name": "code", "valueCode": "a"},
            {"name": "code", "valueCode": "b"},
        ],
    }
    assert read_param(parameters, "code", repeats=True) == ["a", "b"]
    assert read_param(parameters, "code", repeats=False) == "a"


def test_model_derives_fhir_name_from_field():
    """The field name is the source of truth: `questionnaire_response` reads the
    FHIR `questionnaire-response` parameter, no name repeated in the Param."""

    class Spec(OperationParams):
        questionnaire_response: Annotated[dict, Param(type="QuestionnaireResponse")]

    body = {
        "resourceType": "Parameters",
        "parameter": [
            {"name": "questionnaire-response", "resource": {"resourceType": "QuestionnaireResponse"}}
        ],
    }
    assert Spec.model_validate(body).questionnaire_response == {
        "resourceType": "QuestionnaireResponse"
    }


def test_model_missing_required_raises_operation_outcome():
    """A missing required param raises a 400 OperationOutcome from the parsing
    validator, not Pydantic's own ValidationError."""

    class Spec(OperationParams):
        questionnaire: Annotated[dict, Param(type="Questionnaire")]

    with pytest.raises(OperationOutcomeException) as exc:
        Spec.model_validate({"resourceType": "Parameters", "parameter": []})
    assert exc.value.status_code == 400
    assert exc.value.detail["issue"][0]["code"] == "required"


def test_model_requires_param_annotation():
    """A field without Param metadata is a wiring bug — fail loudly at class
    definition (import), not at first request."""

    with pytest.raises(TypeError, match="missing a Param annotation"):

        class Bad(OperationParams):
            questionnaire: dict


def test_model_json_schema_is_parameters_envelope():
    """The model's JSON Schema is the FHIR Parameters wire format (what FastAPI
    puts in the OpenAPI request body), not the flat field model."""

    class Spec(OperationParams):
        questionnaire: Annotated[dict, Param(type="Questionnaire")]

    schema = Spec.model_json_schema()
    assert schema["properties"]["resourceType"]["const"] == "Parameters"
    assert schema["properties"]["parameter"]["type"] == "array"
    assert schema["example"]["resourceType"] == "Parameters"


def test_operation_examples_adds_bare_only_when_as_body_is_sole_required():
    """The bare-resource example is offered only when `as_body` is the operation's
    one required param (so a bare body validates); otherwise just `parameters`."""

    class SoleBody(OperationParams):
        questionnaire: Annotated[dict, Param(as_body=True, type="Questionnaire")]
        local: Annotated[bool | None, Param(type="boolean")] = None

    class BodyPlusRequired(OperationParams):
        questionnaire_response: Annotated[
            dict, Param(as_body=True, type="QuestionnaireResponse")
        ]
        questionnaire: Annotated[dict, Param(type="Questionnaire")]

    sole = operation_examples(SoleBody)
    assert set(sole) == {"parameters", "bare"}
    assert sole["bare"]["value"] == {"resourceType": "Questionnaire"}

    assert set(operation_examples(BodyPlusRequired)) == {"parameters"}


def test_populate_parses_context_without_error(client):
    """A $populate body carrying multi-part `context` parses cleanly and reaches
    the (stubbed) engine rather than failing in the parsing validator."""
    body = {
        "resourceType": "Parameters",
        "parameter": [
            {"name": "questionnaire", "resource": {"resourceType": "Questionnaire", "status": "active"}},
            {
                "name": "context",
                "part": [
                    {"name": "name", "valueString": "patient"},
                    {"name": "content", "resource": {"resourceType": "Patient"}},
                ],
            },
        ],
    }
    r = client.post("/api/v1/Questionnaire/$populate", json=body)
    assert r.status_code == 501, r.json()
    assert {i["code"] for i in r.json()["issue"]} == {"not-supported"}
