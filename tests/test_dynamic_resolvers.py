"""
Verify ``?fields=`` / ``?include=`` cooperate with fields whose value comes
from a ``resolve_<name>`` static method on the schema rather than from a
Django model attribute or the view's return value.

Resolver / dynamic interaction — the contract these tests pin down:

* Resolvers run during ``model_validate``; the dynamic include filter is
  applied at ``model_dump`` time. An ``Includable`` resolver field still
  has its resolver invoked even when the field is not requested. What
  ``?fields=`` / ``?include=`` control is whether that computed value
  appears in the response, not whether the resolver runs.
* ``?fields=`` reliably gates default-visible resolver fields from the
  response payload.
* ``Includable`` resolver fields are absent unless ``?include=`` requests
  them — same as data-driven includable fields.
* Strict-unknown validation accepts resolver field names the same way as
  data-driven ones — it reads from ``Schema.model_fields``, which the
  metaclass populates from the annotation regardless of where the value
  comes from.
"""
from typing import List

import pytest

from ninja import (
    DynamicSchema,
    Includable,
    NinjaAPI,
    dynamic_response,
)
from ninja.testing import TestClient


# Counters incremented on every resolver invocation; reset per test via fixture.
RESOLVER_CALLS: dict = {"name_loud": 0, "bio_summary": 0, "vip": 0, "tagline": 0}


class ProfileSchema(DynamicSchema):
    id: int
    name: str
    tagline: Includable[str]

    @staticmethod
    def resolve_tagline(obj):
        RESOLVER_CALLS["tagline"] += 1
        return f"profile-of-{obj['name']}"


class UserSchema(DynamicSchema):
    id: int
    name: str
    email: str

    # Default-visible resolver fields
    name_loud: str = ""
    bio_summary: str = ""

    # Includable resolver fields
    vip: Includable[bool]
    profile: Includable[ProfileSchema]

    @staticmethod
    def resolve_name_loud(obj):
        RESOLVER_CALLS["name_loud"] += 1
        return str(obj["name"]).upper()

    @staticmethod
    def resolve_bio_summary(obj, context):
        RESOLVER_CALLS["bio_summary"] += 1
        request = context.get("request") if context else None
        suffix = f"[{request.method}]" if request is not None else ""
        return f"{obj['bio']}{suffix}"

    @staticmethod
    def resolve_vip(obj):
        RESOLVER_CALLS["vip"] += 1
        return obj["id"] == 1


def _payload():
    return {
        "id": 1,
        "name": "Alice",
        "email": "alice@example.com",
        "bio": "hello world",
        "profile": {"id": 99, "name": "Alice"},
    }


@pytest.fixture
def client():
    RESOLVER_CALLS.update({k: 0 for k in RESOLVER_CALLS})

    api = NinjaAPI(urls_namespace="dyn-resolvers")

    @api.get("/u", response=UserSchema)
    @dynamic_response
    def get_u(request):
        return _payload()

    @api.get("/us", response=List[UserSchema])
    @dynamic_response
    def list_u(request):
        return [_payload(), {**_payload(), "id": 2, "name": "Bob"}]

    return TestClient(api)


def test_default_response_emits_default_visible_resolvers_only(client):
    """
    By default: plain resolver fields (``name_loud``, ``bio_summary``)
    appear; Includable resolver fields (``vip``, ``profile``) do not.
    """
    body = client.get("/u").json()
    assert body["name_loud"] == "ALICE"
    assert body["bio_summary"].startswith("hello world")
    assert "vip" not in body
    assert "profile" not in body


def test_sparse_keeps_resolver_field_when_listed(client):
    body = client.get("/u?fields=name,name_loud").json()
    assert body == {"name": "Alice", "name_loud": "ALICE"}


def test_sparse_drops_resolver_field_when_not_listed(client):
    body = client.get("/u?fields=name,email").json()
    assert "name_loud" not in body
    assert "bio_summary" not in body


def test_sparse_rejects_includable_resolver_field(client):
    """
    Even though ``vip`` has a resolver, it's Includable; passing it in
    ``?fields=`` is a 422.
    """
    r = client.get("/u?fields=name,vip")
    assert r.status_code == 422
    assert "include" in r.json()["detail"][0]["msg"].lower()


def test_include_brings_in_includable_resolver_field(client):
    body = client.get("/u?include=vip").json()
    assert body["vip"] is True
    assert RESOLVER_CALLS["vip"] >= 1


def test_include_dot_path_to_nested_includable_resolver(client):
    body = client.get("/u?include=profile.tagline").json()
    assert body["profile"]["tagline"] == "profile-of-Alice"
    assert RESOLVER_CALLS["tagline"] >= 1


def test_resolver_receives_request_context(client):
    body = client.get("/u").json()
    assert body["bio_summary"].endswith("[GET]")


def test_list_response_sparse_filters_resolver_field_per_item(client):
    body = client.get("/us?fields=name_loud").json()
    assert body == [{"name_loud": "ALICE"}, {"name_loud": "BOB"}]


def test_list_response_default_hides_includable_resolver_fields(client):
    body = client.get("/us").json()
    for item in body:
        assert "vip" not in item
        assert "profile" not in item


def test_sparse_validation_recognizes_default_visible_resolver_name(client):
    """``name_loud`` is annotated on UserSchema (with a resolver) and is not
    Includable, so it must be accepted in ``?fields=`` just like any other
    default-visible field."""
    r = client.get("/u?fields=name_loud")
    assert r.status_code == 200
    r = client.get("/u?fields=name_loud,bogus_field")
    assert r.status_code == 422


def test_include_unknown_value_rejected(client):
    assert client.get("/u?include=vip").status_code == 200
    assert client.get("/u?include=unknown").status_code == 422
