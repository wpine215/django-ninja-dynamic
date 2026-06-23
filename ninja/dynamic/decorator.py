import inspect
from functools import wraps
from typing import Any, Callable, List, Optional

from django.http import HttpRequest
from pydantic import Field, create_model

from ninja import Query
from ninja.constants import NOT_SET
from ninja.dynamic.config import DEFAULT_CONFIG, DynamicConfig
from ninja.dynamic.openapi import build_openapi_parameters, walk_schema_graph
from ninja.dynamic.parser import parse_query
from ninja.dynamic.queryset import apply_query_optimization
from ninja.dynamic.schema import get_dynamic_meta, unwrap_response_annotation
from ninja.dynamic.selector import (
    FieldSelector,
    _alias_to_name,
    schema_resource_name,
)
from ninja.errors import ConfigError, ValidationError
from ninja.operation import Operation
from ninja.utils import (
    contribute_operation_args,
    contribute_operation_callback,
    is_async_callable,
)


def _make_input_model(config: DynamicConfig) -> type:
    """
    Build a transient pydantic model carrying the two dynamic query params.

    Fields are hidden from OpenAPI via ``include_in_schema=False`` (read by
    django-ninja's ``_extract_parameters``) — we render the dynamic params
    ourselves via ``get_dynamic_openapi_parameters`` so they can reflect the
    bound API's config.
    """
    hidden = {"include_in_schema": False}
    fields: dict = {
        config.fields_param: (Optional[str], Field(None, json_schema_extra=hidden)),
        config.include_param: (Optional[str], Field(None, json_schema_extra=hidden)),
    }
    return create_model("NinjaDynamicInput", **fields)


def _resolve_config(
    decorator_config: Optional[DynamicConfig], op: Optional[Operation]
) -> DynamicConfig:
    """
    Decorator arg → NinjaAPI → DEFAULT_CONFIG.

    Router-level config is not consulted because django-ninja does not keep a
    back-reference from Operation to its source Router; users who want a
    router-level override should pass ``config=`` to the decorator directly.
    """
    if decorator_config is not None:
        return decorator_config
    if op is not None:
        api = getattr(op, "api", None)
        if api is not None:
            cfg = getattr(api, "dynamic_config", None)
            if cfg is not None:
                return cfg
    return DEFAULT_CONFIG


def _root_response_schema(op: Operation):
    """
    Extract ``(schema, is_list)`` from the operation's 200 response model,
    looking through the ``NinjaResponseSchema(response=T)`` envelope.
    """
    response_model = None
    for code in (200, 201, ...):
        if code in op.response_models:
            response_model = op.response_models[code]
            break
    if response_model is None or response_model is NOT_SET:
        for rm in op.response_models.values():
            if rm is not None and rm is not NOT_SET:
                response_model = rm
                break
    if response_model is None or response_model is NOT_SET:
        return None, False
    annotation = response_model.model_fields["response"].annotation
    return unwrap_response_annotation(annotation)


def _validate_input_against_meta(
    selector: FieldSelector,
    schema,
    includable: Optional[List[str]],
    config: DynamicConfig,
) -> None:
    """
    Enforce the runtime contract:

    * ``?fields=`` may only list default-visible (non-Includable) fields.
      Aliases are accepted alongside field names.
    * ``?include=`` may only list paths that match declared Includable
      fields, walking the schema graph for nested dot-paths.
    """
    schema_fields, schema_includable, _ = walk_schema_graph(schema)
    fields_by_resource = {
        schema_resource_name(s): set(fields) | set(_alias_to_name(s).keys())
        for s, fields in schema_fields.items()
    }
    includable_by_resource = {
        schema_resource_name(s): set(allowed) for s, allowed in schema_includable.items()
    }

    root_meta = get_dynamic_meta(schema)
    root_includable_names = (
        set(includable) if includable is not None
        else (set(root_meta.includable) if root_meta else set())
    )
    root_default_visible = set(_alias_to_name(schema).keys())
    root_default_visible_names = {
        canonical for alias, canonical in _alias_to_name(schema).items()
        if canonical not in (root_meta.includable if root_meta else set())
    }
    # ``root_default_visible_alias_or_name`` is the set the user may pass
    # in ``?fields=`` — field names + aliases of default-visible fields only.
    root_default_visible_alias_or_name = root_default_visible - (
        root_meta.includable if root_meta else set()
    )

    # ---- Sparse validation ----
    if config.strict_unknown and selector.sparse:
        for resource, bucket in selector.sparse.items():
            if resource is None:
                allowed = root_default_visible_alias_or_name
                resource_label = "response"
                includable_here = root_includable_names
            else:
                allowed = fields_by_resource.get(resource)
                includable_here = includable_by_resource.get(resource, set())
                if allowed is None:
                    raise ValidationError([{
                        "type": "value_error",
                        "loc": ("query", f"{config.fields_param}[{resource}]"),
                        "msg": (
                            f"Unknown resource: {resource}. "
                            f"Available: {sorted(fields_by_resource)}."
                        ),
                    }])
                allowed = allowed - includable_here

            in_includable = bucket & includable_here
            if in_includable:
                raise ValidationError([{
                    "type": "value_error",
                    "loc": ("query", config.fields_param),
                    "msg": (
                        f"Field(s) {sorted(in_includable)} are includable; "
                        f"use '{config.include_param}' instead of "
                        f"'{config.fields_param}'."
                    ),
                }])

            unknown = sorted(bucket - allowed - includable_here)
            if unknown:
                raise ValidationError([{
                    "type": "value_error",
                    "loc": ("query", config.fields_param),
                    "msg": (
                        f"Unknown field(s) for {resource_label if resource is None else resource}: "
                        f"{unknown}. Available: {sorted(allowed)}."
                    ),
                }])

    # ---- Include validation ----
    # We always resolve aliases here, even when strict_unknown=False, so
    # downstream code (build_include) sees canonical field names.
    if selector.includes:
        normalized: set = set()
        bad: list = []
        for path in selector.includes:
            canonical = _canonicalize_include_path(path, schema, root_includable_names)
            if canonical is None:
                bad.append(".".join(path))
            else:
                normalized.add(canonical)
        if bad and config.strict_unknown:
            allowed_paths = sorted(
                ".".join(p) for p in _enumerate_includable_paths(schema)
            )
            raise ValidationError([{
                "type": "value_error",
                "loc": ("query", config.include_param),
                "msg": (
                    f"Unknown include value(s): {sorted(bad)}. "
                    f"Available: {allowed_paths}."
                ),
            }])
        selector.includes = normalized


def _canonicalize_include_path(path, schema, root_overrides):
    """
    Walk a dot-path through the schema graph, accepting either field names
    or aliases at each segment, and return the canonical field-name path.
    Returns ``None`` if any segment doesn't name an Includable field on its
    parent schema.

    Root-segment validation can be overridden by an explicit
    ``includable=[...]`` decorator arg.
    """
    from ninja.dynamic.selector import _alias_to_name, _resolve_field_schema

    cur_schema = schema
    canonical_segments: list = []
    for i, segment in enumerate(path):
        if cur_schema is None:
            return None
        # Map alias → canonical field name at this level. Unknown segments
        # pass through unchanged so the includable check below rejects them.
        alias_map = _alias_to_name(cur_schema)
        canonical = alias_map.get(segment, segment)
        meta = get_dynamic_meta(cur_schema)
        allowed = (
            root_overrides
            if i == 0 and root_overrides
            else (set(meta.includable) if meta else set())
        )
        if canonical not in allowed:
            return None
        if canonical not in cur_schema.model_fields:
            return None
        canonical_segments.append(canonical)
        cur_schema = _resolve_field_schema(cur_schema.model_fields[canonical].annotation)
    return tuple(canonical_segments)


def _enumerate_includable_paths(schema):
    """All dot-paths reachable from ``schema`` whose segments are Includable."""
    from ninja.dynamic.selector import _resolve_field_schema

    out = set()

    def walk(current, prefix=()):
        meta = get_dynamic_meta(current)
        if not meta:
            return
        for name in meta.includable:
            path = prefix + (name,)
            out.add(path)
            fld = current.model_fields.get(name)
            if fld is None:
                continue
            nested = _resolve_field_schema(fld.annotation)
            if nested is not None:
                walk(nested, path)

    walk(schema)
    return out


_VIEW_STATE_ATTR = "_ninja_dynamic_state"


class _DynamicState:
    """
    State stashed on the wrapped view (shared across all clones of the
    Operation, since clones inherit ``view_func``).
    """

    __slots__ = ("decorator_config", "response_schema", "includable")

    def __init__(self, decorator_config, response_schema, includable):
        self.decorator_config = decorator_config
        self.response_schema = response_schema
        self.includable = includable


def _modify_operation_for_dynamic(
    decorator_config: Optional[DynamicConfig],
    includable: Optional[List[str]],
    op: Operation,
    target_view: Callable[..., Any],
) -> None:
    schema, _ = _root_response_schema(op)
    if schema is None:
        raise ConfigError(
            "@dynamic_response requires a Schema-typed response (got none on "
            f"{op.view_func.__module__}.{op.view_func.__name__})."
        )

    # Stash state on ``target_view`` — the specific wrapper created by our
    # decorator — rather than on ``op.view_func``, because another decorator
    # (e.g. @paginate) may have wrapped us.
    setattr(
        target_view,
        _VIEW_STATE_ATTR,
        _DynamicState(decorator_config, schema, includable),
    )


def get_dynamic_state(op_or_view) -> Optional[_DynamicState]:
    """
    Locate the dynamic state on a view chain, walking ``__wrapped__`` so we
    find it whether or not other decorators wrap our wrapper.
    """
    current = getattr(op_or_view, "view_func", op_or_view)
    while current is not None:
        state = getattr(current, _VIEW_STATE_ATTR, None)
        if state is not None:
            return state
        current = getattr(current, "__wrapped__", None)
    return None


def get_dynamic_openapi_parameters(op: Operation) -> List[dict]:
    """
    Called by the OpenAPI renderer for operations marked as dynamic.
    Resolves the effective config from the bound API and returns parameter
    dicts.
    """
    state = get_dynamic_state(op)
    if state is None:
        return []
    config = _resolve_config(state.decorator_config, op)
    return build_openapi_parameters(state.response_schema, config)


def _inject_dynamic(
    func: Callable[..., Any],
    *,
    config: Optional[DynamicConfig] = None,
    includable: Optional[List[str]] = None,
    optimize_queryset: bool = True,
) -> Callable[..., Any]:
    if getattr(func, "_ninja_is_dynamic", False):
        return func

    effective_config = config if config is not None else DEFAULT_CONFIG
    DynamicInput = _make_input_model(effective_config)

    def _live_config(state, request: HttpRequest) -> DynamicConfig:
        decorator_cfg = state.decorator_config if state is not None else config
        op = getattr(request, "_ninja_operation", None)
        return _resolve_config(decorator_cfg, op)

    def _attach(request, schema, sel, cfg):
        _validate_input_against_meta(sel, schema, includable, cfg)
        request._ninja_dynamic_selector = sel  # type: ignore[attr-defined]
        request._ninja_dynamic_response_schema = schema  # type: ignore[attr-defined]

    if is_async_callable(func):
        @wraps(func)
        async def view_with_dynamic(request: HttpRequest, **kwargs: Any) -> Any:
            kwargs.pop("ninja_dynamic", None)
            state = getattr(view_with_dynamic, _VIEW_STATE_ATTR, None)
            schema = state.response_schema if state is not None else None
            cfg = _live_config(state, request)
            sel = parse_query(request.GET, cfg)
            if schema is not None:
                _attach(request, schema, sel, cfg)
            result = await func(request, **kwargs)
            if optimize_queryset and schema is not None:
                result = apply_query_optimization(result, sel, schema)
            return result
    else:
        @wraps(func)
        def view_with_dynamic(request: HttpRequest, **kwargs: Any) -> Any:
            kwargs.pop("ninja_dynamic", None)
            state = getattr(view_with_dynamic, _VIEW_STATE_ATTR, None)
            schema = state.response_schema if state is not None else None
            cfg = _live_config(state, request)
            sel = parse_query(request.GET, cfg)
            if schema is not None:
                _attach(request, schema, sel, cfg)
            result = func(request, **kwargs)
            if optimize_queryset and schema is not None:
                result = apply_query_optimization(result, sel, schema)
            return result

    contribute_operation_args(
        view_with_dynamic, "ninja_dynamic", DynamicInput, Query(None)
    )

    def _callback(op: Operation) -> None:
        _modify_operation_for_dynamic(
            config, includable, op, target_view=view_with_dynamic
        )

    contribute_operation_callback(view_with_dynamic, _callback)

    view_with_dynamic._ninja_is_dynamic = True  # type: ignore[attr-defined]
    return view_with_dynamic


def dynamic_response(
    func_or_none: Any = NOT_SET,
    *,
    config: Optional[DynamicConfig] = None,
    includable: Optional[List[str]] = None,
    optimize_queryset: bool = True,
) -> Any:
    """
    Decorator that opts an endpoint into per-request dynamic schema shaping.

    Usage::

        @api.get("/users/{id}", response=UserSchema)
        @dynamic_response
        def get_user(request, id: int):
            return User.objects.filter(pk=id)

    With an explicit allowlist (overrides what's auto-detected from a
    ``DynamicSchema``)::

        @dynamic_response(includable=["posts"])

    Args:
        config: Override the DynamicConfig (otherwise resolved from API).
        includable: Explicit list of opt-in field names. Defaults to the
            response schema's ``__dynamic_meta__.includable`` when it's a
            DynamicSchema.
        optimize_queryset: When True (default), attach select_related /
            prefetch_related to QuerySet results based on the request's
            ``?include=``.
    """
    if inspect.isfunction(func_or_none) or inspect.iscoroutinefunction(func_or_none):
        return _inject_dynamic(func_or_none)

    if func_or_none is not NOT_SET:
        raise ConfigError(
            "dynamic_response only accepts keyword arguments when called as a "
            "factory (e.g. @dynamic_response(includable=[...]))."
        )

    def wrapper(func: Callable[..., Any]) -> Callable[..., Any]:
        return _inject_dynamic(
            func,
            config=config,
            includable=includable,
            optimize_queryset=optimize_queryset,
        )

    return wrapper
