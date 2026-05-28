"""End-to-end tests for `POST /api/v1/QuestionnaireResponse/$extract`.

Each fixture under `tests/fixtures/<name>/` is a quadruple:
    q.json         — Questionnaire input
    qr.json        — QuestionnaireResponse input
    sd/*.json      — StructureDefinitions (loaded into the server's SD folder by conftest)
    expected.json  — list of resources the extractor should produce

If every entry in `expected.json` has a `resourceType`, the fixture is a
FHIR-resource extraction and the test asserts the response is the matching
`transaction` Bundle. Otherwise the fixture is a logical-model extraction; the
test then requests `Accept: application/json` and asserts the raw JSON body
matches the expected logical-model instance(s).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import fixture_dirs


def _load(path: Path) -> dict | list:
    return json.loads(path.read_text())


def _is_fhir_fixture(expected: list[dict]) -> bool:
    return bool(expected) and all("resourceType" in r for r in expected)


@pytest.mark.parametrize("fixture", fixture_dirs(), ids=lambda p: p.name)
def test_extract(client, fixture: Path):
    q = _load(fixture / "q.json")
    qr = _load(fixture / "qr.json")
    expected = _load(fixture / "expected.json")

    body = {
        "resourceType": "Parameters",
        "parameter": [
            {"name": "questionnaire", "resource": q},
            {"name": "questionnaire-response", "resource": qr},
        ],
    }

    if _is_fhir_fixture(expected):
        r = client.post("/api/v1/QuestionnaireResponse/$extract", json=body)
        assert r.status_code == 200, r.json()
        assert r.headers["content-type"].startswith("application/fhir+json")

        bundle = r.json()
        assert bundle["resourceType"] == "Bundle"
        assert bundle["type"] == "transaction"

        actual = [entry["resource"] for entry in bundle.get("entry", [])]
        assert actual == expected

        for entry, res in zip(bundle["entry"], actual):
            assert entry["fullUrl"].startswith("urn:uuid:")
            assert entry["request"] == {"method": "POST", "url": res["resourceType"]}
    else:
        r = client.post(
            "/api/v1/QuestionnaireResponse/$extract",
            json=body,
            headers={"Accept": "application/json"},
        )
        assert r.status_code == 200, r.json()
        assert r.headers["content-type"].startswith("application/json")

        expected_payload = expected[0] if len(expected) == 1 else expected
        assert r.json() == expected_payload


def test_missing_required_parameters(client):
    r = client.post(
        "/api/v1/QuestionnaireResponse/$extract",
        json={"resourceType": "Parameters", "parameter": []},
    )
    assert r.status_code == 400
    body = r.json()
    assert body["resourceType"] == "OperationOutcome"
    codes = {i["code"] for i in body["issue"]}
    assert codes == {"required"}


def test_non_parameters_body(client):
    r = client.post(
        "/api/v1/QuestionnaireResponse/$extract",
        json={"resourceType": "Bundle"},
    )
    assert r.status_code == 400
    body = r.json()
    assert body["resourceType"] == "OperationOutcome"
    assert body["issue"][0]["code"] == "structure"


def test_extract_bare_questionnaire_response_misses_questionnaire(client):
    """A bare QuestionnaireResponse fills the body slot, but `questionnaire` is
    still required — so it fails required-validation rather than structure."""
    r = client.post(
        "/api/v1/QuestionnaireResponse/$extract",
        json={"resourceType": "QuestionnaireResponse", "status": "completed"},
    )
    assert r.status_code == 400
    body = r.json()
    assert body["resourceType"] == "OperationOutcome"
    assert body["issue"][0]["code"] == "required"
    assert "questionnaire" in body["issue"][0]["diagnostics"]


def test_metadata(client):
    r = client.get("/api/v1/metadata")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/fhir+json")
    body = r.json()
    assert body["resourceType"] == "CapabilityStatement"
    op_names = {
        op["name"]
        for resource in body["rest"][0]["resource"]
        for op in resource.get("operation", [])
    }
    assert {"extract", "populate", "validate"} <= op_names
