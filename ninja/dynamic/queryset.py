from typing import Any, List, Optional, Set, Tuple, Type

from django.db.models import ForeignKey, Model, OneToOneField, QuerySet
from django.db.models.query import prefetch_related_objects
from django.http import HttpRequest

from ninja.dynamic.selector import FieldSelector
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
    if the field does not exist on ``model`` (a resolver-backed or scalar
    Includable). Callers must treat ``None`` as "not an ORM relation — skip
    this path entirely", not as a fallback to prefetch.
    """
    try:
        field = model._meta.get_field(orm_field_name)
    except Exception:
        return None
    if isinstance(field, (ForeignKey, OneToOneField)):
        return "select"
    related = getattr(field, "related_model", None)
    if related is None:
        # Concrete scalar column (CharField, IntegerField, ...) — not a
        # relation, can't be prefetched.
        return None
    return "prefetch"


def _walk_paths(selector: FieldSelector) -> List[Tuple[str, ...]]:
    """
    All ORM-relation paths implied by the selector's ``?include=``
    dot-paths, sorted shortest-first so callers can attach the deepest spec
    for each prefix.
    """
    return sorted(selector.includes, key=len)


def _compute_chains(
    selector: FieldSelector, base_model: Type[Model]
) -> Tuple[Set[str], Set[str]]:
    """
    Walk the selector's include paths against the base model and split each
    into either a ``select_related`` chain or a ``prefetch_related`` chain.
    Returns ``(select_chains, prefetch_chains)`` as deduped sets.
    """
    select_chains: Set[str] = set()
    prefetch_chains: Set[str] = set()
    for path in _walk_paths(selector):
        prefix, kind = _longest_orm_prefix(path, base_model)
        if not prefix:
            continue
        chain = "__".join(prefix)
        if kind == "select":
            select_chains.add(chain)
        else:
            prefetch_chains.add(chain)
    return select_chains, prefetch_chains


def apply_query_optimization(
    result: Any, selector: FieldSelector, schema: Type[Schema]
) -> Any:
    """
    Attach ``select_related`` / ``prefetch_related`` (or call
    ``prefetch_related_objects``) for every ``?include=`` path that resolves
    to a real ORM relation chain on ``schema``'s underlying Django model.

    Supported input shapes for ``result``:

    * Django ``QuerySet`` — modified in place via ``select_related`` /
      ``prefetch_related``; the optimized queryset is returned.
    * Django ``Manager`` — ``result.all()`` is called to obtain a queryset,
      then same as above.
    * Single Django ``Model`` instance — ``prefetch_related_objects`` is
      called on ``[result]`` so the relation cache is populated.
    * Non-empty ``list`` of Django ``Model`` instances — same as above on
      the whole list.

    Anything else (dicts, plain values, generators, lists of dicts, lists
    that mix types) is returned unchanged. Paths whose segments aren't ORM
    relations are silently skipped — they're supplied at validation time
    by a resolver or the view payload.
    """
    if not selector.includes:
        return result

    base_model = _orm_model_for(schema)
    if base_model is None:
        return result

    select_chains, prefetch_chains = _compute_chains(selector, base_model)
    if not select_chains and not prefetch_chains:
        return result

    # Manager → QuerySet (e.g. ``Author.objects`` instead of ``Author.objects.all()``)
    if (
        not isinstance(result, (QuerySet, Model, list))
        and callable(getattr(result, "all", None))
    ):
        try:
            result = result.all()
        except Exception:
            return result

    if isinstance(result, QuerySet):
        if select_chains:
            result = result.select_related(*sorted(select_chains))
        if prefetch_chains:
            result = result.prefetch_related(*sorted(prefetch_chains))
        return result

    if isinstance(result, Model):
        # Single instance — only optimize if it's the schema's model (a
        # mismatched model can't have these relations).
        if isinstance(result, base_model):
            # ``prefetch_related_objects`` works for select-related chains
            # too (it just doesn't JOIN); since the instance is already
            # fetched we can't add a JOIN after the fact.
            chains = sorted(select_chains | prefetch_chains)
            if chains:
                prefetch_related_objects([result], *chains)
        return result

    if (
        isinstance(result, list)
        and result
        and all(isinstance(x, base_model) for x in result)
    ):
        chains = sorted(select_chains | prefetch_chains)
        if chains:
            prefetch_related_objects(result, *chains)
        return result

    return result


def apply_pending_optimization(
    request: HttpRequest, queryset: Any
) -> Any:
    """
    Apply queryset optimization deferred from a ``@dynamic_response``
    decorator that wraps something else (e.g. ``@paginate``). Pagination
    calls this after the wrapped view returns the raw queryset so that the
    dynamic-schema ``?include=`` paths can attach
    ``select_related`` / ``prefetch_related`` before pagination evaluates
    the queryset.

    No-op when no dynamic state is on the request or when ``?include=``
    wasn't supplied.
    """
    selector = getattr(request, "_ninja_dynamic_selector", None)
    schema = getattr(request, "_ninja_dynamic_response_schema", None)
    if selector is None or schema is None or not selector.includes:
        return queryset
    return apply_query_optimization(queryset, selector, schema)


def _longest_orm_prefix(
    path: Tuple[str, ...], model: Type[Model]
) -> Tuple[Tuple[str, ...], Optional[str]]:
    """
    Walk ``path`` against ``model``'s ORM metadata as far as every segment
    is a real relation, and return ``(orm_prefix, kind)``:

    * ``orm_prefix`` is the longest leading segment tuple that resolves to
      real ORM relations. Empty if even the first segment is non-ORM.
    * ``kind`` is ``"select"`` (every hop is FK/O2O), ``"prefetch"`` (at
      least one reverse/M2M hop), or ``None`` (empty prefix).

    The trailing segments past ``orm_prefix`` are assumed to be
    resolver-backed or scalar fields — they need no ORM work, the
    resolver supplies the value at validation time.
    """
    cur_model = model
    is_all_select = True
    consumed: List[str] = []

    for hop in path:
        kind = _classify_relation(cur_model, hop)
        if kind is None:
            break
        if kind != "select":
            is_all_select = False
        consumed.append(hop)

        next_field = cur_model._meta.get_field(hop)
        related = getattr(next_field, "related_model", None)
        if related is None:
            # Shouldn't happen — _classify_relation already filtered scalars.
            break  # pragma: no cover
        cur_model = related

    if not consumed:
        return (), None
    return tuple(consumed), "select" if is_all_select else "prefetch"
