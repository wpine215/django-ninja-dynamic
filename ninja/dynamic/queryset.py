from typing import Any, List, Optional, Tuple, Type

from django.db.models import ForeignKey, Model, OneToOneField, QuerySet

from ninja.dynamic.selector import FieldSelector, _resolve_field_schema
from ninja.schema import Schema


def _orm_model_for(schema: Type[Schema]) -> Optional[Type[Model]]:
    """
    Best-effort lookup of the Django model behind a schema. Recognizes
    ``ModelSchema`` (``Meta.model``) and the explicit override
    ``__django_model__`` for plain ``Schema`` subclasses.
    """
    meta = getattr(schema, "Meta", None) or getattr(schema, "Config", None)
    if meta is not None:
        m = getattr(meta, "model", None)
        if isinstance(m, type) and issubclass(m, Model):
            return m
    m = getattr(schema, "__django_model__", None)
    if isinstance(m, type) and issubclass(m, Model):
        return m
    return None


def _classify_relation(model: Type[Model], orm_field_name: str) -> Optional[str]:
    """
    Return ``"select"`` for FK/O2O, ``"prefetch"`` for reverse / M2M, ``None``
    if the field is unknown (caller should default to prefetch_related as the
    safe fallback).
    """
    try:
        field = model._meta.get_field(orm_field_name)
    except Exception:
        return None
    if isinstance(field, (ForeignKey, OneToOneField)):
        return "select"
    return "prefetch"


def _walk_paths(selector: FieldSelector) -> List[Tuple[str, ...]]:
    """
    All ORM-relation paths implied by the selector's ``?include=``
    dot-paths, sorted shortest-first so callers can attach the deepest spec
    for each prefix.
    """
    return sorted(selector.includes, key=len)


def apply_query_optimization(
    result: Any, selector: FieldSelector, schema: Type[Schema]
) -> Any:
    """
    If ``result`` is a Django ``QuerySet`` (or ``Manager``) and ``schema`` is
    backed by a Django model, attach ``select_related`` / ``prefetch_related``
    for every relation the selector pulls in via ``?include=``. No-op for
    plain values, dicts, or single model instances.
    """
    if not selector.includes:
        return result

    qs = result
    manager_to_qs = getattr(qs, "all", None)
    if callable(manager_to_qs):
        try:
            qs = manager_to_qs()
        except Exception:
            return result
    if not isinstance(qs, QuerySet):
        return result

    base_model = _orm_model_for(schema)
    if base_model is None:
        return result

    select_chains: List[str] = []
    prefetch_chains: List[str] = []

    for path in _walk_paths(selector):
        kind = _classify_chain(path, schema, base_model)
        chain = "__".join(path)
        if kind == "select":
            select_chains.append(chain)
        else:
            prefetch_chains.append(chain)

    if select_chains:
        qs = qs.select_related(*sorted(set(select_chains)))
    if prefetch_chains:
        qs = qs.prefetch_related(*sorted(set(prefetch_chains)))
    return qs


def _classify_chain(
    path: Tuple[str, ...], schema: Type[Schema], model: Type[Model]
) -> str:
    """
    Classify the whole dot-path as ``"select"`` only if every hop is an
    FK/O2O on its corresponding model; otherwise ``"prefetch"`` (safe choice
    that also works for FK chains).
    """
    cur_model = model
    cur_schema: Optional[Type[Schema]] = schema
    is_all_select = True

    for hop in path:
        kind = _classify_relation(cur_model, hop)
        if kind != "select":
            is_all_select = False

        try:
            next_field = cur_model._meta.get_field(hop)
        except Exception:
            break
        related = getattr(next_field, "related_model", None)
        if related is None:
            break
        cur_model = related

        if cur_schema is not None:
            schema_field = cur_schema.model_fields.get(hop)
            cur_schema = (
                _resolve_field_schema(schema_field.annotation)
                if schema_field is not None
                else None
            )

    return "select" if is_all_select else "prefetch"
