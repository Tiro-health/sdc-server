"""Reading named entries from a FHIR Parameters resource, plus `OperationParams`
— a Pydantic base that turns a spec model into a FastAPI request body: it parses
the FHIR `Parameters` (or bare-resource) wire format into typed fields AND emits
the matching OpenAPI request-body schema, both driven by the same `Param`-
annotated fields. A route needs only `params: MySpec` — no dependency wiring and
no `openapi_extra`."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel, GetJsonSchemaHandler, model_validator
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

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
    declare an `OperationParams` model instead."""
    return _read(_entries(parameters), name, repeats)


# --- parameter spec ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Param:
    """A single operation parameter spec, mirroring `OperationDefinition.parameter`.

    Used as `Annotated` metadata on an `OperationParams` model field, e.g.
    `Annotated[dict, Param(type="Questionnaire")]`. Cardinality follows FHIR —
    `min`/`max`, with `required` ≡ `min >= 1` and `repeats` ≡ `max != 1` — but
    is NOT authored here: it is derived from the field's Python type by
    `_build_params` — `min` from whether the field is required (no default),
    `max` from whether the field is a `list[...]`. The carried shape
    (`resource` / `value[x]` / `part`) is likewise not declared; it's inferred
    from the body's keys at parse time (the `Parameters` invariant guarantees
    exactly one of the three per entry).

    `type` / `allowed_types` are carried as metadata only — the FHIR type and,
    for choice/`Reference` params, the acceptable target types. They drive the
    generated request-body schema (`value[x]` vs `resource`) but are NOT
    validated against the request; this server has no FHIR type-checking
    infrastructure.

    `as_body` (single params only) lets the param arrive as the bare request
    body — the FHIR "resource as body" invocation. The bare body is accepted
    as-is; its type is not checked.

    `name` defaults to the field name with underscores turned back into hyphens;
    set it explicitly only to override that mapping. `parts` is not authored —
    it's resolved by `_build_params` from a multi-part param's element model
    (e.g. `list[PopulateContext]`), making part parsing recursive and spec-driven.
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
        """Model-field key for this param: FHIR hyphens become underscores."""
        return self.fhir_name.replace("-", "_")


def _structure_error(diagnostics: str) -> OperationOutcomeException:
    return OperationOutcomeException(
        status_code=400,
        issues=[{"severity": "error", "code": "structure", "diagnostics": diagnostics}],
    )


def _absent(val: Any, param: Param) -> bool:
    return val == [] if param.repeats else val is None


def _is_list(annotation: Any) -> bool:
    """Whether the field type is a `list[...]` (ignoring `X | None` wrappers)."""
    if get_origin(annotation) is list:
        return True
    return any(_is_list(arg) for arg in get_args(annotation))


def _element_spec(annotation: Any) -> tuple[Param, ...]:
    """The nested param spec for a multi-part param: if the field type carries a
    `BaseModel` element (e.g. `list[PopulateContext]`), build its params
    recursively; else empty. This is what makes `part` parsing spec-driven."""
    for arg in get_args(annotation):
        if isinstance(arg, type) and issubclass(arg, BaseModel):
            return _build_params(arg)
        nested = _element_spec(arg)
        if nested:
            return nested
    return ()


def _build_params(spec: type[BaseModel]) -> tuple[Param, ...]:
    """Introspect a spec model's fields into resolved `Param`s. Each field's
    `Param` metadata is read from its annotation; cardinality is derived from the
    Python type (`min` from `FieldInfo.is_required()`, `max` from `list[...]`),
    `name` defaults to the field name (hyphenated), and `parts` is resolved from
    a nested element model (recursively)."""
    params = []
    for field_name, info in spec.model_fields.items():
        meta = next((m for m in info.metadata if isinstance(m, Param)), None)
        if meta is None:
            raise TypeError(f"{spec.__name__}.{field_name} is missing a Param annotation")
        params.append(
            replace(
                meta,
                name=meta.name or field_name.replace("_", "-"),
                min=1 if info.is_required() else 0,
                max="*" if _is_list(info.annotation) else 1,
                parts=_element_spec(info.annotation),
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


def _parse_body(params: tuple[Param, ...], data: Any) -> dict:
    """Parse a request body into a flat dict keyed by each param's model-field
    key. The body may be a FHIR `Parameters` resource or — when a param is
    `as_body` — the bare resource itself. Missing `required` params raise a
    `400` OperationOutcome, as does a non-object body. A bare body is accepted
    as-is; its type is not checked."""
    if not isinstance(data, dict):
        raise _structure_error("Request body must be a JSON object")

    body_param = next((p for p in params if p.as_body), None)
    result: dict = {}
    rtype = data.get("resourceType")

    if rtype == "Parameters":
        entries = _entries(data)
        for p in params:
            result[p.key] = _read(entries, p.fhir_name, p.repeats, p.parts)
    elif body_param is not None:
        for p in params:
            result[p.key] = data if p is body_param else ([] if p.repeats else None)
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


class OperationParams(BaseModel):
    """Base for a FHIR operation's parameter model. A subclass declares one field
    per parameter, annotated with `Param`; FastAPI then treats the subclass as
    the request body, so a route needs only `params: MySpec` — no dependency and
    no `openapi_extra`.

    The model does double duty, both driven off the same `Param`-annotated fields:

      - parsing: a `mode="before"` validator turns the incoming FHIR `Parameters`
        (or bare-resource) body into the flat field dict, raising a `400`
        OperationOutcome for a malformed body or a missing required param.
      - docs: `__get_pydantic_json_schema__` replaces the auto flat-model schema
        with the FHIR `Parameters` wire-format schema (plus a minimal example),
        so Swagger documents — and can exercise — the real request body.

    The resolved spec is computed once per subclass at class-creation time, so a
    field missing its `Param` annotation fails loudly at import."""

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        cls.__fhir_params__ = _build_params(cls)

    @model_validator(mode="before")
    @classmethod
    def _parse_parameters(cls, data: Any) -> Any:
        return _parse_body(cls.__fhir_params__, data)

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return parameters_schema(cls.__fhir_params__)


# --- OpenAPI request-body schema generation ---------------------------------
#
# Derived from the same `Param` specs as parsing, so Swagger documents the real
# FHIR `Parameters` wire format rather than the flat parsed model.


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


def _parameters_example(params: tuple[Param, ...]) -> dict:
    """A minimal valid FHIR `Parameters` body (required params only)."""
    return {
        "resourceType": "Parameters",
        "parameter": [_example_entry(p) for p in params if p.required],
    }


def parameters_schema(params: tuple[Param, ...]) -> JsonSchemaValue:
    """A JSON Schema for the FHIR `Parameters` request body of an operation,
    derived from its `Param` specs, with a minimal valid example embedded so
    Swagger pre-fills "Try it out"."""
    entries = [_entry_schema(p) for p in params]
    return {
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
        "example": _parameters_example(params),
    }


def operation_examples(spec: type[BaseModel]) -> dict[str, dict]:
    """OpenAPI `openapi_examples` for an operation — the selectable set shown in
    the Swagger request-body dropdown, derived from the same `Param` specs.

    Always offers the FHIR `Parameters` invocation. Adds the bare-resource
    invocation (FHIR "resource as body") only when the `as_body` param is the
    operation's sole required param — otherwise a bare body would fail
    required-validation, so it would be a misleading example.

    Attach via `params: Annotated[Spec, Body(openapi_examples=operation_examples(Spec))]`."""
    params = _build_params(spec)
    examples: dict[str, dict] = {
        "parameters": {
            "summary": "FHIR Parameters resource",
            "value": _parameters_example(params),
        }
    }
    body_param = next((p for p in params if p.as_body), None)
    if body_param is not None and [p for p in params if p.required] == [body_param]:
        fhir_type = _carried_types(body_param)[0]
        examples["bare"] = {
            "summary": f"Bare {fhir_type} (resource as body)",
            "value": {"resourceType": fhir_type},
        }
    return examples
