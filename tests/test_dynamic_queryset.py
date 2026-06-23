"""
End-to-end ORM-optimization tests: confirm that ``?include=`` attaches
``select_related`` / ``prefetch_related`` to the queryset when (and only
when) the requested path resolves to a real ORM relation chain.
"""
from datetime import date
from typing import List, Optional

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from someapp.models import Category, Event

from ninja import DynamicSchema, Includable, NinjaAPI, dynamic_response
from ninja.testing import TestClient


class CategoryOut(DynamicSchema):
    id: int
    title: str

    # A resolver-backed Includable nested inside another schema. The fix
    # must not try to prefetch ``upper_title`` (it's not on Category).
    upper_title: Includable[str]

    @staticmethod
    def resolve_upper_title(obj):
        return obj.title.upper()


class EventOut(DynamicSchema):
    id: int
    title: str
    start_date: date
    end_date: date

    # Real FK relation — should be select_related.
    category: Includable[Optional[CategoryOut]] = None

    # Scalar Includable backed by a resolver — must be skipped by the
    # queryset optimizer, never passed to prefetch_related.
    custom: Includable[str]

    # Includable typed as a nested Schema but NOT an ORM field on Event —
    # also must be skipped.
    extra: Includable["CategoryOut"]

    __django_model__ = Event

    @staticmethod
    def resolve_custom(obj):
        return f"event-{obj.id}"

    @staticmethod
    def resolve_extra(obj):
        return {"id": 0, "title": "stub"}


@pytest.fixture
def api():
    api = NinjaAPI(urls_namespace="dyn-qs")

    @api.get("/events", response=List[EventOut])
    @dynamic_response
    def list_events(request):
        return Event.objects.all()

    return api


@pytest.fixture
def seeded():
    Category.objects.all().delete()
    Event.objects.all().delete()
    cats = [Category.objects.create(title=f"c{i}") for i in range(3)]
    return [
        Event.objects.create(
            title=f"e{i}",
            start_date="2025-01-01",
            end_date="2025-01-02",
            category=cats[i],
        )
        for i in range(3)
    ]


@pytest.mark.django_db
def test_no_include_means_no_join(api, seeded):
    client = TestClient(api)
    with CaptureQueriesContext(connection) as ctx:
        client.get("/events").json()
    joined = [q for q in ctx.captured_queries if "JOIN" in q["sql"].upper()]
    assert not joined, f"unexpected JOIN: {[q['sql'] for q in joined]}"


@pytest.mark.django_db
def test_include_applies_select_related(api, seeded):
    """
    ``?include=category`` augments the queryset with select_related so the
    response is produced in a single JOIN query rather than N+1.
    """
    client = TestClient(api)
    with CaptureQueriesContext(connection) as ctx:
        body = client.get("/events?include=category").json()
    assert len(body) == 3
    assert body[0]["category"] is not None
    select_queries = [
        q for q in ctx.captured_queries if q["sql"].strip().lower().startswith("select")
    ]
    assert len(select_queries) == 1, [q["sql"] for q in select_queries]
    assert "JOIN" in select_queries[0]["sql"].upper()


@pytest.mark.django_db
def test_include_scalar_resolver_field_does_not_raise(api, seeded):
    """
    Regression test for the original bug report: ``?include=custom`` on a
    scalar Includable backed by a resolver must not be passed to
    prefetch_related (which raises ValueError on non-relation fields).
    """
    client = TestClient(api)
    r = client.get("/events?include=custom")
    assert r.status_code == 200
    body = r.json()
    assert body[0]["custom"] == f"event-{seeded[0].id}"


@pytest.mark.django_db
def test_include_nested_schema_resolver_field_does_not_raise(api, seeded):
    """
    A nested-Schema Includable that's NOT backed by an ORM field (resolver
    provides the value) must also be skipped by the optimizer.

    Note: ``upper_title`` on the nested schema is itself Includable, so it
    stays hidden unless ``?include=extra.upper_title`` is also passed.
    """
    client = TestClient(api)
    r = client.get("/events?include=extra")
    assert r.status_code == 200
    assert r.json()[0]["extra"] == {"id": 0, "title": "stub"}


@pytest.mark.django_db
def test_dot_path_with_resolver_segment_does_not_raise(api, seeded):
    """
    ``?include=category.upper_title`` starts with a real FK hop, but
    ``upper_title`` is a resolver on CategoryOut — not on the Category
    model. The optimizer must skip the whole path rather than attempt to
    prefetch ``category__upper_title``.
    """
    client = TestClient(api)
    r = client.get("/events?include=category.upper_title")
    assert r.status_code == 200
    body = r.json()
    assert body[0]["category"]["upper_title"] == "c0".upper()


@pytest.mark.django_db
def test_invalid_path_does_not_disable_valid_paths(api, seeded):
    """
    A request mixing a valid relation and a resolver-backed field should
    still apply select_related for the valid one and skip the other.
    """
    client = TestClient(api)
    with CaptureQueriesContext(connection) as ctx:
        r = client.get("/events?include=category.upper_title")
    assert r.status_code == 200
    # The valid prefix (category) should still be JOINed.
    select_queries = [
        q for q in ctx.captured_queries if q["sql"].strip().lower().startswith("select")
    ]
    # 1 query (events JOIN categories), not N+1.
    assert len(select_queries) == 1
    assert "JOIN" in select_queries[0]["sql"].upper()


# ---------------------------------------------------------------------------
# Reverse-relation Includables (resolver-driven or via Pydantic's
# from_attributes auto-traversal)
#
# Three sub-cases worth distinguishing because they have different DB-query
# behavior, even though all three return the right data:
#
#   1. schema field name == ORM reverse name, no resolver
#   2. schema field name == ORM reverse name, with resolver
#   3. schema field name != ORM reverse name, with resolver (footgun: N+1)
# ---------------------------------------------------------------------------


class _EventOut(DynamicSchema):
    id: int
    title: str


class _CategoryReverseSame(DynamicSchema):
    """Schema field ``event`` matches Category's ORM reverse name."""

    id: int
    title: str
    event: Includable[Optional[_EventOut]]
    __django_model__ = Category


class _CategoryReverseSameResolver(_CategoryReverseSame):
    """Same as above but with a resolver. The optimizer still keys off the
    field name, so it still prefetches; the resolver supplies the value."""

    @staticmethod
    def resolve_event(obj):
        try:
            return obj.event
        except Event.DoesNotExist:
            return None


class _CategoryReverseRenamed(DynamicSchema):
    """Schema field ``related_event`` does NOT match Category's ORM reverse
    name (``event``). Only the resolver knows about the relation."""

    id: int
    title: str
    related_event: Includable[Optional[_EventOut]]
    __django_model__ = Category

    @staticmethod
    def resolve_related_event(obj):
        try:
            return obj.event
        except Event.DoesNotExist:
            return None


def _make_category_api(schema, ns):
    api = NinjaAPI(urls_namespace=ns)

    @api.get("/c", response=List[schema])
    @dynamic_response
    def list_categories(request):
        return Category.objects.all()

    return api


def _select_query_count(ctx):
    return sum(
        1 for q in ctx.captured_queries if q["sql"].strip().lower().startswith("select")
    )


@pytest.mark.django_db
def test_reverse_relation_via_field_name_only(seeded):
    """
    Field named the same as the ORM reverse — no resolver.

    Pydantic's ``from_attributes=True`` traverses ``category.event``
    automatically. The optimizer applies ``prefetch_related('event')``
    so the response is produced in 2 SELECTs (categories + events) not
    N+1.
    """
    c = TestClient(_make_category_api(_CategoryReverseSame, "rev-1"))
    with CaptureQueriesContext(connection) as ctx:
        body = c.get("/c?include=event").json()
    assert len(body) == 3
    assert all(item["event"]["title"].startswith("e") for item in body)
    assert _select_query_count(ctx) == 2


@pytest.mark.django_db
def test_reverse_relation_via_resolver_with_matching_field_name(seeded):
    """
    Field name matches the ORM reverse and there's also a resolver. The
    resolver supplies the value, but the optimizer still keys off the
    schema field name — so prefetching still happens (2 SELECTs, not N+1).
    """
    c = TestClient(_make_category_api(_CategoryReverseSameResolver, "rev-2"))
    with CaptureQueriesContext(connection) as ctx:
        body = c.get("/c?include=event").json()
    assert all(item["event"] is not None for item in body)
    assert _select_query_count(ctx) == 2


@pytest.mark.django_db
def test_reverse_relation_via_resolver_with_renamed_field(seeded):
    """
    Field is named ``related_event`` but the resolver reaches into
    ``obj.event``. The optimizer can't infer that — there is no
    ``related_event`` on the Category model. Functional result is
    correct, but the query count goes from 2 to 1 + N because no
    prefetch is attached.

    This is a known footgun. Users with custom-named resolver fields
    must manually optimize their queryset (or rename the schema field
    to match the ORM reverse name).
    """
    c = TestClient(_make_category_api(_CategoryReverseRenamed, "rev-3"))
    with CaptureQueriesContext(connection) as ctx:
        body = c.get("/c?include=related_event").json()
    # Functional correctness — the data is still returned.
    assert all(item["related_event"]["title"].startswith("e") for item in body)
    # N+1: 1 query for categories + 1 per category for the reverse OneToOne.
    assert _select_query_count(ctx) == 1 + len(seeded)


@pytest.mark.django_db
def test_reverse_relation_renamed_with_manual_optimization(seeded):
    """
    The user-side workaround for the footgun above: pre-optimize the
    queryset returned from the view. The fork's auto-optimization is a
    no-op here (skipped because ``related_event`` isn't an ORM field),
    so the user's manual ``prefetch_related('event')`` survives intact.
    """
    api = NinjaAPI(urls_namespace="rev-4")

    @api.get("/c", response=List[_CategoryReverseRenamed])
    @dynamic_response
    def list_categories(request):
        return Category.objects.prefetch_related("event")

    c = TestClient(api)
    with CaptureQueriesContext(connection) as ctx:
        body = c.get("/c?include=related_event").json()
    assert all(item["related_event"]["title"].startswith("e") for item in body)
    assert _select_query_count(ctx) == 2  # categories + events


# ---------------------------------------------------------------------------
# Fix A: pagination + @dynamic_response decorator stacking.
#
# Pagination evaluates the queryset (via .count() and slicing), so the
# dynamic decorator must thread its ?include= optimization through the
# pagination wrapper. Both decorator orders and the ``RouterPaginated``
# entry point go through ``_inject_pagination``; the integration hook
# lives there.
# ---------------------------------------------------------------------------


class _EventOutForPagination(DynamicSchema):
    id: int
    title: str
    category: Includable[Optional[CategoryOut]]
    __django_model__ = Event


@pytest.mark.django_db
def test_pagination_dynamic_outer_paginate_inner(seeded):
    """
    ``@dynamic_response`` outer + ``@paginate`` inner — the inner decorator
    evaluates the queryset before the outer one sees it. With the
    integration hook in ``_inject_pagination``, the optimization still
    lands on the queryset (2 queries instead of 1 + page_size).
    """
    from ninja.pagination import PageNumberPagination, paginate

    api = NinjaAPI(urls_namespace="pag-dyn-outer")

    @api.get("/e", response=List[_EventOutForPagination])
    @dynamic_response
    @paginate(PageNumberPagination, page_size=2)
    def list_events(request):
        return Event.objects.all()

    with CaptureQueriesContext(connection) as ctx:
        body = TestClient(api).get("/e?include=category").json()
    assert len(body["items"]) == 2
    assert all(item["category"] is not None for item in body["items"])
    # 2 queries: COUNT + the events query (which JOINs categories via
    # select_related). Without the fix this would have been 1 + page_size.
    assert _select_query_count(ctx) == 2


@pytest.mark.django_db
def test_pagination_paginate_outer_dynamic_inner(seeded):
    """
    The reverse decorator order was already working. Regression guard:
    the new integration hook must not double-optimize or break it.
    """
    from ninja.pagination import PageNumberPagination, paginate

    api = NinjaAPI(urls_namespace="pag-dyn-inner")

    @api.get("/e", response=List[_EventOutForPagination])
    @paginate(PageNumberPagination, page_size=2)
    @dynamic_response
    def list_events(request):
        return Event.objects.all()

    with CaptureQueriesContext(connection) as ctx:
        body = TestClient(api).get("/e?include=category").json()
    assert len(body["items"]) == 2
    assert all(item["category"] is not None for item in body["items"])
    assert _select_query_count(ctx) == 2


@pytest.mark.django_db
def test_pagination_router_paginated_with_dynamic(seeded):
    """
    ``RouterPaginated`` auto-wraps collection-typed endpoints in
    ``_inject_pagination``. The integration hook applies there too, so
    using ``RouterPaginated`` + ``@dynamic_response`` works without an
    explicit ``@paginate`` decorator.
    """
    from ninja.pagination import RouterPaginated

    api = NinjaAPI(urls_namespace="pag-router", default_router=RouterPaginated())

    @api.get("/e", response=List[_EventOutForPagination])
    @dynamic_response
    def list_events(request):
        return Event.objects.all()

    with CaptureQueriesContext(connection) as ctx:
        body = TestClient(api).get("/e?include=category").json()
    assert all(item["category"] is not None for item in body["items"])
    assert _select_query_count(ctx) == 2


@pytest.mark.django_db
def test_pagination_without_dynamic_still_works(seeded):
    """
    Regression: pagination by itself (no ``@dynamic_response``) must not
    pay any cost or change behavior from the integration hook.
    """
    from ninja.pagination import PageNumberPagination, paginate

    class PlainOut(DynamicSchema):
        id: int
        title: str

    api = NinjaAPI(urls_namespace="pag-only")

    @api.get("/e", response=List[PlainOut])
    @paginate(PageNumberPagination, page_size=2)
    def list_events(request):
        return Event.objects.all()

    with CaptureQueriesContext(connection) as ctx:
        body = TestClient(api).get("/e").json()
    assert len(body["items"]) == 2
    # 2 queries: COUNT + events. No JOIN expected (no include).
    selects = [
        q for q in ctx.captured_queries if q["sql"].lower().strip().startswith("select")
    ]
    assert len(selects) == 2
    assert all("JOIN" not in q["sql"].upper() for q in selects)


# ---------------------------------------------------------------------------
# Fix B: single instance returns get prefetch_related_objects().
#
# The optimizer previously skipped non-QuerySet results, so a view
# returning ``Model.objects.get(pk=id)`` couldn't benefit from
# ``?include=``. The fix detects single Model instances and lists of
# Model instances and applies ``prefetch_related_objects`` to them.
# ---------------------------------------------------------------------------


class _CategoryMulti(DynamicSchema):
    """For a single-instance test with multiple Includables."""

    id: int
    title: str
    event: Includable[Optional[_EventOut]]
    __django_model__ = Category


@pytest.mark.django_db
def test_single_instance_with_include_loads_relation(seeded):
    """
    A view returning ``Model.objects.get(...)`` with ``?include=category``
    must have the relation populated. The query count is the same as the
    no-fix baseline (one extra query for the relation), but the relation
    cache is populated so subsequent access doesn't lazy-load again.
    """
    api = NinjaAPI(urls_namespace="si-1")

    @api.get("/e/{id}", response=EventOut)
    @dynamic_response
    def get_event(request, id: int):
        return Event.objects.get(pk=id)

    body = TestClient(api).get(f"/e/{seeded[0].id}?include=category").json()
    assert body["category"] is not None
    assert body["category"]["title"] == seeded[0].category.title


@pytest.mark.django_db
def test_single_instance_relation_is_cached_after_prefetch(seeded):
    """
    Direct test of ``apply_query_optimization`` on a single instance —
    after the call, the relation is loaded into the cache and subsequent
    attribute access doesn't trigger a query.
    """
    from ninja.dynamic.queryset import apply_query_optimization
    from ninja.dynamic.selector import FieldSelector

    sel = FieldSelector(includes={("category",)})
    event = Event.objects.get(pk=seeded[0].id)
    # baseline: clear the cache and confirm access fires a query
    if "category" in event._state.fields_cache:
        del event._state.fields_cache["category"]
    with CaptureQueriesContext(connection) as ctx:
        _ = event.category
    assert _select_query_count(ctx) == 1

    # apply optimization, then access should be free
    event = Event.objects.get(pk=seeded[0].id)
    apply_query_optimization(event, sel, EventOut)
    with CaptureQueriesContext(connection) as ctx:
        _ = event.category
    assert _select_query_count(ctx) == 0


@pytest.mark.django_db
def test_evaluated_list_with_include_eliminates_n_plus_one(seeded):
    """
    A view returning ``list(Model.objects.all())`` previously caused N+1
    on ``?include=category``. The fix applies ``prefetch_related_objects``
    to the list, collapsing to 2 queries (the original SELECT + 1 batched
    relation lookup).
    """
    api = NinjaAPI(urls_namespace="si-list")

    @api.get("/e", response=List[EventOut])
    @dynamic_response
    def list_events(request):
        return list(Event.objects.all())  # forced evaluation

    with CaptureQueriesContext(connection) as ctx:
        body = TestClient(api).get("/e?include=category").json()
    assert len(body) == len(seeded)
    assert all(item["category"] is not None for item in body)
    # Before fix: 1 + len(seeded) queries. After: 2 (events + batched categories).
    assert _select_query_count(ctx) == 2


@pytest.mark.django_db
def test_single_instance_unrelated_model_is_skipped(seeded):
    """
    Safety: if the view returns a Model instance of a different class
    than the schema's underlying model, optimization is silently skipped
    rather than raising.
    """
    from ninja.dynamic.queryset import apply_query_optimization
    from ninja.dynamic.selector import FieldSelector

    sel = FieldSelector(includes={("category",)})
    # Pass a Category instance to a schema bound to Event — should no-op.
    unrelated = Category.objects.first()
    result = apply_query_optimization(unrelated, sel, EventOut)
    assert result is unrelated  # unchanged


@pytest.mark.django_db
def test_empty_evaluated_list_is_safe(seeded):
    """
    An empty list returned from the view (no instances to prefetch on)
    must not raise.
    """
    api = NinjaAPI(urls_namespace="si-empty")

    @api.get("/e", response=List[EventOut])
    @dynamic_response
    def list_events(request):
        return []

    body = TestClient(api).get("/e?include=category").json()
    assert body == []
