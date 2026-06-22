from typing import List

import pytest

from ninja import (
    DynamicSchema,
    Includable,
    NinjaAPI,
    dynamic_response,
)
from ninja.testing import TestClient


class AuthorSchema(DynamicSchema):
    id: int
    name: str
    bio: Includable[str]


class PostSchema(DynamicSchema):
    id: int
    title: str
    body: str
    author: Includable[AuthorSchema]


class UserSchema(DynamicSchema):
    id: int
    name: str
    email: str
    bio: Includable[str]
    posts: Includable[List[PostSchema]]


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


def test_default_response_hides_includable_fields(flat_client):
    """Without any ``?include=``, bio and posts are absent."""
    r = flat_client.get("/users/1").json()
    assert set(r) == {"id", "name", "email"}


def test_sparse_fieldset(flat_client):
    r = flat_client.get("/users/1?fields=name,email").json()
    assert r == {"name": "Alice", "email": "alice@example.com"}


def test_sparse_cannot_request_includable_field(flat_client):
    """``?fields=`` is orthogonal to ``?include=``; including an includable
    name in ``?fields=`` is a 422 with a helpful message."""
    r = flat_client.get("/users/1?fields=name,bio")
    assert r.status_code == 422
    msg = r.json()["detail"][0]["msg"]
    assert "includable" in msg.lower()
    assert "include" in msg.lower()


def test_include_brings_in_field(flat_client):
    r = flat_client.get("/users/1?include=bio").json()
    assert r["bio"] == "hello"
    # Default fields still present.
    assert "name" in r
    # Other includables stay hidden.
    assert "posts" not in r


def test_include_with_dot_path_descends(flat_client):
    r = flat_client.get("/users/1?include=posts.author").json()
    assert r["posts"][0]["author"]["name"] == "Bob"
    # author.bio is also includable, so should remain hidden.
    assert "bio" not in r["posts"][0]["author"]


def test_include_deeper_dot_path(flat_client):
    r = flat_client.get("/users/1?include=posts.author.bio").json()
    assert r["posts"][0]["author"]["bio"] == "bobbio"


def test_sparse_and_include_compose_orthogonally(flat_client):
    """``?fields=name&include=bio`` yields exactly {"name", "bio"}."""
    r = flat_client.get("/users/1?fields=name&include=bio").json()
    assert r == {"name": "Alice", "bio": "hello"}


def test_list_response_sparse_applies_per_item(flat_client):
    r = flat_client.get("/users?fields=id,name").json()
    assert len(r) == 2
    assert all(set(item) == {"id", "name"} for item in r)


def test_list_response_default_hides_includables(flat_client):
    r = flat_client.get("/users").json()
    for item in r:
        assert set(item) == {"id", "name", "email"}


def test_unknown_field_raises_validation_error(flat_client):
    r = flat_client.get("/users/1?fields=name,not_a_field")
    assert r.status_code == 422


def test_unknown_include_raises_validation_error(flat_client):
    r = flat_client.get("/users/1?include=bogus")
    assert r.status_code == 422


def test_unknown_dot_path_segment_raises(flat_client):
    # posts is a real includable; bogus underneath it is not.
    r = flat_client.get("/users/1?include=posts.bogus")
    assert r.status_code == 422


def test_jsonapi_per_resource_sparse(jsonapi_client):
    r = jsonapi_client.get(
        "/users/1?fields%5Buser%5D=name&fields%5Bpost%5D=title&include=posts"
    ).json()
    assert set(r) == {"name", "posts"}
    assert r["posts"][0] == {"title": "T1"}


def test_decorator_factory_with_explicit_includable():
    api = NinjaAPI(urls_namespace="explicit-deco")

    class Plain(DynamicSchema):
        id: int
        name: str
        meta: Includable[str]

    @api.get("/x", response=Plain)
    @dynamic_response(includable=["meta"])
    def view(request):
        return {"id": 1, "name": "x", "meta": "abc"}

    client = TestClient(api)
    assert client.get("/x").json() == {"id": 1, "name": "x"}
    assert client.get("/x?include=meta").json() == {"id": 1, "name": "x", "meta": "abc"}
    assert client.get("/x?include=bogus").status_code == 422
