"""
Verify ?fields / ?omit / ?include / ?expand cooperate with fields whose
value comes from a ``resolve_<name>`` static method on the schema rather
than from a Django model attribute or the view's return value.

Resolver / dynamic interaction — the contract these tests pin down:

* Resolvers run during ``model_validate``; the dynamic include/exclude is
  applied at ``model_dump`` time. So Includable / Expandable fields backed
  by a resolver still have their resolver invoked even when the field is
  not requested. What the four query parameters control is whether that
  computed value appears in the *response*, not whether the resolver runs.
* ``?fields=`` and ``?omit=`` reliably gate top-level resolver fields from
  the response payload.
* Strict-unknown validation accepts resolver field names the same way it
  accepts plain-annotated names — it reads from ``Schema.model_fields``,
  which the metaclass populates from the annotation regardless of whether
  the value comes from data or a resolver.
"""
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


# Counters incremented on every resolver invocation; reset per test via fixture.
RESOLVER_CALLS: dict = {"name_loud": 0, "bio_summary": 0, "vip": 0, "tagline": 0}


class ProfileSchema(DynamicSchema):
    id: int
    name: str
    tagline: Expandable[str] = None

    @staticmethod
    def resolve_tagline(obj):
        RESOLVER_CALLS["tagline"] += 1
        return f"profile-of-{obj['name']}"


class UserSchema(DynamicSchema):
    id: int
    name: str
    email: str

    name_loud: str = ""
    bio_summary: str = ""

    vip: Includable[bool] = None
    profile: Includable[ProfileSchema] = None

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


def test_default_response_runs_all_resolvers(client):
    """
    With no sparse selection, every resolver runs and every field
    appears — including Includable / Expandable fields. The markers
    declare what *can* be filtered, not what is filtered by default.
    """
    body = client.get("/u").json()
    assert body["name_loud"] == "ALICE"
    assert body["bio_summary"].startswith("hello world")
    # Includable resolver field — value present (resolver ran) because no
    # sparse filter is in effect.
    assert body["vip"] is True
    assert RESOLVER_CALLS["name_loud"] == 1
    assert RESOLVER_CALLS["bio_summary"] == 1
    assert RESOLVER_CALLS["vip"] == 1


def test_sparse_keeps_resolver_field_when_listed(client):
    body = client.get("/u?fields=name,name_loud").json()
    assert body == {"name": "Alice", "name_loud": "ALICE"}


def test_sparse_drops_resolver_field_when_not_listed(client):
    body = client.get("/u?fields=name,email").json()
    assert "name_loud" not in body
    assert "bio_summary" not in body
    assert "vip" not in body
    # The dynamic system filters at dump time only; resolvers may still
    # run during validation. Document the public contract: the field is
    # absent from the response payload.


def test_omit_drops_resolver_field(client):
    body = client.get("/u?omit=name_loud,bio_summary,vip").json()
    assert "name_loud" not in body
    assert "bio_summary" not in body
    assert "vip" not in body
    assert body["name"] == "Alice"


def test_sparse_with_includable_resolver_appears_when_requested(client):
    body = client.get("/u?fields=name,vip").json()
    assert body == {"name": "Alice", "vip": True}


def test_sparse_with_includable_resolver_absent_when_not_requested(client):
    # vip is not in the sparse set, so it must not appear even though it has
    # a resolver and a default of None.
    body = client.get("/u?fields=name,email").json()
    assert "vip" not in body


def test_sparse_drives_expand_on_nested_resolver_field(client):
    """
    Sparse selection at the root makes the nested schema recurse along the
    expand path. Verify that the nested resolver (``tagline``) appears only
    when ``?expand=profile.tagline`` is requested.
    """
    body = client.get(
        "/u?fields=name,profile&include=profile&expand=profile.tagline"
    ).json()
    assert "tagline" in body["profile"]
    assert body["profile"]["tagline"] == "profile-of-Alice"
    assert RESOLVER_CALLS["tagline"] == 1


def test_resolver_receives_request_context(client):
    body = client.get("/u").json()
    assert body["bio_summary"].endswith("[GET]")


def test_list_response_sparse_filters_resolver_field_per_item(client):
    body = client.get("/us?fields=name_loud").json()
    assert body == [{"name_loud": "ALICE"}, {"name_loud": "BOB"}]


def test_sparse_validation_recognizes_resolver_field_name(client):
    # name_loud is a real annotated field (with a resolver), so the strict
    # validator must accept it just like a data-driven field name.
    r = client.get("/u?fields=name_loud")
    assert r.status_code == 200
    # An unknown name still 422s.
    r = client.get("/u?fields=name_loud,bogus_field")
    assert r.status_code == 422


def test_include_unknown_resolver_includable_rejected(client):
    assert client.get("/u?include=vip").status_code == 200
    assert client.get("/u?include=unknown").status_code == 422


def test_expand_path_validates_against_schema_graph(client):
    # profile.tagline is declared Expandable on the nested schema — accepted.
    r = client.get("/u?fields=name,profile&include=profile&expand=profile.tagline")
    assert r.status_code == 200


def test_omit_with_resolver_does_not_emit_field_in_list_response(client):
    body = client.get("/us?omit=email,bio_summary").json()
    for item in body:
        assert "email" not in item
        assert "bio_summary" not in item
        assert item["name_loud"].isupper()
