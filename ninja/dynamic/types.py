from typing import Any, TypeVar

T = TypeVar("T")


class _DynamicMarker:
    """
    Base class for the ``Includable`` annotation marker.

    Used as ``Includable[List[PostSchema]]`` in schema field annotations so
    ``DynamicSchema`` can auto-detect which fields are opt-in via
    class-creation introspection.
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
    Marker: annotate a field that is opt-in via ``?include=field_name``.

    The field is hidden from the default response. The client opts it in
    with ``?include=field_name``; dot-paths request deep inclusion
    (``?include=posts.author`` brings in ``posts`` and descends into
    ``author`` on each item).

    The field type ``T`` is rewritten by ``DynamicSchema``'s metaclass as
    ``Optional[T]`` with a default of ``None``, so no explicit default is
    needed. Example::

        class UserSchema(DynamicSchema):
            id: int
            name: str
            posts: Includable[List[PostSchema]]
    """

    kind = "includable"


def unwrap_marker(annotation: Any) -> "tuple[Any, str | None]":
    """
    If ``annotation`` is a ``_Marked`` annotation, return ``(inner_type, kind)``.

    Otherwise return ``(annotation, None)``. Used by ``DynamicSchema``'s
    metaclass to strip markers before handing annotations to Pydantic.
    """
    if isinstance(annotation, _Marked):
        return annotation.inner, annotation.kind
    return annotation, None
