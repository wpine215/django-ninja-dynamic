import inspect
from typing import Any, Callable, Optional, Type

from django.http import HttpRequest, HttpResponseForbidden
from django.middleware.csrf import CsrfViewMiddleware

__all__ = [
    "check_csrf",
    "normalize_path",
    "contribute_operation_callback",
]


def replace_path_param_notation(path: str) -> str:
    return path.replace("{", "<").replace("}", ">")


def normalize_path(path: str) -> str:
    while "//" in path:
        path = path.replace("//", "/")
    return path


def _no_view() -> None:
    pass  # pragma: no cover


def check_csrf(
    request: HttpRequest, callback: Callable = _no_view
) -> Optional[HttpResponseForbidden]:
    mware = CsrfViewMiddleware(lambda x: HttpResponseForbidden())  # pragma: no cover
    request.csrf_processing_done = False  # type: ignore
    mware.process_request(request)
    return mware.process_view(request, callback, (), {})


def is_async_callable(f: Callable[..., Any]) -> bool:
    return inspect.iscoroutinefunction(f) or inspect.iscoroutinefunction(
        getattr(f, "__call__", None)
    )


def is_optional_type(t: Type[Any]) -> bool:
    try:
        return type(None) in t.__args__
    except AttributeError:
        return False


def contribute_operation_callback(
    func: Callable[..., Any], callback: Callable[..., Any]
) -> None:
    if not hasattr(func, "_ninja_contribute_to_operation"):
        func._ninja_contribute_to_operation = []  # type: ignore
    func._ninja_contribute_to_operation.append(callback)  # type: ignore


def contribute_operation_args(
    func: Callable[..., Any], arg_name: str, arg_type: Type, arg_source: Any
) -> None:
    if not hasattr(func, "_ninja_contribute_args"):
        func._ninja_contribute_args = []  # type: ignore
    func._ninja_contribute_args.append((arg_name, arg_type, arg_source))  # type: ignore


def collect_contributions(func: Callable[..., Any], attr: str) -> list:
    """
    Walk ``func.__wrapped__`` to accumulate ``_ninja_contribute_*`` lists
    from every decorator in a ``functools.wraps``-preserving chain.

    Without this, stacking two decorators that each call
    ``contribute_operation_args`` / ``contribute_operation_callback`` only
    runs the outermost decorator's contributions: the inner decorator's
    attribute is set on a function the operation never inspects.

    Items are returned outermost-first; callers that need
    innermost-first ordering can reverse the result.
    """
    seen_ids = set()
    result: list = []
    current = func
    while current is not None:
        if id(current) in seen_ids:
            break
        seen_ids.add(id(current))
        items = getattr(current, attr, None)
        if items:
            for item in items:
                result.append(item)
        current = getattr(current, "__wrapped__", None)
    # Dedupe by identity while preserving order — the same list object can
    # appear on multiple wrappers if an inner decorator passed its attribute
    # up by mutating the outer function.
    deduped: list = []
    seen_item_ids = set()
    for item in result:
        if id(item) in seen_item_ids:
            continue
        seen_item_ids.add(id(item))
        deduped.append(item)
    return deduped
