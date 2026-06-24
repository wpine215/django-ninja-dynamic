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

    * ``sparse`` — ``?fields=`` selection, keyed by resource name (``None``
      for flat-style / unscoped). The unscoped bucket applies only at the
      root; per-resource buckets apply wherever a matching schema appears.
    * ``includes`` — dot-paths requested via ``?include=``. A single-segment
      path like ``("posts",)`` opts in the top-level ``posts`` field; a
      multi-segment path like ``("posts", "author")`` also descends into
      ``author`` on each post.
    """

    sparse: Dict[Optional[str], Set[str]] = field(default_factory=dict)
    includes: Set[Tuple[str, ...]] = field(default_factory=set)

    def fields_for(
        self, schema: Type[Schema], is_root: bool = False
    ) -> Optional[FrozenSet[str]]:
        """
        Effective sparse-field set for ``schema``. Returns ``None`` if no
        sparse selection applies (i.e. include every default field).

        * Per-resource (JSON:API) buckets apply wherever the schema's resource
          name matches.
        * The unscoped (``None``) bucket — populated by ``?fields=`` in flat
          style — applies *only* at the root, not recursively.
        """
        if not self.sparse:
            return None
        resource = schema_resource_name(schema)
        if resource in self.sparse:
            return frozenset(self.sparse[resource])
        if is_root and None in self.sparse:
            return frozenset(self.sparse[None])
        return None

    def child_paths_at(self, parent_path: Tuple[str, ...]) -> Set[str]:
        """
        Direct children of ``parent_path`` requested via ``?include=``.

        For ``includes={("posts",), ("posts", "author")}``:

        * ``parent_path=()`` -> ``{"posts"}``
        * ``parent_path=("posts",)`` -> ``{"author"}``
        """
        out: Set[str] = set()
        prefix_len = len(parent_path)
        for path in self.includes:
            if len(path) > prefix_len and path[:prefix_len] == parent_path:
                out.add(path[prefix_len])
        return out

    def has_deeper_path_through(self, parent_path: Tuple[str, ...]) -> bool:
        prefix_len = len(parent_path)
        return any(
            len(path) > prefix_len and path[:prefix_len] == parent_path
            for path in self.includes
        )


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
    Wrap an inner Pydantic include dict at a path. Each path segment is
    ``(field_name, is_list_at_segment)``: when ``is_list`` the wrapper uses
    ``{"__all__": ...}``.

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
    Map a user-supplied set of names (field names or aliases) to canonical
    field names. Unknown entries pass through unchanged so upstream
    validation can flag them.
    """
    mapping = _alias_to_name(schema)
    return frozenset(mapping.get(n, n) for n in names)


def _includable_for(schema: Type[Schema]) -> FrozenSet[str]:
    """The set of field names marked ``Includable[T]`` on ``schema``."""
    from ninja.dynamic.schema import get_dynamic_meta

    meta = get_dynamic_meta(schema)
    return frozenset(meta.includable) if meta else frozenset()


def _build_include_inner(
    selector: FieldSelector,
    schema: Type[Schema],
    parent_path: Tuple[str, ...] = (),
    _visited: Tuple[Type[Schema], ...] = (),
) -> Optional[Dict[str, Any]]:
    """
    Compute the per-schema Pydantic include spec for one nesting level,
    without any envelope wrapping. Returns ``None`` when no filtering is
    needed at this level *or* anywhere below — caller may then skip
    ``include=`` entirely.

    The contract this enforces:

    * Default-visible (non-marker) fields are always present unless a
      ``?fields=`` sparse filter excludes them.
    * ``Includable[T]`` fields are absent unless the matching ``?include=``
      path requests them.
    * ``?fields=`` and ``?include=`` are orthogonal: sparse cannot bring in
      includable fields. The decorator's validator rejects that at 422,
      but as a safety net any includable accidentally listed in sparse is
      dropped here.
    """
    is_root = not parent_path
    includable = _includable_for(schema)
    all_field_names = set(schema.model_fields.keys())
    plain_fields = all_field_names - includable

    # Cycle guard: if we've already descended through ``schema`` higher up
    # the stack AND no further user-supplied dot-path goes through this
    # point, terminate with a one-level spec that includes only the
    # default-visible fields (so ``Includable`` fields at the cycle
    # boundary still get filtered out, but we don't recurse forever).
    if schema in _visited and not selector.has_deeper_path_through(parent_path):
        return {name: True for name in plain_fields}

    fields_set = selector.fields_for(schema, is_root=is_root)
    if fields_set is not None:
        fields_set = _normalize_to_field_names(fields_set, schema)
    has_sparse = fields_set is not None

    requested_at_this_level = selector.child_paths_at(parent_path)
    requested_includables_here = requested_at_this_level & includable

    # Default-visible plain fields that aren't in the sparse allowlist but
    # have an ``?include=`` dot-path going through them must still be kept,
    # otherwise sparse silently swallows the include (e.g. ``?fields=name
    # &include=group.organization`` would drop ``group`` and never reach
    # ``organization``).
    default_with_deeper_path: Set[str] = {
        name for name in (requested_at_this_level & plain_fields)
        if selector.has_deeper_path_through(parent_path + (name,))
    }

    if has_sparse:
        sparse_plain = fields_set & plain_fields
        effective: Set[str] = (
            sparse_plain | requested_includables_here | default_with_deeper_path
        )
    else:
        effective = plain_fields | requested_includables_here

    # Decide which fields to recurse into:
    #   * deeper dot-path requested through this field, OR
    #   * the nested schema has its own ``Includable`` fields to filter, OR
    #   * a JSON:API per-resource sparse bucket targets the nested resource.
    nested_to_descend: Set[str] = set()
    for fname in effective:
        if fname not in schema.model_fields:
            continue
        nested_schema = _resolve_field_schema(schema.model_fields[fname].annotation)
        if nested_schema is None:
            continue
        sub_path = parent_path + (fname,)
        if selector.has_deeper_path_through(sub_path):
            nested_to_descend.add(fname)
            continue
        if _includable_for(nested_schema):
            nested_to_descend.add(fname)
            continue
        nested_resource = schema_resource_name(nested_schema)
        if nested_resource in selector.sparse:
            nested_to_descend.add(fname)

    inner: Dict[str, Any] = {}
    for fname in effective:
        if fname not in schema.model_fields:
            continue
        if fname not in nested_to_descend:
            inner[fname] = True
            continue
        nested_schema = _resolve_field_schema(schema.model_fields[fname].annotation)
        nested_is_list = _is_list_annotation(schema.model_fields[fname].annotation)
        nested_inner = _build_include_inner(
            selector,
            nested_schema,
            parent_path=parent_path + (fname,),
            _visited=_visited + (schema,),
        )
        if nested_inner is None:
            inner[fname] = True
        elif nested_is_list:
            inner[fname] = {"__all__": nested_inner}
        else:
            inner[fname] = nested_inner

    if not has_sparse and not includable and not nested_to_descend:
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
    filtering is needed at any level.
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
