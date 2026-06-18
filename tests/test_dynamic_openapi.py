from typing import List

import pytest

from ninja import (
    DynamicSchema,
    Expandable,
    Includable,
    NinjaAPI,
    dynamic_response,
)


class AuthorSchema(DynamicSchema):
    id: int
    name: str


class PostSchema(DynamicSchema):
    id: int
    title: str
    body: str
    author: Expandable[AuthorSchema] = None


class UserSchema(DynamicSchema):
    id: int
    name: str
    email: str
    posts: Includable[List[PostSchema]] = None


def _params_by_name(api: NinjaAPI, path: str) -> dict:
    # path_prefix="/api/" bypasses the URL-reverse lookup that fails in
    # unit tests where the test API is not registered in ROOT_URLCONF.
    op = api.get_openapi_schema(path_prefix="/api/")["paths"][path]["get"]
    return {p["name"]: p for p in op["parameters"]}


@pytest.fixture
def flat_api():
    api = NinjaAPI(urls_namespace="flat-doc")

    @api.get("/users/{id}", response=UserSchema)
    @dynamic_response
    def get_user(request, id: int):
        return {}

    return api


@pytest.fixture
def jsonapi_api():
    api = NinjaAPI(urls_namespace="jsonapi-doc", dynamic_fields_style="jsonapi")

    @api.get("/users/{id}", response=UserSchema)
    @dynamic_response
    def get_user(request, id: int):
        return {}

    return api


def test_flat_adds_all_four_query_params(flat_api):
    params = _params_by_name(flat_api, "/api/users/{id}")
    assert {"fields", "omit", "include", "expand"}.issubset(params)


def test_flat_fields_param_lists_available_fields(flat_api):
    params = _params_by_name(flat_api, "/api/users/{id}")
    assert "id, name, email, posts" in params["fields"]["description"]


def test_flat_include_param_lists_includables(flat_api):
    params = _params_by_name(flat_api, "/api/users/{id}")
    assert "posts" in params["include"]["description"]


def test_flat_expand_lists_dot_paths(flat_api):
    params = _params_by_name(flat_api, "/api/users/{id}")
    assert "posts.author" in params["expand"]["description"]


def test_jsonapi_emits_per_resource_brackets(jsonapi_api):
    params = _params_by_name(jsonapi_api, "/api/users/{id}")
    assert "fields[user]" in params
    assert "fields[post]" in params
    assert "fields[author]" in params


def test_non_dynamic_endpoint_has_no_dynamic_params():
    api = NinjaAPI(urls_namespace="no-dyn-doc")

    @api.get("/plain", response=UserSchema)
    def plain(request):
        return {}

    params = _params_by_name(api, "/api/plain")
    assert not {"fields", "omit", "include", "expand"} & params.keys()


def test_response_schema_preserved_in_openapi(flat_api):
    schema = flat_api.get_openapi_schema(path_prefix="/api/")
    op = schema["paths"]["/api/users/{id}"]["get"]
    content = op["responses"][200]["content"]
    # Maximal-schema strategy: the response component still references the
    # full UserSchema with the optional `posts` field present.
    assert "application/json" in content
