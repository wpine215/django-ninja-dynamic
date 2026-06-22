from typing import Any, Dict, List, Set, Tuple, Type

from ninja.dynamic.config import DynamicConfig
from ninja.dynamic.schema import get_dynamic_meta
from ninja.dynamic.selector import _resolve_field_schema, schema_resource_name
from ninja.schema import Schema


def walk_schema_graph(
    root: Type[Schema],
) -> Tuple[
    Dict[Type[Schema], List[str]],
    Dict[Type[Schema], Set[str]],
    Set[Tuple[str, ...]],
]:
    """
    BFS the response schema's nested Schema graph and report:

    * ``schema_fields``    — every reachable schema → its field list
    * ``schema_includable`` — schema → set of declared ``Includable`` names
    * ``includable_paths`` — every dot-path of an Includable nesting, e.g.
      ``("posts", "author")``
    """
    schema_fields: Dict[Type[Schema], List[str]] = {}
    schema_includable: Dict[Type[Schema], Set[str]] = {}
    includable_paths: Set[Tuple[str, ...]] = set()

    visited: Set[Type[Schema]] = set()
    queue: List[Tuple[Type[Schema], Tuple[str, ...]]] = [(root, ())]

    while queue:
        schema, parent_path = queue.pop(0)
        if schema in visited:
            continue
        visited.add(schema)

        schema_fields[schema] = list(schema.model_fields.keys())
        meta = get_dynamic_meta(schema)
        if meta is not None:
            schema_includable[schema] = set(meta.includable)
            for fname in meta.includable:
                includable_paths.add(parent_path + (fname,))

        for sub_field, sub_schema in _nested_schema_fields(schema):
            new_path = parent_path + (sub_field,)
            queue.append((sub_schema, new_path))

    return schema_fields, schema_includable, includable_paths


def _nested_schema_fields(schema: Type[Schema]):
    for fname, fld in schema.model_fields.items():
        nested = _resolve_field_schema(fld.annotation)
        if nested is not None and nested is not schema:
            yield fname, nested


def _default_visible(schema: Type[Schema]) -> List[str]:
    """Field names on ``schema`` that aren't marked Includable."""
    meta = get_dynamic_meta(schema)
    includable = meta.includable if meta else set()
    return [f for f in schema.model_fields if f not in includable]


def build_openapi_parameters(
    root_schema: Type[Schema], config: DynamicConfig
) -> List[Dict[str, Any]]:
    """
    Build the OpenAPI ``parameters`` list documenting the dynamic query
    params for an operation whose response is shaped from ``root_schema``.

    Two parameters are emitted (when their target schema has anything to
    show):

    * ``?fields=`` — allowlist filter over default-visible fields
    * ``?include=`` — opt-in dot-paths into ``Includable`` fields

    Returns a list of OpenAPI 3.0 parameter objects ready to merge into
    ``operation.openapi_extra["parameters"]``.
    """
    schema_fields, schema_includable, includable_paths = walk_schema_graph(
        root_schema
    )

    out: List[Dict[str, Any]] = []

    if config.style == "flat":
        top_visible = _default_visible(root_schema)
        if config.fields_param and top_visible:
            out.append({
                "in": "query",
                "name": config.fields_param,
                "required": False,
                "schema": {"type": "string", "example": ",".join(top_visible[:2])},
                "description": (
                    "Sparse fieldset: comma-separated allowlist of fields to "
                    "return. Includable fields are not valid here; use "
                    f"'{config.include_param}' for those. "
                    f"Available: {', '.join(top_visible)}."
                ),
            })
    else:
        for schema in schema_fields:
            visible = _default_visible(schema)
            if not visible:
                continue
            resource = schema_resource_name(schema)
            out.append({
                "in": "query",
                "name": f"{config.fields_param}[{resource}]",
                "required": False,
                "style": "form",
                "explode": True,
                "schema": {"type": "string"},
                "description": (
                    f"Sparse fieldset for ``{resource}`` resources. "
                    f"Available: {', '.join(visible)}."
                ),
            })

    sorted_paths = sorted(".".join(p) for p in includable_paths)
    if config.include_param and sorted_paths:
        out.append({
            "in": "query",
            "name": config.include_param,
            "required": False,
            "schema": {"type": "string", "example": sorted_paths[0]},
            "description": (
                "Comma-separated list of opt-in fields to include. "
                "Supports dot-paths (e.g. ``posts.author``) for nested "
                f"inclusion. Available: {', '.join(sorted_paths)}."
            ),
        })

    return out
