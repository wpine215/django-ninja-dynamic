from typing import List

import pytest

from ninja import (
    DynamicSchema,
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
    author: Includable[AuthorSchema]


class UserSchema(DynamicSchema):
    id: int
    name: str
    email: str
    posts: Includable[List[PostSchema]]


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


def test_flat_emits_fields_and_include_params(flat_api):
    params = _params_by_name(flat_api, "/api/users/{id}")
    assert {"fields", "include"}.issubset(params)


def test_flat_no_omit_or_expand_params(flat_api):
    params = _params_by_name(flat_api, "/api/users/{id}")
    assert "omit" not in params
    assert "expand" not in params


def test_flat_fields_lists_default_visible_only(flat_api):
    params = _params_by_name(flat_api, "/api/users/{id}")
    desc = params["fields"]["description"]
    # Default-visible fields appear
    assert "id" in desc
    assert "name" in desc
    assert "email" in desc
    # Includable fields do NOT appear in the fields description
    assert "posts" not in desc.split("Available:")[-1]


def test_flat_fields_description_directs_to_include(flat_api):
    params = _params_by_name(flat_api, "/api/users/{id}")
    desc = params["fields"]["description"]
    assert "include" in desc.lower()


def test_flat_include_lists_includable_dot_paths(flat_api):
    params = _params_by_name(flat_api, "/api/users/{id}")
    desc = params["include"]["description"]
    assert "posts" in desc
    assert "posts.author" in desc


def test_jsonapi_emits_per_resource_brackets(jsonapi_api):
    params = _params_by_name(jsonapi_api, "/api/users/{id}")
    assert "fields[user]" in params
    assert "fields[post]" in params
    assert "fields[author]" in params


def test_jsonapi_include_present(jsonapi_api):
    params = _params_by_name(jsonapi_api, "/api/users/{id}")
    assert "include" in params
    assert "posts.author" in params["include"]["description"]


def test_non_dynamic_endpoint_has_no_dynamic_params():
    api = NinjaAPI(urls_namespace="no-dyn-doc")

    @api.get("/plain", response=UserSchema)
    def plain(request):
        return {}

    op = api.get_openapi_schema(path_prefix="/api/")["paths"]["/api/plain"]["get"]
    names = {p["name"] for p in op["parameters"]}
    assert "fields" not in names
    assert "include" not in names


def test_response_schema_preserved_in_openapi(flat_api):
    schema = flat_api.get_openapi_schema(path_prefix="/api/")
    op = schema["paths"]["/api/users/{id}"]["get"]
    content = op["responses"][200]["content"]
    assert "application/json" in content
