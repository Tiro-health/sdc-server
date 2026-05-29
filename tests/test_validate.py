"""Tests for `POST /api/v1/QuestionnaireResponse/$validate` (scaffold).

The validation engine is not implemented yet — the endpoint is wired up and
returns a `501` OperationOutcome. These tests pin the HTTP contract
(parameter parsing, required-parameter validation, conformance) so the engine
can be dropped in later without breaking the seam.
"""

_QUESTIONNAIRE_RESPONSE = {
    "resourceType": "QuestionnaireResponse",
    "status": "completed",
}


def test_validate_not_implemented(client):
    body = {
        "resourceType": "Parameters",
        "parameter": [
            {"name": "questionnaire-response", "resource": _QUESTIONNAIRE_RESPONSE}
        ],
    }
    r = client.post("/api/v1/QuestionnaireResponse/$validate", json=body)

    assert r.status_code == 501, r.json()
    outcome = r.json()
    assert outcome["resourceType"] == "OperationOutcome"
    codes = {i["code"] for i in outcome["issue"]}
    assert codes == {"not-supported"}


def test_validate_missing_required_questionnaire_response(client):
    r = client.post(
        "/api/v1/QuestionnaireResponse/$validate",
        json={"resourceType": "Parameters", "parameter": []},
    )
    assert r.status_code == 400
    outcome = r.json()
    assert outcome["resourceType"] == "OperationOutcome"
    codes = {i["code"] for i in outcome["issue"]}
    assert codes == {"required"}


def test_validate_resource_as_body(client):
    """A bare QuestionnaireResponse (not wrapped in Parameters) is accepted as
    the body and reaches the (stubbed) engine."""
    r = client.post(
        "/api/v1/QuestionnaireResponse/$validate",
        json=_QUESTIONNAIRE_RESPONSE,
    )
    assert r.status_code == 501, r.json()
    codes = {i["code"] for i in r.json()["issue"]}
    assert codes == {"not-supported"}


def test_validate_resource_as_body_any_type(client):
    """The bare-body slot is type-agnostic (no FHIR type-checking infra): a
    wrong-type resource is accepted as the body and reaches the (stubbed)
    engine rather than being rejected up front."""
    r = client.post(
        "/api/v1/QuestionnaireResponse/$validate",
        json={"resourceType": "Bundle"},
    )
    assert r.status_code == 501, r.json()
    codes = {i["code"] for i in r.json()["issue"]}
    assert codes == {"not-supported"}
