"""Unit tests for the Parameters readers and the `operation_parameters`
preprocessor — including spec-driven multi-part (`parts`) parsing used by SDC
`$populate` `context`."""
from __future__ import annotations

import asyncio
from typing import Annotated, TypedDict

import pytest

from sdc_server.fhir_parameters import Param, operation_parameters, read_param


class _FakeRequest:
    def __init__(self, body: object) -> None:
        self._body = body

    async def json(self) -> object:
        return self._body


# Module-level so `get_type_hints` can resolve the nested spec (function-local
# TypedDicts aren't in module globals under `from __future__ import annotations`).
class _NestedContext(TypedDict):
    name: Annotated[str, Param(min=1)]
    content: Annotated[dict, Param(min=1)]


class _NestedSpec(TypedDict):
    context: Annotated[list[_NestedContext], Param(max="*")]


def test_operation_parameters_parses_nested_parts():
    """A multi-part param whose element is a Param-annotated TypedDict parses
    each occurrence into a dict keyed by the declared sub-fields, recursing into
    each sub-part's inferred shape (value[x] / resource)."""
    dep = operation_parameters(_NestedSpec)
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
    result = asyncio.run(dep(_FakeRequest(body)))
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


def test_operation_parameters_derives_fhir_name_from_field():
    """The field name is the source of truth: `questionnaire_response` reads the
    FHIR `questionnaire-response` parameter, no name repeated in the Param."""

    class Spec(TypedDict):
        questionnaire_response: Annotated[dict, Param(min=1, type="QuestionnaireResponse")]

    dep = operation_parameters(Spec)
    body = {
        "resourceType": "Parameters",
        "parameter": [
            {"name": "questionnaire-response", "resource": {"resourceType": "QuestionnaireResponse"}}
        ],
    }
    result = asyncio.run(dep(_FakeRequest(body)))
    assert result == {"questionnaire_response": {"resourceType": "QuestionnaireResponse"}}


def test_operation_parameters_requires_param_annotation():
    """A field without Param metadata is a wiring bug — fail loudly at build."""

    class Bad(TypedDict):
        questionnaire: dict

    with pytest.raises(TypeError, match="missing a Param annotation"):
        operation_parameters(Bad)


def test_populate_parses_context_without_error(client):
    """A $populate body carrying multi-part `context` parses cleanly and reaches
    the (stubbed) engine rather than failing in the preprocessor."""
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
