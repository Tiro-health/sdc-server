"""Reading named entries from a FHIR Parameters resource, plus a centralized
operation-parameter preprocessor used as a FastAPI dependency."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import (
    Any,
    Awaitable,
    Callable,
    ForwardRef,
    Literal,
    get_args,
    get_origin,
    get_type_hints,
    is_typeddict,
)

from fastapi import Request

from sdc_server.utils import OperationOutcomeException


def _entries(parameters: dict) -> list[dict]:
    if parameters.get("resourceType") != "Parameters":
        raise ValueError(f"Expected Parameters resource, got {parameters.get('resourceType')!r}")
    return parameters.get("parameter", [])


def _occurrence(entry: dict, parts: tuple[Param, ...] = ()) -> Any:
    """The single carried value of a `parameter`/`part` entry, inferred from its
    keys. The FHIR `Parameters` invariant guarantees exactly one of
    `value[x]` / `resource` / `part`, so the shape is read off the body rather
    than declared:

      - inline `resource` → the resource dict
      - `part`            → a dict of declared sub-param key → value, recursing
                            into each; requires a `parts` spec (an unspecced part
                            falls through to `value[x]` and yields None)
      - `value[x]`        → the primitive/complex value
    """
    match entry:
        case {"resource": resource}:
            return resource
        case {"part": part} if parts:
            return {p.key: _read(part, p.fhir_name, p.repeats, p.parts) for p in parts}
        case _:  # value[x] — dynamic key suffix
            return next((v for k, v in entry.items() if k.startswith("value")), None)


def _read(entries: list[dict], name: str, repeats: bool, parts: tuple[Param, ...] = ()) -> Any:
    """Collect entries named `name` from an entry list (`Parameters.parameter`
    or a `part` list), resolving each via `_occurrence`. Returns a list when
    `repeats`, else the first match (or None)."""
    matches = []
    for e in entries:
        if e.get("name") != name:
            continue
        payload = _occurrence(e, parts)
        if payload is not None:
            matches.append(payload)
    return matches if repeats else (matches[0] if matches else None)


def read_param(parameters: dict, name: str, repeats: bool = False) -> Any:
    """Read a named `resource` / `value[x]` parameter from a `Parameters`
    resource; the shape is inferred from each entry's keys and `repeats` selects
    cardinality (a list — `[]` when none — vs the first occurrence, or None).

    Multi-part (`part`) params are not handled here; they need a nested spec, so
    use `operation_parameters`."""
    return _read(_entries(parameters), name, repeats)


# --- centralized operation-parameter preprocessor ---------------------------


@dataclass(frozen=True, slots=True)
class Param:
    """A single operation parameter spec, mirroring `OperationDefinition.parameter`.

    Cardinality follows FHIR — `min`/`max` — so `required` ≡ `min >= 1` and
    `repeats` ≡ `max != 1`. These are NOT authored on the annotation: they are
    derived from the spec TypedDict's Python type by `_build_params` — `min`
    from whether the field is `NotRequired`, `max` from whether the field is a
    `list[...]`. The carried shape (`resource` / `value[x]` / `part`) is
    likewise not declared here; it's inferred from the body's keys at parse
    time (the `Parameters` invariant guarantees exactly one of the three per
    entry).

    `type` / `allowed_types` are carried as metadata only — the FHIR type and,
    for choice/`Reference` params, the acceptable target types — so a
    conformance `OperationDefinition` can be generated from these specs. They
    are NOT validated against the request; this server has no FHIR type-checking
    infrastructure.

    `as_body` (single params only) lets the param arrive as the bare request
    body — the FHIR "resource as body" invocation. The bare body is accepted
    as-is; its type is not checked.

    When used as `Annotated` metadata on a spec TypedDict (see
    `operation_parameters`), `name` defaults to the field name with underscores
    turned back into hyphens; set it explicitly only to override that mapping.
    `parts` is not set in annotations — it's resolved by `operation_parameters`
    from a multi-part param's element TypedDict (e.g. `list[PopulateContext]`),
    making part parsing recursive and spec-driven.
    """

    name: str | None = None  # FHIR parameter name; defaults to the field name
    min: int = 0
    max: int | Literal["*"] = 1
    as_body: bool = False
    type: str | None = None
    allowed_types: tuple[str, ...] = ()
    parts: tuple["Param", ...] = ()  # resolved sub-spec for multi-part params

    def __post_init__(self) -> None:
        if self.as_body and self.repeats:
            raise ValueError("as_body is only valid on a single (max=1) param")

    @property
    def required(self) -> bool:
        """FHIR cardinality lower bound as a boolean."""
        return self.min >= 1

    @property
    def repeats(self) -> bool:
        """Whether `max` allows more than one occurrence."""
        return self.max != 1

    @property
    def fhir_name(self) -> str:
        """Resolved FHIR parameter name. `name` is `str | None` on the annotation
        (defaulting to the field name) but is always resolved by `_build_params`
        before any parsing, so this narrows it to `str`."""
        assert self.name is not None, "Param.name is resolved before use"
        return self.name

    @property
    def key(self) -> str:
        """Typed-dict key for this param: FHIR hyphens become underscores."""
        return self.fhir_name.replace("-", "_")


def _structure_error(diagnostics: str) -> OperationOutcomeException:
    return OperationOutcomeException(
        status_code=400,
        issues=[{"severity": "error", "code": "structure", "diagnostics": diagnostics}],
    )


def _absent(val: Any, param: Param) -> bool:
    return val == [] if param.repeats else val is None


def _find_param(hint: Any) -> Param | None:
    """The first `Param` carried in an `Annotated[...]` hint, searching nested
    type args so `NotRequired[Annotated[T, Param(...)]]` works regardless of
    whether `get_type_hints` strips the `NotRequired` wrapper."""
    for arg in get_args(hint):
        if isinstance(arg, Param):
            return arg
        found = _find_param(arg)
        if found is not None:
            return found
    return None


def _element_spec(hint: Any) -> tuple[Param, ...]:
    """The nested param spec for a multi-part param: if the hint's type carries
    a TypedDict element (e.g. `list[PopulateContext]`), build its params
    recursively; else empty. This is what makes `part` parsing spec-driven."""
    for arg in get_args(hint):
        if is_typeddict(arg):
            return _build_params(arg)
        nested = _element_spec(arg)
        if nested:
            return nested
    return ()


def _is_list(hint: Any) -> bool:
    """Whether the annotated type is a `list[...]` (ignoring `Annotated`
    metadata and any `NotRequired` / `X | None` wrappers)."""
    for arg in get_args(hint):
        if get_origin(arg) is list:
            return True
        if _is_list(arg):
            return True
    return get_origin(hint) is list


def _build_params(spec: type) -> tuple[Param, ...]:
    """Introspect a spec TypedDict into resolved `Param`s. Cardinality is derived
    from the Python type, not authored on the annotation: `min` from whether the
    field is `NotRequired` (via `__required_keys__`), `max` from whether it is a
    `list[...]`. `name` defaults to the field name (hyphenated) and `parts` is
    resolved from a nested element TypedDict (recursively).

    `__required_keys__` is only reliable when the spec's module does NOT use
    `from __future__ import annotations` (PEP 563 leaves the annotations
    unevaluated — as `str` or `ForwardRef` — so the TypedDict cannot see the
    `NotRequired` wrappers and marks every key required). Guard against that
    footgun loudly."""
    if any(isinstance(v, (str, ForwardRef)) for v in spec.__annotations__.values()):
        raise TypeError(
            f"{spec.__name__}: spec TypedDict modules must not use "
            "'from __future__ import annotations' — it breaks NotRequired "
            "detection (__required_keys__)."
        )
    required_keys = spec.__required_keys__
    params = []
    for field, hint in get_type_hints(spec, include_extras=True).items():
        meta = _find_param(hint)
        if meta is None:
            raise TypeError(f"{spec.__name__}.{field} is missing a Param annotation")
        params.append(
            replace(
                meta,
                name=meta.name or field.replace("_", "-"),
                min=1 if field in required_keys else 0,
                max="*" if _is_list(hint) else 1,
                parts=_element_spec(hint),
            )
        )
    return tuple(params)


def _missing(value: Any, params: tuple[Param, ...], prefix: str = "") -> list[str]:
    """Names of required params absent from a parsed result, recursing into
    multi-part values (dotted, e.g. `context.content`)."""
    names = []
    for p in params:
        sub = value.get(p.key) if isinstance(value, dict) else None
        if p.required and _absent(sub, p):
            names.append(f"{prefix}{p.fhir_name}")
            continue
        if p.parts and sub is not None:
            for occ in (sub if p.repeats else [sub]):
                names.extend(_missing(occ, p.parts, f"{prefix}{p.fhir_name}."))
    return names


def operation_parameters(spec: type) -> Callable[..., Awaitable[dict]]:
    """Build a FastAPI dependency that parses an operation's request body into a
    flat dict matching `spec` — a TypedDict whose fields are annotated with
    `Param` metadata, e.g. `Annotated[dict, Param(type="Questionnaire")]`.

    The Python type is the single source of truth. The field name keys the
    result dict and, with underscores turned back into hyphens, supplies the
    FHIR parameter name (override via `Param(name=...)`). Cardinality is derived
    from the type too: a `NotRequired` field is optional (`min=0`), a `list[...]`
    field repeats (`max="*"`). `Param` carries only the FHIR-specific metadata
    that can't be typed (`type`, `allowed_types`, `as_body`). Keeping the result
    type and the parse spec in one declaration means they cannot drift.

    The body may be either a FHIR `Parameters` resource or — when a param is
    marked `as_body` — the bare resource itself (the FHIR "resource as body"
    invocation). Missing `required` params raise a `400` OperationOutcome, as
    does a malformed body. A bare body is accepted as-is; its type is not
    checked.
    """
    params = _build_params(spec)
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
            entries = _entries(body)
            for p in params:
                result[p.key] = _read(entries, p.fhir_name, p.repeats, p.parts)
        elif body_param is not None:
            for p in params:
                result[p.key] = body if p is body_param else ([] if p.repeats else None)
        else:
            raise _structure_error(f"Expected Parameters resource, got {rtype!r}")

        missing = _missing(result, params)
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


# --- OpenAPI request-body schema generation (for Swagger docs) --------------
#
# The operation endpoints parse the body via the `operation_parameters`
# dependency (reading `Request` directly), so FastAPI cannot introspect a
# request body for them. These helpers derive an OpenAPI `requestBody` from the
# SAME `Param` specs (`_build_params`) so Swagger documents — and can exercise —
# the real FHIR `Parameters` wire format, not the flat parsed dict.


# FHIR datatypes carried as `value[x]`; everything else is an inline `resource`.
_VALUE_DATATYPES = {
    "base64Binary", "boolean", "canonical", "code", "date", "dateTime",
    "decimal", "id", "instant", "integer", "markdown", "oid", "string",
    "time", "unsignedInt", "uri", "url", "uuid",
    "Coding", "CodeableConcept", "Identifier", "Period", "Quantity",
    "Range", "Reference",
}
_PRIMITIVE_JSON_TYPE = {"boolean": "boolean", "integer": "integer",
                        "unsignedInt": "integer", "decimal": "number"}


def _value_key(fhir_type: str) -> str:
    """The `value[x]` key for a datatype: `code` → `valueCode`,
    `Reference` → `valueReference`."""
    return "value" + fhir_type[:1].upper() + fhir_type[1:]


def _carried_types(param: Param) -> tuple[str, ...]:
    """The acceptable FHIR types a param entry may carry."""
    return param.allowed_types or ((param.type,) if param.type else ("Resource",))


def _carrier_schema(fhir_type: str) -> dict:
    """JSON Schema for one carried shape: an inline `resource` for a resource
    type, else the matching `value[x]`."""
    if fhir_type in _VALUE_DATATYPES:
        key = _value_key(fhir_type)
        json_type = _PRIMITIVE_JSON_TYPE.get(
            fhir_type, "string" if fhir_type[:1].islower() else "object"
        )
        return {
            "type": "object",
            "required": [key],
            "properties": {key: {"type": json_type, "description": fhir_type}},
        }
    return {
        "type": "object",
        "required": ["resource"],
        "properties": {"resource": {"type": "object", "description": fhir_type}},
    }


def _entry_schema(param: Param) -> dict:
    """JSON Schema for a single `Parameters.parameter` (or nested `part`) entry:
    a fixed `name` plus its carried `resource` / `value[x]` / `part`."""
    name = {"const": param.fhir_name, "type": "string"}
    if param.parts:
        return {
            "type": "object",
            "required": ["name", "part"],
            "properties": {
                "name": name,
                "part": {
                    "type": "array",
                    "items": {"oneOf": [_entry_schema(p) for p in param.parts]},
                },
            },
        }
    carriers = [_carrier_schema(t) for t in _carried_types(param)]
    if len(carriers) == 1:
        c = carriers[0]
        return {
            "type": "object",
            "required": ["name", *c["required"]],
            "properties": {"name": name, **c["properties"]},
        }
    return {
        "type": "object",
        "required": ["name"],
        "properties": {"name": name},
        "oneOf": carriers,
    }


def _example_entry(param: Param) -> dict:
    """A concrete, valid `parameter` entry for the request-body example."""
    if param.parts:
        return {
            "name": param.fhir_name,
            "part": [_example_entry(p) for p in param.parts if p.required],
        }
    fhir_type = _carried_types(param)[0]
    if fhir_type not in _VALUE_DATATYPES:
        return {"name": param.fhir_name, "resource": {"resourceType": fhir_type}}
    samples: dict[str, Any] = {"boolean": True, "integer": 1, "unsignedInt": 1,
                               "decimal": 1.0, "Reference": {"reference": "Patient/example"}}
    sample = samples.get(fhir_type, "example")
    return {"name": param.fhir_name, _value_key(fhir_type): sample}


def operation_request_body(spec: type, *, description: str | None = None) -> dict:
    """An OpenAPI `requestBody` object for an operation, derived from its `Param`
    spec TypedDict. Describes the FHIR `Parameters` wire format and ships a
    minimal valid example (required params only). Attach to a route via
    `@router.post(..., openapi_extra={"requestBody": operation_request_body(Spec)})`."""
    params = _build_params(spec)
    entries = [_entry_schema(p) for p in params]
    schema = {
        "type": "object",
        "title": "FHIR Parameters",
        "required": ["resourceType", "parameter"],
        "properties": {
            "resourceType": {"const": "Parameters", "type": "string"},
            "parameter": {
                "type": "array",
                "items": {"oneOf": entries} if len(entries) > 1 else entries[0],
            },
        },
    }
    if description:
        schema["description"] = description
    example = {
        "resourceType": "Parameters",
        "parameter": [_example_entry(p) for p in params if p.required],
    }
    content = {"schema": schema, "example": example}
    return {
        "required": True,
        "content": {
            "application/fhir+json": content,
            "application/json": content,
        },
    }
