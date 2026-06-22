from typing import List

from ninja import DynamicSchema, Includable, Schema
from ninja.dynamic.selector import (
    FieldSelector,
    build_include,
    schema_resource_name,
)


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
    posts: Includable[List[PostSchema]]


def test_resource_name_strips_schema_suffix():
    assert schema_resource_name(UserSchema) == "user"
    assert schema_resource_name(AuthorSchema) == "author"


def test_resource_name_snake_cases_camel_case():
    class BlogPost(Schema):
        x: int

    assert schema_resource_name(BlogPost) == "blog_post"


def test_default_includable_fields_hidden():
    """No selector input: the default-visible set is emitted, includables dropped."""
    sel = FieldSelector()
    out = build_include(sel, UserSchema, is_list_response=False)
    assert out == {"response": {"id": True, "name": True, "email": True}}


def test_default_with_no_includables_returns_none():
    """A schema with no Includable fields needs no filtering."""

    class Plain(Schema):
        a: int
        b: str

    sel = FieldSelector()
    out = build_include(sel, Plain, is_list_response=False)
    assert out is None


def test_sparse_only_filters_default_visible():
    sel = FieldSelector(sparse={None: {"name", "email"}})
    out = build_include(sel, UserSchema, is_list_response=False)
    assert out == {"response": {"name": True, "email": True}}


def test_sparse_drops_includable_in_safety_net():
    """
    Validator rejects includable in ?fields= at 422, but as a defense in
    depth the selector also drops them.
    """
    sel = FieldSelector(sparse={None: {"name", "posts"}})
    out = build_include(sel, UserSchema, is_list_response=False)
    assert out == {"response": {"name": True}}


def test_include_top_level_brings_in_default_visible_plus():
    sel = FieldSelector(includes={("posts",)})
    out = build_include(sel, UserSchema, is_list_response=False)
    assert out == {
        "response": {
            "id": True,
            "name": True,
            "email": True,
            "posts": {"__all__": {"id": True, "title": True, "body": True}},
        }
    }


def test_include_dot_path_descends():
    sel = FieldSelector(includes={("posts", "author")})
    out = build_include(sel, UserSchema, is_list_response=False)
    assert out == {
        "response": {
            "id": True,
            "name": True,
            "email": True,
            "posts": {
                "__all__": {
                    "id": True,
                    "title": True,
                    "body": True,
                    "author": {"id": True, "name": True},
                }
            },
        }
    }


def test_sparse_and_include_compose():
    sel = FieldSelector(
        sparse={None: {"name"}}, includes={("posts",)}
    )
    out = build_include(sel, UserSchema, is_list_response=False)
    assert out == {
        "response": {
            "name": True,
            "posts": {"__all__": {"id": True, "title": True, "body": True}},
        }
    }


def test_sparse_list_uses_all_key():
    sel = FieldSelector(sparse={None: {"name"}})
    out = build_include(sel, UserSchema, is_list_response=True)
    assert out == {"response": {"__all__": {"name": True}}}


def test_jsonapi_per_resource_sparse():
    sel = FieldSelector(
        sparse={"user": {"name", "posts"}, "post": {"title"}},
        includes={("posts",)},
    )
    out = build_include(sel, UserSchema, is_list_response=False)
    assert out == {
        "response": {
            "name": True,
            "posts": {"__all__": {"title": True}},
        }
    }


def test_fields_for_falls_back_to_unscoped_only_at_root():
    sel = FieldSelector(sparse={None: {"name"}})
    assert sel.fields_for(UserSchema, is_root=True) == frozenset({"name"})
    assert sel.fields_for(PostSchema, is_root=False) is None


def test_fields_for_prefers_resource_match_over_unscoped():
    sel = FieldSelector(sparse={None: {"x"}, "user": {"y"}})
    assert sel.fields_for(UserSchema, is_root=True) == frozenset({"y"})
    assert sel.fields_for(UserSchema, is_root=False) == frozenset({"y"})


def test_child_paths_at_root():
    sel = FieldSelector(includes={("posts",), ("posts", "author"), ("other",)})
    assert sel.child_paths_at(()) == {"posts", "other"}
    assert sel.child_paths_at(("posts",)) == {"author"}
    assert sel.child_paths_at(("missing",)) == set()
