from typing import Any, Dict, List, Set, Tuple, Type

from ninja.dynamic.config import DynamicConfig
from ninja.dynamic.schema import get_dynamic_meta
from ninja.dynamic.selector import _resolve_field_schema, schema_resource_name
from ninja.schema import Schema


def walk_schema_graph(
    root: Type[Schema],
) -> Tuple[Dict[Type[Schema], List[str]], Dict[Type[Schema], Set[str]], Set[Tuple[str, ...]]]:
    """
    BFS the response schema's nested Schema graph and report:

    * ``schema_fields``  — every reachable schema → its default field list
    * ``schema_dynamic`` — schema → declared includable/expandable field names
    * ``expand_paths``   — every dot-path of an Expandable nesting, e.g.
      ``("posts", "author")``
    """
    schema_fields: Dict[Type[Schema], List[str]] = {}
    schema_dynamic: Dict[Type[Schema], Set[str]] = {}
    expand_paths: Set[Tuple[str, ...]] = set()

    visited: Set[Type[Schema]] = set()
    queue: List[Tuple[Type[Schema], Tuple[str, ...]]] = [(root, ())]

    while queue:
        schema, parent_path = queue.pop(0)
        if schema in visited:
            for sub_field, sub_schema in _nested_schema_fields(schema):
                new_path = parent_path + (sub_field,)
                meta = get_dynamic_meta(schema)
                if meta and sub_field in meta.expandable:
                    expand_paths.add(new_path)
            continue
        visited.add(schema)

        schema_fields[schema] = list(schema.model_fields.keys())
        meta = get_dynamic_meta(schema)
        if meta is not None:
            schema_dynamic[schema] = meta.includable | meta.expandable

        for sub_field, sub_schema in _nested_schema_fields(schema):
            new_path = parent_path + (sub_field,)
            if meta and sub_field in meta.expandable:
                expand_paths.add(new_path)
            queue.append((sub_schema, new_path))

    return schema_fields, schema_dynamic, expand_paths


def _nested_schema_fields(schema: Type[Schema]):
    for fname, fld in schema.model_fields.items():
        nested = _resolve_field_schema(fld.annotation)
        if nested is not None and nested is not schema:
            yield fname, nested


def build_openapi_parameters(
    root_schema: Type[Schema], config: DynamicConfig
) -> List[Dict[str, Any]]:
    """
    Build the OpenAPI ``parameters`` list documenting the four dynamic-shape
    query params for an operation whose response is shaped from ``root_schema``.

    Returns a list of OpenAPI 3.0 parameter objects ready to merge into
    ``operation.openapi_extra["parameters"]``.
    """
    schema_fields, schema_dynamic, expand_paths = walk_schema_graph(root_schema)

    out: List[Dict[str, Any]] = []

    if config.style == "flat":
        top_fields = schema_fields[root_schema]
        all_field_names = sorted({f for fields in schema_fields.values() for f in fields})

        if config.fields_param:
            out.append({
                "in": "query",
                "name": config.fields_param,
                "required": False,
                "schema": {"type": "string", "example": ",".join(top_fields[:2])},
                "description": (
                    "Sparse fieldset: comma-separated list of fields to return. "
                    f"Available on top-level response: {', '.join(top_fields)}."
                ),
            })
        if config.omit_param:
            out.append({
                "in": "query",
                "name": config.omit_param,
                "required": False,
                "schema": {"type": "string"},
                "description": (
                    "Comma-separated list of top-level fields to omit. "
                    f"Available: {', '.join(top_fields)}."
                ),
            })
    else:
        for schema, fields in schema_fields.items():
            resource = schema_resource_name(schema)
            if config.fields_param:
                out.append({
                    "in": "query",
                    "name": f"{config.fields_param}[{resource}]",
                    "required": False,
                    "style": "form",
                    "explode": True,
                    "schema": {"type": "string"},
                    "description": (
                        f"Sparse fieldset for ``{resource}`` resources. "
                        f"Available: {', '.join(fields)}."
                    ),
                })

    includables = sorted({
        f for s in schema_dynamic for f in (get_dynamic_meta(s) or _Empty).includable
        if s is root_schema or True
    })
    root_meta = get_dynamic_meta(root_schema)
    root_includables = sorted(root_meta.includable) if root_meta else []
    if config.include_param and root_includables:
        out.append({
            "in": "query",
            "name": config.include_param,
            "required": False,
            "schema": {"type": "string", "example": root_includables[0]},
            "description": (
                "Comma-separated list of optional relations to include. "
                f"Available: {', '.join(root_includables)}."
            ),
        })

    if config.expand_param and expand_paths:
        sorted_paths = sorted(".".join(p) for p in expand_paths)
        out.append({
            "in": "query",
            "name": config.expand_param,
            "required": False,
            "schema": {"type": "string", "example": sorted_paths[0]},
            "description": (
                "Comma-separated dot-paths to expand nested relations. "
                f"Available: {', '.join(sorted_paths)}."
            ),
        })

    return out


class _Empty:
    includable: Set[str] = set()
    expandable: Set[str] = set()
