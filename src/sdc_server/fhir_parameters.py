"""Helpers for reading named entries from a FHIR Parameters resource."""
from __future__ import annotations


def _entries(parameters: dict) -> list[dict]:
    if parameters.get("resourceType") != "Parameters":
        raise ValueError(f"Expected Parameters resource, got {parameters.get('resourceType')!r}")
    return parameters.get("parameter", [])


def get_resource(parameters: dict, name: str) -> dict | None:
    """Return the `resource` value of the first parameter with the given name, or None."""
    for p in _entries(parameters):
        if p.get("name") == name and "resource" in p:
            return p["resource"]
    return None


def get_resources(parameters: dict, name: str) -> list[dict]:
    """Return the `resource` values of all parameters with the given name."""
    return [p["resource"] for p in _entries(parameters) if p.get("name") == name and "resource" in p]


def load_questionnaire(parameters: dict) -> dict | None:
    """Read the `questionnaire` parameter as a Questionnaire resource."""
    return get_resource(parameters, "questionnaire")


def load_questionnaire_response(parameters: dict) -> dict | None:
    """Read the `questionnaire-response` parameter as a QuestionnaireResponse resource."""
    return get_resource(parameters, "questionnaire-response")
