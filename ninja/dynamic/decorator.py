import inspect
from functools import partial, wraps
from typing import Any, Callable, List, Optional

from django.http import HttpRequest
from pydantic import Field, create_model

from ninja import Query, Schema
from ninja.constants import NOT_SET
from ninja.dynamic.config import DEFAULT_CONFIG, DynamicConfig
from ninja.dynamic.openapi import build_openapi_parameters
from ninja.dynamic.parser import parse_query
from ninja.dynamic.queryset import apply_query_optimization
from ninja.dynamic.schema import (
    get_dynamic_meta,
    unwrap_response_annotation,
)
from ninja.dynamic.selector import FieldSelector
from ninja.errors import ConfigError, ValidationError
from ninja.operation import Operation
from ninja.utils import (
    contribute_operation_args,
    contribute_operation_callback,
    is_async_callable,
)


def _make_input_model(config: DynamicConfig) -> type:
    """
    Build a transient pydantic model for the four dynamic query params.

    The fields are hidden from OpenAPI via ``json_schema_extra={"include_in_schema": False}``
    — django-ninja's ``_extract_parameters`` reads that key and skips the
    field. We render the dynamic params ourselves via
    ``get_dynamic_openapi_parameters`` so they reflect the bound API's config.
    """
    hidden = {"include_in_schema": False}
    fields: dict = {
        config.fields_param: (Optional[str], Field(None, json_schema_extra=hidden)),
        config.omit_param: (Optional[str], Field(None, json_schema_extra=hidden)),
        config.include_param: (Optional[str], Field(None, json_schema_extra=hidden)),
        config.expand_param: (Optional[str], Field(None, json_schema_extra=hidden)),
    }
    return create_model("NinjaDynamicInput", **fields)


def _resolve_config(
    decorator_config: Optional[DynamicConfig], op: Optional[Operation]
) -> DynamicConfig:
    """
    Decorator arg → NinjaAPI → DEFAULT_CONFIG.

    Router-level config is not consulted because the framework does not
    keep a back-reference from Operation to its source Router; users who
    want a router-level override should construct a ``RouterDynamic`` (which
    delegates) or pass ``config=`` to the decorator directly.
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
    expandable: Optional[List[str]],
    config: DynamicConfig,
) -> None:
    """
    Reject ``?include=`` / ``?expand=`` values that aren't declared. Sparse
    fields are validated against the schema's actual model_fields.
    """
    meta = get_dynamic_meta(schema)
    allowed_include = set(includable) if includable is not None else (
        set(meta.includable) if meta else set()
    )
    allowed_expand_paths = set(
        tuple(p.split(".")) for p in expandable
    ) if expandable is not None else None

    bad = sorted(selector.includes - allowed_include)
    if bad:
        raise ValidationError([{
            "type": "value_error",
            "loc": ("query", config.include_param),
            "msg": f"Unknown include value(s): {bad}. Allowed: {sorted(allowed_include)}.",
        }])

    if allowed_expand_paths is not None:
        bad_expand = sorted(".".join(p) for p in (selector.expands - allowed_expand_paths))
        if bad_expand:
            raise ValidationError([{
                "type": "value_error",
                "loc": ("query", config.expand_param),
                "msg": f"Unknown expand value(s): {bad_expand}.",
            }])

    if config.strict_unknown and selector.sparse:
        from ninja.dynamic.openapi import walk_schema_graph
        from ninja.dynamic.selector import _alias_to_name, schema_resource_name

        schema_fields, _, _ = walk_schema_graph(schema)
        fields_by_resource = {
            schema_resource_name(s): set(fields) | set(_alias_to_name(s).keys())
            for s, fields in schema_fields.items()
        }

        for resource, bucket in selector.sparse.items():
            if resource is None:
                # Root schema: accept field names and aliases interchangeably.
                allowed = set(_alias_to_name(schema).keys())
            else:
                allowed = fields_by_resource.get(resource)
                if allowed is None:
                    raise ValidationError([{
                        "type": "value_error",
                        "loc": ("query", f"{config.fields_param}[{resource}]"),
                        "msg": f"Unknown resource: {resource}. Available: {sorted(fields_by_resource)}.",
                    }])
            unknown = sorted(bucket - allowed)
            if unknown:
                raise ValidationError([{
                    "type": "value_error",
                    "loc": ("query", config.fields_param),
                    "msg": f"Unknown field(s) for {resource or 'response'}: {unknown}. Available: {sorted(allowed)}.",
                }])


_VIEW_STATE_ATTR = "_ninja_dynamic_state"


class _DynamicState:
    """
    State stashed on the wrapped view (shared across all clones of the
    Operation, since clones inherit ``view_func``).
    """

    __slots__ = ("decorator_config", "response_schema", "includable", "expandable")

    def __init__(self, decorator_config, response_schema, includable, expandable):
        self.decorator_config = decorator_config
        self.response_schema = response_schema
        self.includable = includable
        self.expandable = expandable


def _modify_operation_for_dynamic(
    decorator_config: Optional[DynamicConfig],
    includable: Optional[List[str]],
    expandable: Optional[List[str]],
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
    # (e.g. @paginate) may have wrapped us. The dynamic wrapped view reads
    # the state from itself; setting it on the outermost view_func would
    # leave the inner wrapper blind.
    setattr(
        target_view,
        _VIEW_STATE_ATTR,
        _DynamicState(decorator_config, schema, includable, expandable),
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
    Called by the OpenAPI renderer for operations marked as dynamic. Resolves
    the effective config from the bound API and returns parameter dicts.
    """
    state = get_dynamic_state(op)
    if state is None:
        return []
    config = _resolve_config(state.decorator_config, op)
    return build_openapi_parameters(state.response_schema, config)


def _build_selector(
    request: HttpRequest, config: DynamicConfig
) -> FieldSelector:
    return parse_query(request.GET, config)


def _inject_dynamic(
    func: Callable[..., Any],
    *,
    config: Optional[DynamicConfig] = None,
    includable: Optional[List[str]] = None,
    expandable: Optional[List[str]] = None,
    optimize_queryset: bool = True,
) -> Callable[..., Any]:
    if getattr(func, "_ninja_is_dynamic", False):
        return func

    effective_config = config if config is not None else DEFAULT_CONFIG
    DynamicInput = _make_input_model(effective_config)

    def _attach_selector_and_optimize(
        request: HttpRequest, schema, sel: FieldSelector
    ) -> None:
        # validation happens inside; raises ValidationError on bad input
        _validate_input_against_meta(
            sel, schema, includable, expandable, _attach_selector_and_optimize._cfg
        )
        request._ninja_dynamic_selector = sel  # type: ignore[attr-defined]
        request._ninja_dynamic_response_schema = schema  # type: ignore[attr-defined]

    # cfg is mutated later (resolved at operation-construction time) — store
    # a default here for early calls before the callback runs.
    _attach_selector_and_optimize._cfg = effective_config  # type: ignore[attr-defined]

    def _live_config(state, request: HttpRequest) -> DynamicConfig:
        # Decorator-level config always wins; otherwise consult the bound
        # operation's api (via the request stash set in Operation.run).
        decorator_cfg = state.decorator_config if state is not None else config
        op = getattr(request, "_ninja_operation", None)
        return _resolve_config(decorator_cfg, op)

    if is_async_callable(func):
        @wraps(func)
        async def view_with_dynamic(request: HttpRequest, **kwargs: Any) -> Any:
            kwargs.pop("ninja_dynamic", None)
            state = getattr(view_with_dynamic, _VIEW_STATE_ATTR, None)
            schema = state.response_schema if state is not None else None
            cfg = _live_config(state, request)
            _attach_selector_and_optimize._cfg = cfg
            sel = parse_query(request.GET, cfg)
            if schema is not None:
                _attach_selector_and_optimize(request, schema, sel)
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
            _attach_selector_and_optimize._cfg = cfg
            sel = parse_query(request.GET, cfg)
            if schema is not None:
                _attach_selector_and_optimize(request, schema, sel)
            result = func(request, **kwargs)
            if optimize_queryset and schema is not None:
                result = apply_query_optimization(result, sel, schema)
            return result

    contribute_operation_args(
        view_with_dynamic, "ninja_dynamic", DynamicInput, Query(None)
    )

    def _callback(op: Operation) -> None:
        _modify_operation_for_dynamic(
            config, includable, expandable, op, target_view=view_with_dynamic
        )

    contribute_operation_callback(view_with_dynamic, _callback)

    view_with_dynamic._ninja_is_dynamic = True  # type: ignore[attr-defined]
    return view_with_dynamic


def dynamic_response(
    func_or_none: Any = NOT_SET,
    *,
    config: Optional[DynamicConfig] = None,
    includable: Optional[List[str]] = None,
    expandable: Optional[List[str]] = None,
    optimize_queryset: bool = True,
) -> Any:
    """
    Decorator that opts an endpoint into per-request dynamic schema shaping.

    Usage::

        @api.get("/users/{id}", response=UserSchema)
        @dynamic_response
        def get_user(request, id: int):
            return User.objects.filter(pk=id)

    With explicit lists (overrides what's auto-detected from a DynamicSchema)::

        @dynamic_response(includable=["posts"], expandable=["posts.author"])

    Args:
        config: Override the DynamicConfig (otherwise resolved from Router/API).
        includable: Explicit list of opt-in relations. Defaults to the response
            schema's ``__dynamic_meta__.includable`` when it's a DynamicSchema.
        expandable: Explicit list of dot-paths. Defaults to the response schema's
            ``__dynamic_meta__.expandable``.
        optimize_queryset: When True (default), attach select_related /
            prefetch_related to QuerySet results based on the request's
            include/expand.
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
            expandable=expandable,
            optimize_queryset=optimize_queryset,
        )

    return wrapper
