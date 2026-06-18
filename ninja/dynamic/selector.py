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


def build_include(
    selector: FieldSelector,
    schema: Type[Schema],
    is_list_response: bool,
    parent_path: Tuple[str, ...] = (),
) -> Optional[Dict[str, Any]]:
    """
    Build a Pydantic ``model_dump(include=...)`` dict for a response shaped
    by ``selector``. Returns ``None`` when no sparse selection is active at
    any level — caller should then skip ``include=`` entirely.

    Recursion into a nested Schema field is triggered by any of:
      * the field is on a request-supplied expand path
      * the field's resource has its own per-resource sparse bucket
        (JSON:API ``fields[post]=...`` while we're emitting ``post`` items)
    """
    is_root = not parent_path
    fields_set = selector.fields_for(schema, is_root=is_root)
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
        nested_include = build_include(
            selector,
            nested_schema,
            is_list_response=False,
            parent_path=parent_path + (fname,),
        )
        if nested_include is None:
            inner[fname] = True
        elif nested_is_list:
            inner[fname] = {"__all__": nested_include}
        else:
            inner[fname] = nested_include

    if not has_sparse and not nested_to_descend:
        return None

    if parent_path:
        return inner
    return _wrap_in_response_envelope(inner, is_list_response)


def build_exclude(
    selector: FieldSelector,
    schema: Type[Schema],
    is_list_response: bool,
) -> Optional[Dict[str, Any]]:
    """
    Build a Pydantic ``model_dump(exclude=...)`` dict for the top-level omit
    set on ``schema``. Returns ``None`` when no omit is active.

    Note: only flat top-level omit is honored here. Nested omit (JSON:API
    style ``fields[post]`` paired with omit) is not currently expressed via
    exclude — sparse selection covers that case more cleanly.
    """
    omit_set = selector.omit_for(schema, is_root=True)
    if not omit_set:
        return None
    inner = {name: True for name in omit_set if name in schema.model_fields}
    if not inner:
        return None
    return _wrap_in_response_envelope(inner, is_list_response)
