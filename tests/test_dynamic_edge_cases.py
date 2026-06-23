"""
Edge cases discovered during the post-implementation audit. Includes both
regression tests for fixed bugs (empty ``?fields=``, alias in ``?include=``)
and pinning tests for working-but-previously-untested behaviors.
"""
from typing import Dict, List, Optional

import pytest
from django.http import JsonResponse

from ninja import (
    DynamicSchema,
    Field,
    Includable,
    NinjaAPI,
    Schema,
    dynamic_response,
)
from ninja.dynamic.config import DynamicConfig
from ninja.testing import TestClient


# ---------------------------------------------------------------------------
# Fixed bugs — regression tests
# ---------------------------------------------------------------------------
class TestEmptyFieldsParam:
    """
    ``?fields=`` (no values) was returning ``{}``; it should be treated as
    if the parameter wasn't supplied at all.
    """

    @pytest.fixture
    def client(self):
        class S(DynamicSchema):
            id: int
            name: str

        api = NinjaAPI(urls_namespace="ec-empty-fields")

        @api.get("/x", response=S)
        @dynamic_response
        def gx(request):
            return {"id": 1, "name": "A"}

        return TestClient(api)

    def test_empty_fields_returns_defaults(self, client):
        assert client.get("/x?fields=").json() == {"id": 1, "name": "A"}

    def test_only_separators_returns_defaults(self, client):
        assert client.get("/x?fields=,,,").json() == {"id": 1, "name": "A"}

    def test_real_fields_still_filtered(self, client):
        assert client.get("/x?fields=name").json() == {"name": "A"}


class TestAliasInInclude:
    """
    ``?include=alias`` must work, mirroring the way ``?fields=alias`` works
    for default-visible aliased fields.
    """

    @pytest.fixture
    def client(self):
        class S(DynamicSchema):
            id: int
            name: str
            more_data: Includable[str] = Field(None, alias="moreData")

        api = NinjaAPI(urls_namespace="ec-include-alias")

        @api.get("/x", response=S, by_alias=True)
        @dynamic_response
        def gx(request):
            # Supply with the alias so by_alias serialization round-trips.
            return {"id": 1, "name": "A", "moreData": "hi"}

        return TestClient(api)

    def test_include_by_field_name(self, client):
        body = client.get("/x?include=more_data").json()
        assert body["moreData"] == "hi"

    def test_include_by_alias(self, client):
        body = client.get("/x?include=moreData").json()
        assert body["moreData"] == "hi"

    def test_unknown_still_rejected(self, client):
        assert client.get("/x?include=bogus").status_code == 422


# ---------------------------------------------------------------------------
# Schema-graph edge cases
# ---------------------------------------------------------------------------
class TestSelfReferentialSchema:
    def test_self_recursive_includable(self):
        class CommentSchema(DynamicSchema):
            id: int
            body: str
            replies: Includable[List["CommentSchema"]]

        CommentSchema.model_rebuild()

        api = NinjaAPI(urls_namespace="ec-self")

        @api.get("/c", response=CommentSchema)
        @dynamic_response
        def gc(request):
            return {
                "id": 1,
                "body": "top",
                "replies": [{"id": 2, "body": "reply", "replies": []}],
            }

        c = TestClient(api)
        assert c.get("/c").json() == {"id": 1, "body": "top"}
        deep = c.get("/c?include=replies.replies").json()
        assert deep["replies"][0]["replies"] == []


class TestTwoSchemaCycle:
    def test_cycle_does_not_infinite_loop(self):
        class A(DynamicSchema):
            id: int
            name: str
            posts: Includable[List["B"]]

        class B(DynamicSchema):
            id: int
            title: str
            author: Includable[A]

        A.model_rebuild()
        B.model_rebuild()

        api = NinjaAPI(urls_namespace="ec-cycle")

        @api.get("/a", response=A)
        @dynamic_response
        def ga(request):
            return {
                "id": 1,
                "name": "Alice",
                "posts": [{"id": 10, "title": "P", "author": {"id": 1, "name": "Alice"}}],
            }

        c = TestClient(api)
        # Walking the graph for OpenAPI must terminate.
        sch = api.get_openapi_schema(path_prefix="/api/")
        assert "/api/a" in sch["paths"]
        # Default response hides includables.
        assert c.get("/a").json() == {"id": 1, "name": "Alice"}
        # Include via dot-path works.
        body = c.get("/a?include=posts.author").json()
        assert body["posts"][0]["author"]["name"] == "Alice"


class TestAllIncludableSchema:
    def test_default_response_is_empty(self):
        class S(DynamicSchema):
            a: Includable[int]
            b: Includable[str]

        api = NinjaAPI(urls_namespace="ec-all-incl")

        @api.get("/s", response=S)
        @dynamic_response
        def gs(request):
            return {"a": 1, "b": "x"}

        c = TestClient(api)
        assert c.get("/s").json() == {}
        assert c.get("/s?include=a").json() == {"a": 1}
        assert c.get("/s?include=a,b").json() == {"a": 1, "b": "x"}

    def test_openapi_omits_fields_param_when_no_default_visible(self):
        class S(DynamicSchema):
            a: Includable[int]

        api = NinjaAPI(urls_namespace="ec-all-incl-doc")

        @api.get("/s", response=S)
        @dynamic_response
        def gs(request):
            return {"a": 1}

        sch = api.get_openapi_schema(path_prefix="/api/")
        names = [p["name"] for p in sch["paths"]["/api/s"]["get"]["parameters"]]
        assert "fields" not in names
        assert "include" in names


class TestPlainSchemaInsideDynamicSchema:
    def test_plain_nested_default_visible(self):
        class Plain(Schema):
            x: int
            y: str

        class Outer(DynamicSchema):
            id: int
            plain_data: Plain
            opt_plain: Includable[Plain]

        api = NinjaAPI(urls_namespace="ec-mixed")

        @api.get("/o", response=Outer)
        @dynamic_response
        def go(request):
            return {"id": 1, "plain_data": {"x": 1, "y": "a"}, "opt_plain": {"x": 2, "y": "b"}}

        c = TestClient(api)
        # plain_data appears by default (not Includable), opt_plain does not.
        body = c.get("/o").json()
        assert body == {"id": 1, "plain_data": {"x": 1, "y": "a"}}
        body = c.get("/o?include=opt_plain").json()
        assert body["opt_plain"] == {"x": 2, "y": "b"}


class TestMultiLevelInheritance:
    def test_grandparent_includables_inherit(self):
        class GP(DynamicSchema):
            a: Includable[int]

        class Par(GP):
            b: Includable[str]

        class Child(Par):
            c: Includable[bool]

        assert Child.__dynamic_meta__.includable == {"a", "b", "c"}

        api = NinjaAPI(urls_namespace="ec-inherit")

        @api.get("/c", response=Child)
        @dynamic_response
        def gc(request):
            return {"a": 1, "b": "x", "c": True}

        c = TestClient(api)
        assert c.get("/c").json() == {}
        assert c.get("/c?include=a,b,c").json() == {"a": 1, "b": "x", "c": True}


class TestIncludableDictContainer:
    def test_dict_includable_is_returned_whole(self):
        class S(DynamicSchema):
            id: int
            config: Includable[Dict[str, int]]

        api = NinjaAPI(urls_namespace="ec-dict")

        @api.get("/x", response=S)
        @dynamic_response
        def gx(request):
            return {"id": 1, "config": {"a": 1, "b": 2}}

        c = TestClient(api)
        assert c.get("/x?include=config").json() == {"id": 1, "config": {"a": 1, "b": 2}}

    def test_dot_path_through_dict_is_rejected(self):
        class S(DynamicSchema):
            id: int
            config: Includable[Dict[str, int]]

        api = NinjaAPI(urls_namespace="ec-dict-deep")

        @api.get("/x", response=S)
        @dynamic_response
        def gx(request):
            return {"id": 1, "config": {"a": 1}}

        c = TestClient(api)
        assert c.get("/x?include=config.a").status_code == 422


class TestSchemaReusedAcrossEndpoints:
    def test_two_endpoints_same_schema_have_independent_state(self):
        class S(DynamicSchema):
            id: int
            opt: Includable[str]

        api = NinjaAPI(urls_namespace="ec-reuse")

        @api.get("/a", response=S)
        @dynamic_response
        def ga(request):
            return {"id": 1, "opt": "A"}

        @api.get("/b", response=S)
        @dynamic_response
        def gb(request):
            return {"id": 2, "opt": "B"}

        c = TestClient(api)
        assert c.get("/a").json() == {"id": 1}
        assert c.get("/b").json() == {"id": 2}
        assert c.get("/a?include=opt").json() == {"id": 1, "opt": "A"}


# ---------------------------------------------------------------------------
# Parser / runtime edge cases
# ---------------------------------------------------------------------------
class TestRepeatedQueryParams:
    @pytest.fixture
    def client(self):
        class S(DynamicSchema):
            id: int
            name: str
            bio: Includable[str]

        api = NinjaAPI(urls_namespace="ec-repeat")

        @api.get("/x", response=S)
        @dynamic_response
        def gx(request):
            return {"id": 1, "name": "A", "bio": "b"}

        return TestClient(api)

    def test_repeated_include_dedupes(self, client):
        assert client.get("/x?include=bio&include=bio").json() == {
            "id": 1, "name": "A", "bio": "b"
        }

    def test_repeated_fields_accumulates(self, client):
        assert client.get("/x?fields=id&fields=name").json() == {"id": 1, "name": "A"}


class TestWhitespace:
    def test_whitespace_around_values_is_stripped(self):
        class S(DynamicSchema):
            id: int
            name: str
            bio: Includable[str]

        api = NinjaAPI(urls_namespace="ec-ws")

        @api.get("/x", response=S)
        @dynamic_response
        def gx(request):
            return {"id": 1, "name": "A", "bio": "b"}

        c = TestClient(api)
        assert c.get("/x?fields= name , id ").json() == {"id": 1, "name": "A"}
        assert c.get("/x?include= bio ").json() == {"id": 1, "name": "A", "bio": "b"}


class TestStrictUnknownFalse:
    @pytest.fixture
    def client(self):
        class S(DynamicSchema):
            id: int
            name: str
            bio: Includable[str]

        api = NinjaAPI(urls_namespace="ec-loose")

        @api.get("/x", response=S)
        @dynamic_response(config=DynamicConfig(strict_unknown=False))
        def gx(request):
            return {"id": 1, "name": "A", "bio": "b"}

        return TestClient(api)

    def test_unknown_field_silently_dropped(self, client):
        assert client.get("/x?fields=name,bogus").json() == {"name": "A"}

    def test_unknown_include_silently_dropped(self, client):
        body = client.get("/x?include=bio,unknown").json()
        assert body["bio"] == "b"

    def test_includable_in_fields_silently_dropped(self, client):
        # bio is includable; with strict_unknown=False, passing it in
        # ?fields= is silently ignored rather than 422'd.
        assert client.get("/x?fields=name,bio").json() == {"name": "A"}


class TestMultipleHttpMethodsOnSamePath:
    def test_get_and_post_have_independent_dynamic_state(self):
        class S(DynamicSchema):
            id: int
            name: str
            bio: Includable[str]

        api = NinjaAPI(urls_namespace="ec-multi-method")

        @api.get("/x", response=S)
        @dynamic_response
        def get_x(request):
            return {"id": 1, "name": "G", "bio": "g"}

        @api.post("/x", response=S)
        @dynamic_response
        def post_x(request):
            return {"id": 2, "name": "P", "bio": "p"}

        c = TestClient(api)
        assert c.get("/x?fields=name").json() == {"name": "G"}
        assert c.post("/x?fields=name").json() == {"name": "P"}
        assert c.get("/x?include=bio").json()["bio"] == "g"


class TestReturnHttpResponseBypassesFilter:
    def test_direct_httpresponse_skips_dynamic(self):
        """
        Returning an HttpResponse short-circuits django-ninja's response
        pipeline — including our dynamic filter. This is the intended
        escape hatch for views that need to return raw bytes.
        """
        class S(DynamicSchema):
            id: int
            name: str

        api = NinjaAPI(urls_namespace="ec-raw")

        @api.get("/r", response=S)
        @dynamic_response
        def gr(request):
            return JsonResponse({"id": 1, "name": "raw", "extra": "kept"})

        r = TestClient(api).get("/r?fields=name")
        # The filter does not apply; raw body is returned verbatim.
        assert r.status_code == 200
        assert r.json() == {"id": 1, "name": "raw", "extra": "kept"}


class TestReturnNone:
    def test_none_renders_as_null(self):
        class S(DynamicSchema):
            id: int
            bio: Includable[str]

        api = NinjaAPI(urls_namespace="ec-none")

        @api.get("/n", response=Optional[S])
        @dynamic_response
        def gn(request):
            return None

        c = TestClient(api)
        assert c.get("/n").json() is None
        assert c.get("/n?fields=id").json() is None
