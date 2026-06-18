from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Optional, Set, Tuple, Type

from ninja.schema import Schema


def schema_resource_name(schema: Type[Schema]) -> str:
    """
    Derive a JSON:API-style resource name from a schema class.

    ``UserSchema``  -> ``"user"``
    ``BlogPost``    -> ``"blog_post"``
    """
    name = schema.__name__
    if name.endswith("Schema"):
        name = name[: -len("Schema")]
    out = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0 and not name[i - 1].isupper():
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


@dataclass
class FieldSelector:
    """
    Parsed state of dynamic-shape query parameters for a single request.

    Resource keys: ``None`` for flat-style selection that applies to whichever
    schema is at hand; a string resource-name for JSON:API style (e.g.
    ``fields[user]=name``).
    """

    sparse: Dict[Optional[str], Set[str]] = field(default_factory=dict)
    omit: Dict[Optional[str], Set[str]] = field(default_factory=dict)
    includes: Set[str] = field(default_factory=set)
    expands: Set[Tuple[str, ...]] = field(default_factory=set)

    @property
    def is_empty(self) -> bool:
        return not (self.sparse or self.omit or self.includes or self.expands)

    def fields_for(
        self, schema: Type[Schema], is_root: bool = False
    ) -> Optional[FrozenSet[str]]:
        """
        Effective sparse-field set for ``schema``. Returns ``None`` if no
        sparse selection applies (i.e. include every default field).

        * Per-resource (JSON:API) buckets apply wherever the schema's resource
          name matches.
        * The unscoped (``None``) bucket — populated by ``?fields=...`` in
          flat style — applies *only* at the root, not recursively.
        """
        if not self.sparse:
            return None
        resource = schema_resource_name(schema)
        if resource in self.sparse:
            return frozenset(self.sparse[resource])
        if is_root and None in self.sparse:
            return frozenset(self.sparse[None])
        return None

    def omit_for(
        self, schema: Type[Schema], is_root: bool = False
    ) -> FrozenSet[str]:
        if not self.omit:
            return frozenset()
        resource = schema_resource_name(schema)
        if resource in self.omit:
            return frozenset(self.omit[resource])
        if is_root and None in self.omit:
            return frozenset(self.omit[None])
        return frozenset()

    def expand_paths_for(self, parent_path: Tuple[str, ...]) -> Set[str]:
        """
        Direct children of ``parent_path`` reached by an ``expand=`` request.

        For ``expands={("posts", "author")}`` and parent_path=("posts",),
        returns ``{"author"}``.
        """
        out: Set[str] = set()
        for path in self.expands:
            if len(path) > len(parent_path) and path[: len(parent_path)] == parent_path:
                out.add(path[len(parent_path)])
        return out


def _wrap_in_response_envelope(
    inner: Optional[Dict[str, Any]], is_list: bool
) -> Optional[Dict[str, Any]]:
    """
    Wrap a selection dict in the ``{"response": ...}`` envelope used by
    ``NinjaResponseSchema`` (see ``Operation._create_response_model``).
    """
    if inner is None:
        return None
    if is_list:
        return {"response": {"__all__": inner}}
    return {"response": inner}


def _wrap_at_path(
    inner: Optional[Dict[str, Any]],
    path: Tuple[Tuple[str, bool], ...],
    sibling_fields: Optional[Tuple[Tuple[str, ...], ...]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Wrap an inner Pydantic include/exclude dict at a path. Each path segment
    is ``(field_name, is_list_at_segment)``: when ``is_list`` the wrapper
    uses ``{"__all__": ...}``.

    ``sibling_fields[i]`` lists the sibling fields at depth ``i`` of the
    path (excluding the path step itself). When provided, those siblings
    are included as ``True`` alongside the wrapping field so e.g. paged
    wrappers (count/next/previous) are not filtered out by an include
    spec that only targets ``items``. Returns ``None`` if ``inner`` is
    ``None``.
    """
    if inner is None:
        return None
    result: Any = inner
    siblings = sibling_fields or ()
    n = len(path)
    for i in range(n - 1, -1, -1):
        fname, is_list = path[i]
        layer: Dict[str, Any] = {}
        if is_list:
            layer[fname] = {"__all__": result}
        else:
            layer[fname] = result
        if i < len(siblings):
            for sib in siblings[i]:
                if sib != fname:
                    layer[sib] = True
        result = layer
    return result


def _siblings_along_path(
    envelope_annotation: Any,
    path: Tuple[Tuple[str, bool], ...],
) -> Tuple[Tuple[str, ...], ...]:
    """
    For each level of ``path``, return the field names of the schema at
    that level so ``_wrap_at_path`` can preserve sibling fields. The first
    path segment is always ``("response", ...)`` against the
    ``NinjaResponseSchema`` envelope (which has no other siblings), so
    that level contributes an empty tuple. Subsequent levels walk into
    ``envelope_annotation``.
    """
    out: list = [()]  # the "response" envelope has no siblings
    current = envelope_annotation
    for fname, is_list in path[1:]:
        schema_here: Optional[Type[Schema]] = None
        if isinstance(current, type) and issubclass(current, Schema):
            schema_here = current
        if schema_here is not None:
            out.append(tuple(schema_here.model_fields.keys()))
        else:
            out.append(())
        if schema_here is not None and fname in schema_here.model_fields:
            sub_ann = schema_here.model_fields[fname].annotation
            if is_list:
                current = _resolve_field_schema(sub_ann)
            else:
                current = sub_ann
        else:
            current = None
    return tuple(out)


def find_schema_location(
    envelope_annotation: Any,
    target: Type[Schema],
) -> Optional[Tuple[Tuple[str, bool], ...]]:
    """
    Walk a response-envelope annotation to find where ``target`` (the
    captured response schema) lives. Returns the path as a tuple of
    ``(field_name, is_list)`` segments, or ``None`` if ``target`` is not
    reachable inside the envelope. The path is rooted at the
    ``NinjaResponseSchema.response`` field, e.g.:

    * ``response=UserSchema`` -> ``(("response", False),)``
    * ``response=List[UserSchema]`` -> ``(("response", True),)``
    * ``response=PagedUser`` where ``PagedUser.items: List[UserSchema]``
      -> ``(("response", False), ("items", True))``
    * ``response=ErrorSchema`` (captured = UserSchema) -> ``None``
    """

    def _walk(ann: Any) -> Optional[Tuple[Tuple[str, bool], ...]]:
        if isinstance(ann, type) and issubclass(ann, Schema) and ann is target:
            return ()
        if _is_list_annotation(ann):
            inner = _resolve_field_schema(ann)
            if inner is target:
                return ()
            return None
        if isinstance(ann, type) and issubclass(ann, Schema):
            for fname, fld in ann.model_fields.items():
                sub_ann = fld.annotation
                sub_is_list = _is_list_annotation(sub_ann)
                sub_schema = _resolve_field_schema(sub_ann)
                if sub_schema is target:
                    return ((fname, sub_is_list),)
                sub = _walk(sub_ann)
                if sub is not None:
                    return ((fname, sub_is_list),) + sub
        return None

    head_result = _walk(envelope_annotation)
    if head_result is None:
        return None
    # Compute is_list at the "response" level itself.
    response_is_list = _is_list_annotation(envelope_annotation)
    return (("response", response_is_list),) + head_result


def _resolve_field_schema(annotation: Any) -> Optional[Type[Schema]]:
    """
    If a field annotation wraps a nested Schema (directly or via List[...] /
    Optional[...]), return the underlying Schema class. Otherwise None.
    """
    from typing import get_args, get_origin

    if isinstance(annotation, type) and issubclass(annotation, Schema):
        return annotation

    origin = get_origin(annotation)
    if origin is None:
        return None
    for arg in get_args(annotation):
        nested = _resolve_field_schema(arg)
        if nested is not None:
            return nested
    return None


def _is_list_annotation(annotation: Any) -> bool:
    from typing import get_args, get_origin

    if get_origin(annotation) is list:
        return True
    for arg in get_args(annotation) or ():
        if _is_list_annotation(arg):
            return True
    return False


def _alias_to_name(schema: Type[Schema]) -> Dict[str, str]:
    """Map field aliases to their field names (identity for unaliased fields)."""
    out: Dict[str, str] = {}
    for name, fld in schema.model_fields.items():
        out[name] = name
        if fld.alias and fld.alias != name:
            out[fld.alias] = name
    return out


def _normalize_to_field_names(
    names: FrozenSet[str], schema: Type[Schema]
) -> FrozenSet[str]:
    """
    Map a user-supplied set of names (which may be field names or aliases)
    to canonical field names. Unknown entries pass through unchanged so
    upstream validation can flag them.
    """
    mapping = _alias_to_name(schema)
    return frozenset(mapping.get(n, n) for n in names)


def _build_include_inner(
    selector: FieldSelector,
    schema: Type[Schema],
    parent_path: Tuple[str, ...] = (),
) -> Optional[Dict[str, Any]]:
    """
    Compute the per-schema Pydantic include spec (without any envelope
    wrapping). Returns ``None`` when no sparse / expand selection applies
    at this schema level.
    """
    is_root = not parent_path
    fields_set = selector.fields_for(schema, is_root=is_root)
    if fields_set is not None:
        fields_set = _normalize_to_field_names(fields_set, schema)
    has_sparse = fields_set is not None
    expansions_here = selector.expand_paths_for(parent_path)

    nested_to_descend: Set[str] = set()
    for fname, fld in schema.model_fields.items():
        if fname in expansions_here:
            nested_to_descend.add(fname)
            continue
        nested_schema = _resolve_field_schema(fld.annotation)
        if nested_schema is not None:
            nested_resource = schema_resource_name(nested_schema)
            if nested_resource in selector.sparse or nested_resource in selector.omit:
                nested_to_descend.add(fname)

    if has_sparse:
        effective: Set[str] = set(fields_set) | nested_to_descend
    else:
        effective = set()

    inner: Dict[str, Any] = {}
    for fname, fld in schema.model_fields.items():
        if has_sparse and fname not in effective:
            continue
        nested_schema = _resolve_field_schema(fld.annotation)
        if nested_schema is None or fname not in nested_to_descend:
            inner[fname] = True
            continue

        nested_is_list = _is_list_annotation(fld.annotation)
        nested_include = _build_include_inner(
            selector, nested_schema, parent_path=parent_path + (fname,)
        )
        if nested_include is None:
            inner[fname] = True
        elif nested_is_list:
            inner[fname] = {"__all__": nested_include}
        else:
            inner[fname] = nested_include

    if not has_sparse and not nested_to_descend:
        return None

    return inner


def build_include(
    selector: FieldSelector,
    schema: Type[Schema],
    is_list_response: bool,
    parent_path: Tuple[str, ...] = (),
) -> Optional[Dict[str, Any]]:
    """
    Build a Pydantic ``model_dump(include=...)`` dict for a response shaped
    by ``selector``. Backwards-compatible wrapper that places the inner
    spec at the ``response`` envelope field. Returns ``None`` when no
    sparse selection is active at any level — caller should then skip
    ``include=`` entirely.
    """
    inner = _build_include_inner(selector, schema, parent_path=parent_path)
    if inner is None:
        return None
    if parent_path:
        return inner
    return _wrap_in_response_envelope(inner, is_list_response)


def build_include_at_path(
    selector: FieldSelector,
    schema: Type[Schema],
    path: Tuple[Tuple[str, bool], ...],
    envelope_annotation: Any = None,
) -> Optional[Dict[str, Any]]:
    """
    Build a Pydantic ``model_dump(include=...)`` dict, wrapping at an
    arbitrary path through nested response envelopes (e.g. pagination's
    ``Paged.items`` wrapper). When ``envelope_annotation`` is provided,
    sibling fields of each wrapper level are preserved (so e.g. paged
    metadata like ``count`` and ``next`` don't get dropped along with the
    item filter). See ``find_schema_location``.
    """
    inner = _build_include_inner(selector, schema)
    if inner is None:
        return None
    siblings = (
        _siblings_along_path(envelope_annotation, path)
        if envelope_annotation is not None
        else None
    )
    return _wrap_at_path(inner, path, siblings)


def _build_exclude_inner(
    selector: FieldSelector,
    schema: Type[Schema],
) -> Optional[Dict[str, Any]]:
    omit_set = selector.omit_for(schema, is_root=True)
    if not omit_set:
        return None
    canonical = _normalize_to_field_names(omit_set, schema)
    inner = {name: True for name in canonical if name in schema.model_fields}
    return inner or None


def build_exclude(
    selector: FieldSelector,
    schema: Type[Schema],
    is_list_response: bool,
) -> Optional[Dict[str, Any]]:
    """
    Build a Pydantic ``model_dump(exclude=...)`` dict for the top-level omit
    set on ``schema``. Backwards-compatible wrapper that places the inner
    spec at the ``response`` envelope. Returns ``None`` when no omit applies.

    Note: only flat top-level omit is honored here. Nested omit (JSON:API
    style ``fields[post]`` paired with omit) is not currently expressed via
    exclude — sparse selection covers that case more cleanly.
    """
    inner = _build_exclude_inner(selector, schema)
    if inner is None:
        return None
    return _wrap_in_response_envelope(inner, is_list_response)


def build_exclude_at_path(
    selector: FieldSelector,
    schema: Type[Schema],
    path: Tuple[Tuple[str, bool], ...],
) -> Optional[Dict[str, Any]]:
    inner = _build_exclude_inner(selector, schema)
    return _wrap_at_path(inner, path)
