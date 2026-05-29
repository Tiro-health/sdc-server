"""Reading named entries from a FHIR Parameters resource, plus a centralized
operation-parameter preprocessor used as a FastAPI dependency."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Literal, get_args, get_type_hints, is_typeddict

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
            return {p.key: _read(part, p.name, p.repeats, p.parts) for p in parts}
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
    `repeats` ≡ `max != 1`. The carried shape (`resource` / `value[x]` / `part`)
    is not declared here; it's inferred from the body's keys at parse time (the
    `Parameters` invariant guarantees exactly one of the three per entry).

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
    def key(self) -> str:
        """Typed-dict key for this param: FHIR hyphens become underscores."""
        assert self.name is not None, "Param.name is resolved before use"
        return self.name.replace("-", "_")


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


def _build_params(spec: type) -> tuple[Param, ...]:
    """Introspect a spec TypedDict into resolved `Param`s: each field's `Param`
    metadata, with `name` derived from the field name and `parts` resolved from
    a nested element TypedDict (recursively)."""
    params = []
    for field, hint in get_type_hints(spec, include_extras=True).items():
        meta = _find_param(hint)
        if meta is None:
            raise TypeError(f"{spec.__name__}.{field} is missing a Param annotation")
        params.append(
            replace(meta, name=meta.name or field.replace("_", "-"), parts=_element_spec(hint))
        )
    return tuple(params)


def _missing(value: Any, params: tuple[Param, ...], prefix: str = "") -> list[str]:
    """Names of required params absent from a parsed result, recursing into
    multi-part values (dotted, e.g. `context.content`)."""
    names = []
    for p in params:
        sub = value.get(p.key) if isinstance(value, dict) else None
        if p.required and _absent(sub, p):
            names.append(f"{prefix}{p.name}")
            continue
        if p.parts and sub is not None:
            for occ in (sub if p.repeats else [sub]):
                names.extend(_missing(occ, p.parts, f"{prefix}{p.name}."))
    return names


def operation_parameters(spec: type) -> Callable[..., Awaitable[dict]]:
    """Build a FastAPI dependency that parses an operation's request body into a
    flat dict matching `spec` — a TypedDict whose fields are annotated with
    `Param` metadata, e.g. `Annotated[dict, Param(min=1)]`.

    The field name is the single source of truth: it keys the result dict and,
    with underscores turned back into hyphens, supplies the FHIR parameter name
    (override via `Param(name=...)`). This keeps the result type and the parse
    spec in one declaration, so they cannot drift.

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
                result[p.key] = _read(entries, p.name, p.repeats, p.parts)
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
