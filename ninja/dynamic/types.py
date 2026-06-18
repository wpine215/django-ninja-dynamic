from typing import Any, TypeVar

T = TypeVar("T")


class _DynamicMarker:
    """
    Base class for Includable / Expandable annotation markers.

    Used as `Includable[List[PostSchema]]` in schema field annotations so
    DynamicSchema can auto-detect which fields are includable / expandable
    via class-creation introspection.
    """

    kind: str = ""

    def __class_getitem__(cls, item: Any) -> Any:
        return _Marked(cls.kind, item)


class _Marked:
    __slots__ = ("kind", "inner")

    def __init__(self, kind: str, inner: Any):
        self.kind = kind
        self.inner = inner

    def __repr__(self) -> str:
        return f"{self.kind.title()}[{self.inner!r}]"


class Includable(_DynamicMarker):
    """
    Marker: annotate a field that is opt-in via `?include=field_name`.

    The field type T is unwrapped at metaclass time, so Pydantic still
    sees the underlying type. Example::

        class UserSchema(DynamicSchema):
            posts: Includable[List[PostSchema]] = None
    """

    kind = "includable"


class Expandable(_DynamicMarker):
    """
    Marker: annotate a field that is opt-in via `?expand=path`.

    Differs from Includable in intent: expand is for deep dot-path expansion
    on already-included relations (e.g. `?expand=posts.author`). Example::

        class PostSchema(DynamicSchema):
            author: Expandable[AuthorSchema] = None
    """

    kind = "expandable"


def unwrap_marker(annotation: Any) -> "tuple[Any, str | None]":
    """
    If ``annotation`` is a Marked annotation, return ``(inner_type, kind)``.

    Otherwise return ``(annotation, None)``. Used by DynamicSchema's metaclass
    to strip markers before handing annotations to Pydantic.
    """
    if isinstance(annotation, _Marked):
        return annotation.inner, annotation.kind
    return annotation, None
