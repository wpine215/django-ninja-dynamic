"""
End-to-end ORM-optimization tests: confirm that ?include= / ?expand= attach
select_related / prefetch_related to the queryset before the response is
serialized, so we don't N+1 the database.
"""
from typing import List, Optional

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from someapp.models import Category, Event

from datetime import date

from ninja import DynamicSchema, Includable, NinjaAPI, dynamic_response
from ninja.testing import TestClient


class CategoryOut(DynamicSchema):
    id: int
    title: str


class EventOut(DynamicSchema):
    id: int
    title: str
    start_date: date
    end_date: date
    category: Includable[Optional[CategoryOut]] = None

    # Explicit Django-model link consumed by ninja.dynamic.queryset.apply_query_optimization.
    __django_model__ = Event


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
    With ``?include=category``, the queryset should be augmented with
    select_related('category'), producing exactly one query (a JOIN) instead
    of one per item.
    """
    client = TestClient(api)
    with CaptureQueriesContext(connection) as ctx:
        body = client.get("/events?include=category").json()
    assert len(body) == 3
    assert body[0]["category"] is not None
    # one query total for events + categories (select_related JOINs)
    select_queries = [q for q in ctx.captured_queries if q["sql"].strip().lower().startswith("select")]
    assert len(select_queries) == 1, [q["sql"] for q in select_queries]
    assert "JOIN" in select_queries[0]["sql"].upper()
