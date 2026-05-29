"""Tests for `POST /api/v1/Questionnaire/$populate` (scaffold).

The population engine is not implemented yet — the endpoint is wired up and
returns a `501` OperationOutcome. These tests pin the HTTP contract
(parameter parsing, required-parameter validation, conformance) so the engine
can be dropped in later without breaking the seam.
"""

_QUESTIONNAIRE = {"resourceType": "Questionnaire", "status": "active"}


def test_populate_not_implemented(client):
    body = {
        "resourceType": "Parameters",
        "parameter": [{"name": "questionnaire", "resource": _QUESTIONNAIRE}],
    }
    r = client.post("/api/v1/Questionnaire/$populate", json=body)

    assert r.status_code == 501, r.json()
    outcome = r.json()
    assert outcome["resourceType"] == "OperationOutcome"
    codes = {i["code"] for i in outcome["issue"]}
    assert codes == {"not-supported"}


def test_populate_missing_required_questionnaire(client):
    r = client.post(
        "/api/v1/Questionnaire/$populate",
        json={"resourceType": "Parameters", "parameter": []},
    )
    assert r.status_code == 400
    outcome = r.json()
    assert outcome["resourceType"] == "OperationOutcome"
    codes = {i["code"] for i in outcome["issue"]}
    assert codes == {"required"}


def test_populate_context_missing_required_sub_part(client):
    """The nested `PopulateContext` spec validates sub-parts: a `context` whose
    `content` part is absent fails required-validation as `context.content`."""
    body = {
        "resourceType": "Parameters",
        "parameter": [
            {"name": "questionnaire", "resource": _QUESTIONNAIRE},
            {"name": "context", "part": [{"name": "name", "valueString": "patient"}]},
        ],
    }
    r = client.post("/api/v1/Questionnaire/$populate", json=body)
    assert r.status_code == 400, r.json()
    outcome = r.json()
    assert outcome["issue"][0]["code"] == "required"
    assert "context.content" in outcome["issue"][0]["diagnostics"]
