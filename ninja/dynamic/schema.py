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

    Built by ``DynamicMetaclass`` from ``Includable[T]`` / ``Expandable[T]``
    annotations and inherited from base classes.
    """

    includable: Set[str]
    expandable: Set[str]

    def __init__(self, includable: Set[str], expandable: Set[str]):
        super().__init__()
        self["includable"] = self.includable = set(includable)
        self["expandable"] = self.expandable = set(expandable)


class DynamicMetaclass(ResolverMetaclass):
    """
    Inspects ``Includable[T]`` / ``Expandable[T]`` annotations on a
    ``DynamicSchema`` subclass, strips the markers so Pydantic sees the
    underlying type (wrapped in ``Optional``), and stashes the discovered
    fields on ``__dynamic_meta__``.
    """

    __dynamic_meta__: DynamicMeta

    @no_type_check
    def __new__(cls, name, bases, namespace, **kwargs):
        annotations = _read_namespace_annotations(namespace)
        local_includable: Set[str] = set()
        local_expandable: Set[str] = set()

        for fname, ann in list(annotations.items()):
            inner, kind = unwrap_marker(ann)
            if kind is None:
                continue
            annotations[fname] = Optional[inner]
            if kind == "includable":
                local_includable.add(fname)
            elif kind == "expandable":
                local_expandable.add(fname)
            namespace.setdefault(fname, None)

        _write_namespace_annotations(namespace, annotations)

        merged_includable: Set[str] = set(local_includable)
        merged_expandable: Set[str] = set(local_expandable)
        for base in bases:
            base_meta = getattr(base, "__dynamic_meta__", None)
            if isinstance(base_meta, DynamicMeta):
                merged_includable |= base_meta.includable
                merged_expandable |= base_meta.expandable

        namespace["__dynamic_meta__"] = DynamicMeta(
            includable=merged_includable, expandable=merged_expandable
        )
        return super().__new__(cls, name, bases, namespace, **kwargs)


class DynamicSchema(Schema, metaclass=DynamicMetaclass):
    """
    Schema base class that auto-detects ``Includable[T]`` / ``Expandable[T]``
    annotated fields, exposes them via ``__dynamic_meta__``, and is recognized
    by ``@dynamic_response`` and the ``RouterDynamic``-style auto-wiring.

    Example::

        class PostSchema(DynamicSchema):
            id: int
            title: str
            author: Expandable[AuthorSchema] = None

        class UserSchema(DynamicSchema):
            id: int
            name: str
            posts: Includable[List[PostSchema]] = None
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

    Also looks one level inside a wrapper Schema that contains a single
    ``List[InnerSchema]`` field — this is the shape pagination's
    ``make_response_paginated`` produces (``Paged{Foo}.items``). Recognizing
    it lets ``@dynamic_response`` capture the inner item schema regardless of
    decorator stacking order.
    """
    from typing import get_args, get_origin

    if isinstance(annotation, type) and issubclass(annotation, Schema):
        # If this Schema looks like a pagination-style wrapper (has a
        # List[Schema] field), recurse into that field to find the item
        # schema. We only recurse when the wrapper isn't itself a user-
        # declared response schema — heuristic: a single List[Schema] field
        # alongside other scalar metadata fields (count, next, previous, ...).
        list_fields = []
        for fname, fld in annotation.model_fields.items():
            sub_ann = fld.annotation
            if get_origin(sub_ann) is list:
                args = get_args(sub_ann)
                if args and isinstance(args[0], type) and issubclass(args[0], Schema):
                    list_fields.append((fname, args[0]))
        if len(list_fields) == 1:
            _, inner = list_fields[0]
            return inner, True
        return annotation, False

    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (list, Union):
        for arg in args:
            inner, _is_list = unwrap_response_annotation(arg)
            if inner is not None:
                return inner, origin is list or _is_list
    return None, False
