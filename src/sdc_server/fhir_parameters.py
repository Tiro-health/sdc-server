"""Reading named entries from a FHIR Parameters resource, plus a centralized
operation-parameter preprocessor used as a FastAPI dependency."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

from fastapi import Request

from sdc_server.utils import OperationOutcomeException


def _entries(parameters: dict) -> list[dict]:
    if parameters.get("resourceType") != "Parameters":
        raise ValueError(f"Expected Parameters resource, got {parameters.get('resourceType')!r}")
    return parameters.get("parameter", [])


def _payload(entry: dict) -> Any:
    """The single carried value of a `parameter`/`part` entry: its inline
    `resource`, else its `value[x]`, else None."""
    if "resource" in entry:
        return entry["resource"]
    for key, val in entry.items():
        if key.startswith("value"):
            return val
    return None


def _occurrence(entry: dict, kind: str) -> Any:
    """Read one matching parameter occurrence as `kind`, or None if the entry
    doesn't carry that shape:

      - `"resource"` → the inline `resource`
      - `"value"`    → the `value[x]` (type-suffixed key)
      - `"part"`     → a flat dict of sub-part name → resource/value
    """
    if kind == "resource":
        return entry.get("resource")
    if kind == "part":
        if "part" not in entry:
            return None
        return {sub.get("name"): _payload(sub) for sub in entry["part"]}
    for key, val in entry.items():  # value[x]
        if key.startswith("value"):
            return val
    return None


def read_param(parameters: dict, name: str, kind: str, repeats: bool) -> Any:
    """Read a named parameter from a `Parameters` resource.

    `kind` selects the carried shape (`resource` / `value` / `part`); `repeats`
    selects cardinality independently — these are orthogonal in FHIR, where a
    parameter of any shape may occur more than once. A repeating param returns
    every matching occurrence as a list (`[]` when none); a single param returns
    the first occurrence (or None).
    """
    matches = []
    for p in _entries(parameters):
        if p.get("name") != name:
            continue
        payload = _occurrence(p, kind)
        if payload is not None:
            matches.append(payload)
    return matches if repeats else (matches[0] if matches else None)


def get_resource(parameters: dict, name: str) -> dict | None:
    """The single inline `resource` named `name`, or None."""
    return read_param(parameters, name, "resource", repeats=False)


def get_resources(parameters: dict, name: str) -> list[dict]:
    """All inline `resource`s named `name`."""
    return read_param(parameters, name, "resource", repeats=True)


def get_value(parameters: dict, name: str) -> Any | None:
    """The single `value[x]` named `name`, or None."""
    return read_param(parameters, name, "value", repeats=False)


def get_parts(parameters: dict, name: str) -> list[dict]:
    """All multi-part params named `name`, each as a sub-part name → value dict
    (e.g. SDC `$populate` `context`: `{"name": ..., "content": ...}`)."""
    return read_param(parameters, name, "part", repeats=True)


# --- centralized operation-parameter preprocessor ---------------------------


@dataclass(frozen=True, slots=True)
class Param:
    """A single operation parameter spec.

    `kind` is the carried *shape*, `repeats` is the *cardinality* — orthogonal,
    since FHIR lets a parameter of any shape occur more than once:
      - `kind="resource"` — an inline `resource`
      - `kind="value"`    — a primitive `value[x]`
      - `kind="part"`     — a multi-part param; each occurrence becomes a dict
                            of sub-part name → resource/value (e.g. SDC
                            `$populate` `context`: `{"name": ..., "content": ...}`)
      - `repeats=True`    — returns every occurrence as a list (else the first)

    `as_body` (single resource params only) lets the param arrive as the bare
    request body. `resource_type` (resource params only) is validated against
    the bare body's `resourceType`.
    """

    name: str  # FHIR parameter name, e.g. "questionnaire-response"
    kind: Literal["resource", "value", "part"] = "resource"
    repeats: bool = False
    required: bool = False
    as_body: bool = False
    resource_type: str | None = None

    def __post_init__(self) -> None:
        if self.as_body and (self.kind != "resource" or self.repeats):
            raise ValueError("as_body is only valid on a single 'resource' param")
        if self.resource_type and self.kind != "resource":
            raise ValueError("resource_type is only valid on a 'resource' param")

    @property
    def key(self) -> str:
        """Typed-dict key for this param: FHIR hyphens become underscores."""
        return self.name.replace("-", "_")


def _structure_error(diagnostics: str) -> OperationOutcomeException:
    return OperationOutcomeException(
        status_code=400,
        issues=[{"severity": "error", "code": "structure", "diagnostics": diagnostics}],
    )


def _absent(val: Any, param: Param) -> bool:
    return val == [] if param.repeats else val is None


def operation_parameters(*params: Param) -> Callable[..., Awaitable[dict]]:
    """Build a FastAPI dependency that parses an operation's request body into a
    flat dict keyed by parameter name (hyphens become underscores).

    The body may be either a FHIR `Parameters` resource or — when a param is
    marked `as_body` — the bare resource itself (the FHIR "resource as body"
    invocation). Missing `required` params raise a `400` OperationOutcome, as
    does a malformed body or a bare resource of the wrong type.
    """
    body_param = next((p for p in params if p.as_body), None)

    async def _dep(request: Request) -> dict:
        try:
            body = await request.json()
        except Exception:
            raise _structure_error("Request body is not valid JSON")
        if not isinstance(body, dict):
            raise _structure_error("Request body must be a JSON object")

        result: dict = {}
        rtype = body.get("resourceType")

        if rtype == "Parameters":
            for p in params:
                result[p.key] = read_param(body, p.name, p.kind, p.repeats)
        elif body_param is not None:
            if body_param.resource_type and rtype != body_param.resource_type:
                raise _structure_error(
                    f"Expected {body_param.resource_type} or Parameters resource, "
                    f"got {rtype!r}"
                )
            for p in params:
                result[p.key] = body if p is body_param else ([] if p.repeats else None)
        else:
            raise _structure_error(f"Expected Parameters resource, got {rtype!r}")

        missing = [p.name for p in params if p.required and _absent(result[p.key], p)]
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
        return result

    return _dep
