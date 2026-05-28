"""Unit tests for the Parameters readers — focused on multi-part (`parts`)
extraction used by SDC `$populate` `context`."""
from __future__ import annotations

from sdc_server.fhir_parameters import get_parts, read_param


def test_get_parts_repeating_context():
    parameters = {
        "resourceType": "Parameters",
        "parameter": [
            {"name": "questionnaire", "resource": {"resourceType": "Questionnaire"}},
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

    assert get_parts(parameters, "context") == [
        {"name": "patient", "content": {"resourceType": "Patient", "id": "p1"}},
        {"name": "encounter", "content": {"reference": "Encounter/e1"}},
    ]


def test_get_parts_absent_returns_empty():
    parameters = {"resourceType": "Parameters", "parameter": []}
    assert get_parts(parameters, "context") == []


def test_read_param_repeating_values():
    """Cardinality is orthogonal to shape: a `value[x]` param can repeat too."""
    parameters = {
        "resourceType": "Parameters",
        "parameter": [
            {"name": "code", "valueCode": "a"},
            {"name": "code", "valueCode": "b"},
        ],
    }
    assert read_param(parameters, "code", "value", repeats=True) == ["a", "b"]
    assert read_param(parameters, "code", "value", repeats=False) == "a"


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
