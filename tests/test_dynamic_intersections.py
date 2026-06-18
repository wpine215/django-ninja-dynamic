"""
Coverage of intersections between the dynamic-schema fork and other
django-ninja features. Each test (or test block) pins down behavior for
one feature combination identified in the audit:

    #1  Pagination + @dynamic_response (both decorator orders)
    #2  Multi-status responses (response={200: A, 404: B})
    #3  by_alias=True operation flag
    #4  Async views
    #5  exclude_none=True operation flag
    #6  User-supplied openapi_extra= preservation
    #7  Router cloning / router reuse
    #8  include_in_schema=False
    #9  Streaming responses (graceful no-op expected)
    #10 ModelSchema + DynamicSchema metaclass conflict (documented limitation)
    #11 @decorate_view (operation.run wrapper) interaction
    #12 FilterSchema cohabitation
"""
from typing import List, Optional

import pytest

from ninja import (
    DynamicSchema,
    Expandable,
    Field,
    FilterSchema,
    Includable,
    NinjaAPI,
    Router,
    Schema,
    Status,
    dynamic_response,
)
from ninja.pagination import PageNumberPagination, paginate
from ninja.testing import TestAsyncClient, TestClient


# ---------------------------------------------------------------------------
# Shared schemas
# ---------------------------------------------------------------------------
class UserSchema(DynamicSchema):
    id: int
    name: str
    email: str
    bio: Includable[str] = None


class ErrorSchema(Schema):
    detail: str


def _users_payload(n: int = 5):
    return [
        {"id": i, "name": f"U{i}", "email": f"u{i}@x", "bio": f"bio{i}"}
        for i in range(1, n + 1)
    ]


# ---------------------------------------------------------------------------
# #1 Pagination + @dynamic_response — both orderings
# ---------------------------------------------------------------------------
class TestPaginationIntersection:
    """
    Stacked decorators must both fire and the dynamic filter must reach the
    Paged wrapper's items array (preserving sibling fields like ``count``).
    """

    @pytest.fixture
    def dyn_outer(self):
        api = NinjaAPI(urls_namespace="i1-do")

        @api.get("/users", response=List[UserSchema])
        @dynamic_response
        @paginate(PageNumberPagination, page_size=2)
        def view(request):
            return _users_payload()

        return TestClient(api)

    @pytest.fixture
    def pag_outer(self):
        api = NinjaAPI(urls_namespace="i1-po")

        @api.get("/users", response=List[UserSchema])
        @paginate(PageNumberPagination, page_size=2)
        @dynamic_response
        def view(request):
            return _users_payload()

        return TestClient(api)

    def test_dyn_outer_sparse_filters_items_preserves_count(self, dyn_outer):
        r = dyn_outer.get("/users?fields=name").json()
        assert r == {"items": [{"name": "U1"}, {"name": "U2"}], "count": 5}

    def test_pag_outer_sparse_filters_items_preserves_count(self, pag_outer):
        r = pag_outer.get("/users?fields=name").json()
        assert r == {"items": [{"name": "U1"}, {"name": "U2"}], "count": 5}

    def test_pag_outer_pagination_still_works_with_filter(self, pag_outer):
        r = pag_outer.get("/users?fields=name&page=2").json()
        assert r == {"items": [{"name": "U3"}, {"name": "U4"}], "count": 5}

    def test_pag_outer_omit_works(self, pag_outer):
        r = pag_outer.get("/users?omit=email,bio").json()
        items = r["items"]
        assert all("email" not in i and "bio" not in i for i in items)
        assert all("name" in i for i in items)

    def test_pag_outer_include_works(self, pag_outer):
        r = pag_outer.get("/users?fields=name,bio&include=bio").json()
        items = r["items"]
        assert items[0] == {"name": "U1", "bio": "bio1"}


# ---------------------------------------------------------------------------
# #2 Multiple response status codes
# ---------------------------------------------------------------------------
class TestMultiStatusResponse:
    @pytest.fixture
    def client(self):
        api = NinjaAPI(urls_namespace="i2")

        @api.get("/u/{id}", response={200: UserSchema, 404: ErrorSchema})
        @dynamic_response
        def get_user(request, id: int):
            if id == 0:
                return Status(404, {"detail": "not found"})
            return {"id": id, "name": "A", "email": "a@x", "bio": "b"}

        return TestClient(api)

    def test_200_filter_applied(self, client):
        assert client.get("/u/1?fields=name").json() == {"name": "A"}

    def test_404_passes_through_untouched(self, client):
        """
        A ``?fields=`` query applies to the schema we captured (UserSchema);
        when a 404 with ErrorSchema fires instead, the filter must skip and
        the error body must come through intact.
        """
        r = client.get("/u/0?fields=name")
        assert r.status_code == 404
        assert r.json() == {"detail": "not found"}


# ---------------------------------------------------------------------------
# #3 by_alias=True
# ---------------------------------------------------------------------------
class TestByAlias:
    @pytest.fixture
    def client(self):
        api = NinjaAPI(urls_namespace="i3")

        class AliasedUser(DynamicSchema):
            id: int
            full_name: str = Field(..., alias="name")
            email: str

        @api.get("/u/{id}", response=AliasedUser, by_alias=True)
        @dynamic_response
        def get_user(request, id: int):
            return {"id": id, "name": "Alice", "email": "a@x"}

        return TestClient(api)

    def test_request_with_alias_succeeds(self, client):
        r = client.get("/u/1?fields=name").json()
        assert r == {"name": "Alice"}

    def test_request_with_field_name_succeeds(self, client):
        r = client.get("/u/1?fields=full_name").json()
        assert r == {"name": "Alice"}

    def test_omit_accepts_alias(self, client):
        r = client.get("/u/1?omit=name").json()
        assert "name" not in r
        assert r["email"] == "a@x"

    def test_unknown_name_rejected(self, client):
        r = client.get("/u/1?fields=bogus")
        assert r.status_code == 422

    def test_error_message_lists_aliases(self, client):
        r = client.get("/u/1?fields=bogus")
        msg = r.json()["detail"][0]["msg"]
        assert "name" in msg or "full_name" in msg


# ---------------------------------------------------------------------------
# #4 Async views
# ---------------------------------------------------------------------------
class TestAsync:
    @pytest.mark.asyncio
    async def test_async_view_runs_through_dynamic_pipeline(self):
        api = NinjaAPI(urls_namespace="i4")

        @api.get("/u/{id}", response=UserSchema)
        @dynamic_response
        async def get_user(request, id: int):
            return {"id": id, "name": "Alice", "email": "a@x", "bio": "hi"}

        client = TestAsyncClient(api)
        r = await client.get("/u/1?fields=name,email")
        assert r.json() == {"name": "Alice", "email": "a@x"}

    @pytest.mark.asyncio
    async def test_async_include_marker(self):
        api = NinjaAPI(urls_namespace="i4b")

        @api.get("/u/{id}", response=UserSchema)
        @dynamic_response
        async def get_user(request, id: int):
            return {"id": id, "name": "Alice", "email": "a@x", "bio": "hi"}

        client = TestAsyncClient(api)
        r = await client.get("/u/1?include=bio")
        assert r.json()["bio"] == "hi"


# ---------------------------------------------------------------------------
# #5 exclude_none=True operation flag
# ---------------------------------------------------------------------------
class TestExcludeNone:
    @pytest.fixture
    def client(self):
        api = NinjaAPI(urls_namespace="i5")

        @api.get("/u/{id}", response=UserSchema, exclude_none=True)
        @dynamic_response
        def get_user(request, id: int):
            return {"id": id, "name": "Alice", "email": "a@x", "bio": None}

        return TestClient(api)

    def test_none_bio_dropped_by_default(self, client):
        r = client.get("/u/1").json()
        assert "bio" not in r
        assert r["name"] == "Alice"

    def test_sparse_with_exclude_none_composes(self, client):
        r = client.get("/u/1?fields=name,email").json()
        assert r == {"name": "Alice", "email": "a@x"}


# ---------------------------------------------------------------------------
# #6 User-supplied openapi_extra preservation
# ---------------------------------------------------------------------------
class TestOpenAPIExtraPreservation:
    @pytest.fixture
    def api(self):
        api = NinjaAPI(urls_namespace="i6")

        @api.get(
            "/u/{id}",
            response=UserSchema,
            openapi_extra={
                "x-internal": "yes",
                "parameters": [
                    {"in": "header", "name": "X-Trace-Id", "required": False}
                ],
            },
        )
        @dynamic_response
        def get_user(request, id: int):
            return {}

        return api

    def test_x_extension_survives(self, api):
        op = api.get_openapi_schema(path_prefix="/api/")["paths"]["/api/u/{id}"]["get"]
        assert op.get("x-internal") == "yes"

    def test_user_parameters_and_dynamic_parameters_coexist(self, api):
        op = api.get_openapi_schema(path_prefix="/api/")["paths"]["/api/u/{id}"]["get"]
        names = [p["name"] for p in op["parameters"]]
        # User's header
        assert "X-Trace-Id" in names
        # Our dynamic params
        assert "fields" in names
        assert "omit" in names
        assert "include" in names


# ---------------------------------------------------------------------------
# #7 Router cloning / reuse via add_router into multiple APIs
# ---------------------------------------------------------------------------
class TestRouterReuse:
    def test_same_router_mounted_in_two_apis_keeps_dynamic_state(self):
        """
        django-ninja clones operations when a router is mounted, so the
        dynamic state is stashed on the shared view_func — both APIs must
        find it.
        """
        router = Router()

        @router.get("/u/{id}", response=UserSchema)
        @dynamic_response
        def get_user(request, id: int):
            return {"id": id, "name": "A", "email": "a@x", "bio": "b"}

        api_a = NinjaAPI(urls_namespace="i7-a")
        api_a.add_router("/a", router)
        api_b = NinjaAPI(urls_namespace="i7-b")
        api_b.add_router("/b", router)

        ra = TestClient(api_a).get("/a/u/1?fields=name").json()
        rb = TestClient(api_b).get("/b/u/1?fields=name").json()
        assert ra == {"name": "A"}
        assert rb == {"name": "A"}


# ---------------------------------------------------------------------------
# #8 include_in_schema=False on the operation
# ---------------------------------------------------------------------------
class TestIncludeInSchemaFalse:
    def test_dynamic_params_omitted_when_operation_hidden(self):
        api = NinjaAPI(urls_namespace="i8")

        @api.get("/hidden", response=UserSchema, include_in_schema=False)
        @dynamic_response
        def hidden(request):
            return {}

        schema = api.get_openapi_schema(path_prefix="/api/")
        # The operation isn't emitted at all when include_in_schema=False —
        # whether through the regular path entry or as a phantom dynamic
        # operation. Either is acceptable; assert it's not there.
        assert "/api/hidden" not in schema["paths"]


# ---------------------------------------------------------------------------
# #9 Streaming responses — out-of-scope but should not crash
# ---------------------------------------------------------------------------
class TestStreamingGracefulNoOp:
    def test_streaming_endpoint_with_dynamic_does_not_crash(self):
        """
        Streaming endpoints (SSE/JSONL) aren't in the dynamic feature
        contract. Pinning down: applying @dynamic_response to a streaming
        endpoint should either no-op or raise a clean ConfigError at
        registration time — never a runtime mystery.
        """
        from ninja import JSONL

        api = NinjaAPI(urls_namespace="i9")

        # Streaming responses use JSONL[Schema] / SSE[Schema] generics
        # instead of Schema or List[Schema]. @dynamic_response is expected
        # to either no-op (dynamic_dump_kwargs returns empty because the
        # captured schema is wrapped in a StreamFormat) or raise a clear
        # ConfigError at decoration time.
        try:
            @api.get("/stream", response=JSONL[UserSchema])
            @dynamic_response
            def stream(request):
                for u in _users_payload(3):
                    yield u
        except Exception as exc:
            # Acceptable: a clear error at decoration time mentioning the
            # streaming surface.
            msg = str(exc).lower()
            assert "stream" in msg or "dynamic" in msg or "response" in msg, exc
            return

        # If registration succeeded, the endpoint still works (streaming
        # responses are sent as-is; the dynamic filter is inert).
        r = TestClient(api).get("/stream")
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# #10 ModelSchema + DynamicSchema metaclass conflict
# ---------------------------------------------------------------------------
class TestModelSchemaConflict:
    def test_metaclass_conflict_raises_typeerror(self):
        """
        Multiple inheritance from both ``ModelSchema`` and ``DynamicSchema``
        currently raises a metaclass conflict. Pinning the documented
        limitation here so any future improvement (a unified metaclass) is
        a discoverable change.
        """
        from ninja import ModelSchema
        from someapp.models import Event

        with pytest.raises(TypeError, match="metaclass conflict"):
            class HybridSchema(DynamicSchema, ModelSchema):
                class Meta:
                    model = Event
                    fields = ["id", "title"]

    def test_workaround_django_model_attribute_supported(self):
        """
        Plain DynamicSchema + ``__django_model__`` is the supported path
        for now (already exercised by tests/test_dynamic_queryset.py).
        """
        from someapp.models import Event

        class EventOut(DynamicSchema):
            id: int
            title: str
            __django_model__ = Event

        # The attribute survives the metaclass.
        assert EventOut.__django_model__ is Event


# ---------------------------------------------------------------------------
# #11 @decorate_view interaction
# ---------------------------------------------------------------------------
class TestDecorateViewInteraction:
    def test_decorate_view_runs_alongside_dynamic_response(self):
        """
        @decorate_view wraps Operation.run; dynamic state lives on view_func.
        The two should compose: the run-wrapper observes the request and
        defers, dynamic state stays attached, sparse filter still applies.
        """
        from ninja.decorators import decorate_view

        observed = {}

        def trace(handler):
            def wrapper(request, *a, **kw):
                observed["called"] = True
                return handler(request, *a, **kw)

            return wrapper

        api = NinjaAPI(urls_namespace="i11")

        @api.get("/u/{id}", response=UserSchema)
        @decorate_view(trace)
        @dynamic_response
        def get_user(request, id: int):
            return {"id": id, "name": "A", "email": "a@x", "bio": "b"}

        r = TestClient(api).get("/u/1?fields=name").json()
        assert r == {"name": "A"}
        assert observed.get("called") is True


# ---------------------------------------------------------------------------
# #12 FilterSchema cohabitation
# ---------------------------------------------------------------------------
class TestFilterSchemaCohabitation:
    def test_filter_schema_query_params_do_not_collide(self):
        """
        FilterSchema attaches query params with the names of its fields.
        It must not collide with dynamic's ``fields``/``omit``/``include``/
        ``expand`` params on a single endpoint — both feature sets coexist.
        """
        from ninja import Query

        class UserFilter(FilterSchema):
            name_starts: Optional[str] = Field(None, q="name__startswith")

        api = NinjaAPI(urls_namespace="i12")

        @api.get("/users", response=List[UserSchema])
        @dynamic_response
        def list_users(request, filters: UserFilter = Query(...)):
            users = _users_payload()
            if filters.name_starts:
                users = [u for u in users if u["name"].startswith(filters.name_starts)]
            return users

        c = TestClient(api)
        # Filter alone
        r = c.get("/users?name_starts=U2").json()
        assert r == [{"id": 2, "name": "U2", "email": "u2@x", "bio": "bio2"}]
        # Filter + dynamic sparse together
        r = c.get("/users?name_starts=U3&fields=name").json()
        assert r == [{"name": "U3"}]
