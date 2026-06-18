from typing import List

import pytest

from ninja import (
    DynamicSchema,
    Expandable,
    Includable,
    NinjaAPI,
    dynamic_response,
)
from ninja.testing import TestClient


class AuthorSchema(DynamicSchema):
    id: int
    name: str
    bio: str


class PostSchema(DynamicSchema):
    id: int
    title: str
    body: str
    author: Expandable[AuthorSchema] = None


class UserSchema(DynamicSchema):
    id: int
    name: str
    email: str
    bio: str
    posts: Includable[List[PostSchema]] = None


def _payload(id: int = 1) -> dict:
    return {
        "id": id,
        "name": "Alice",
        "email": "alice@example.com",
        "bio": "hello",
        "posts": [
            {"id": 11, "title": "T1", "body": "B1",
             "author": {"id": 99, "name": "Bob", "bio": "bobbio"}}
        ],
    }


@pytest.fixture
def flat_client():
    api = NinjaAPI(urls_namespace="flat-deco")

    @api.get("/users/{id}", response=UserSchema)
    @dynamic_response
    def get_user(request, id: int):
        return _payload(id)

    @api.get("/users", response=List[UserSchema])
    @dynamic_response
    def list_users(request):
        return [_payload(1), _payload(2)]

    return TestClient(api)


@pytest.fixture
def jsonapi_client():
    api = NinjaAPI(urls_namespace="jsonapi-deco", dynamic_fields_style="jsonapi")

    @api.get("/users/{id}", response=UserSchema)
    @dynamic_response
    def get_user(request, id: int):
        return _payload(id)

    return TestClient(api)


def test_default_returns_all_default_fields(flat_client):
    r = flat_client.get("/users/1").json()
    assert set(r) >= {"id", "name", "email", "bio", "posts"}


def test_sparse_fieldset(flat_client):
    r = flat_client.get("/users/1?fields=name,email").json()
    assert r == {"name": "Alice", "email": "alice@example.com"}


def test_omit_drops_field(flat_client):
    r = flat_client.get("/users/1?omit=bio").json()
    assert "bio" not in r
    assert r["name"] == "Alice"


def test_include_brings_in_relation(flat_client):
    r = flat_client.get("/users/1?include=posts").json()
    assert isinstance(r["posts"], list)
    assert r["posts"][0]["title"] == "T1"


def test_expand_descends_into_relation(flat_client):
    r = flat_client.get("/users/1?include=posts&expand=posts.author").json()
    assert r["posts"][0]["author"]["name"] == "Bob"


def test_sparse_and_include_compose(flat_client):
    r = flat_client.get("/users/1?fields=name,posts&include=posts").json()
    assert set(r) == {"name", "posts"}
    assert r["posts"][0]["title"] == "T1"


def test_list_response_sparse_applies_per_item(flat_client):
    r = flat_client.get("/users?fields=id,name").json()
    assert len(r) == 2
    assert all(set(item) == {"id", "name"} for item in r)


def test_unknown_field_raises_validation_error(flat_client):
    r = flat_client.get("/users/1?fields=name,not_a_field")
    assert r.status_code == 422


def test_unknown_include_raises_validation_error(flat_client):
    r = flat_client.get("/users/1?include=bogus")
    assert r.status_code == 422


def test_fields_and_omit_together_raises(flat_client):
    r = flat_client.get("/users/1?fields=name&omit=bio")
    assert r.status_code == 422


def test_jsonapi_per_resource_sparse(jsonapi_client):
    r = jsonapi_client.get(
        "/users/1?fields%5Buser%5D=name,posts&fields%5Bpost%5D=title&include=posts"
    ).json()
    assert set(r) == {"name", "posts"}
    assert r["posts"][0] == {"title": "T1"}


def test_decorator_factory_with_explicit_includable():
    api = NinjaAPI(urls_namespace="explicit-deco")

    class Plain(DynamicSchema):
        id: int
        name: str
        meta: Includable[str] = None

    @api.get("/x", response=Plain)
    @dynamic_response(includable=["meta"])
    def view(request):
        return {"id": 1, "name": "x", "meta": "abc"}

    client = TestClient(api)
    assert client.get("/x?include=meta").json() == {"id": 1, "name": "x", "meta": "abc"}
    # Disallowed include is rejected
    assert client.get("/x?include=bogus").status_code == 422
