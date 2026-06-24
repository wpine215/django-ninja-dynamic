from typing import Any, Dict, Optional, Set, Type, Union, no_type_check

from ninja.dynamic.types import unwrap_marker
from ninja.schema import ResolverMetaclass, Schema


def _read_namespace_annotations(namespace: Dict[str, Any]) -> Dict[str, Any]:
    """
    Annotations are stored differently across Python versions:
      * <3.14: ``namespace["__annotations__"]`` is a dict.
      * 3.14+ (PEP 649): ``namespace["__annotate_func__"]`` is a callable
        that returns the annotations dict for a given format.
    Return a fresh dict from whichever source is available.
    """
    raw = namespace.get("__annotations__")
    if raw:
        return dict(raw)
    annotate_func = namespace.get("__annotate_func__") or namespace.get("__annotate__")
    if callable(annotate_func):
        try:
            return dict(annotate_func(1))
        except Exception:
            pass
    return {}


def _write_namespace_annotations(
    namespace: Dict[str, Any], annotations: Dict[str, Any]
) -> None:
    """
    Replace the namespace's annotations and, on 3.14+, the matching
    ``__annotate_func__`` so deferred-format requests also return the
    updated mapping.
    """
    namespace["__annotations__"] = annotations
    if "__annotate_func__" in namespace or "__annotate__" in namespace:
        def _annotate_func(_format: int = 1, _ann: Dict[str, Any] = annotations) -> Dict[str, Any]:
            return dict(_ann)

        if "__annotate_func__" in namespace:
            namespace["__annotate_func__"] = _annotate_func
        if "__annotate__" in namespace:
            namespace["__annotate__"] = _annotate_func


class DynamicMeta(Dict[str, Set[str]]):
    """
    Per-schema metadata describing which fields are dynamic.

    Built by ``DynamicMetaclass`` from ``Includable[T]`` annotations and
    inherited from base classes.
    """

    includable: Set[str]

    def __init__(self, includable: Set[str]):
        super().__init__()
        self["includable"] = self.includable = set(includable)


class DynamicMetaclass(ResolverMetaclass):
    """
    Inspects ``Includable[T]`` annotations on a ``DynamicSchema`` subclass,
    strips the marker so Pydantic sees the underlying type (rewritten as
    ``Optional[T]``), injects a default of ``None`` when the user didn't
    supply one, and stashes the discovered field names on
    ``__dynamic_meta__``.
    """

    __dynamic_meta__: DynamicMeta

    @no_type_check
    def __new__(cls, name, bases, namespace, **kwargs):
        annotations = _read_namespace_annotations(namespace)
        local_includable: Set[str] = set()

        for fname, ann in list(annotations.items()):
            inner, kind = unwrap_marker(ann)
            if kind is None:
                continue
            annotations[fname] = Optional[inner]
            if kind == "includable":
                local_includable.add(fname)
            # Inject default=None when not provided. Without this, Pydantic
            # treats the field as required and any response validation that
            # doesn't supply data raises. Marker fields are hidden by
            # default, so a None default is always the right answer; users
            # who need a different default should drop the marker and
            # declare the field plainly.
            namespace.setdefault(fname, None)

        _write_namespace_annotations(namespace, annotations)

        merged_includable: Set[str] = set(local_includable)
        for base in bases:
            base_meta = getattr(base, "__dynamic_meta__", None)
            if isinstance(base_meta, DynamicMeta):
                merged_includable |= base_meta.includable

        namespace["__dynamic_meta__"] = DynamicMeta(includable=merged_includable)
        return super().__new__(cls, name, bases, namespace, **kwargs)


class DynamicSchema(Schema, metaclass=DynamicMetaclass):
    """
    Schema base class that auto-detects ``Includable[T]`` annotated fields,
    exposes them via ``__dynamic_meta__``, and is recognized by
    ``@dynamic_response``.

    Example::

        class PostSchema(DynamicSchema):
            id: int
            title: str

        class UserSchema(DynamicSchema):
            id: int
            name: str
            posts: Includable[List[PostSchema]]

    The ``posts`` field is hidden from default responses; clients opt in
    with ``?include=posts``. No explicit ``= None`` default is needed —
    the metaclass adds it.
    """

    __dynamic_meta__: DynamicMeta


def get_dynamic_meta(schema: Type[Any]) -> Optional[DynamicMeta]:
    """Return ``schema.__dynamic_meta__`` if it's a DynamicSchema, else None."""
    meta = getattr(schema, "__dynamic_meta__", None)
    return meta if isinstance(meta, DynamicMeta) else None


def unwrap_response_annotation(annotation: Any) -> "tuple[Type[Schema] | None, bool]":
    """
    Unwrap ``response=List[UserSchema]`` (or just ``UserSchema``) into
    ``(UserSchema, is_list)``. Returns ``(None, False)`` if there's no Schema
    underneath (e.g. a plain dict response).

    Also unwraps the wrapper Schema that pagination's
    ``make_response_paginated`` produces (``Paged{Foo}.<items_attr>``). The
    wrapper is identified by the ``__ninja_paginated_items_attr__`` marker set
    by ``make_response_paginated`` — *not* by a structural "one list field"
    heuristic, which would false-positive on user schemas like
    ``FeedSchema(id, posts: List[PostSchema])``.
    """
    from typing import get_args, get_origin

    if isinstance(annotation, type) and issubclass(annotation, Schema):
        items_attr = getattr(annotation, "__ninja_paginated_items_attr__", None)
        if items_attr and items_attr in annotation.model_fields:
            sub_ann = annotation.model_fields[items_attr].annotation
            if get_origin(sub_ann) is list:
                args = get_args(sub_ann)
                if args and isinstance(args[0], type) and issubclass(args[0], Schema):
                    return args[0], True
        return annotation, False

    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (list, Union):
        for arg in args:
            inner, _is_list = unwrap_response_annotation(arg)
            if inner is not None:
                return inner, origin is list or _is_list
    return None, False
