from typing import List, Optional

from ninja import DynamicSchema, Expandable, Includable, Schema
from ninja.dynamic.selector import (
    FieldSelector,
    build_exclude,
    build_include,
    schema_resource_name,
)


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


def test_resource_name_strips_schema_suffix():
    assert schema_resource_name(UserSchema) == "user"
    assert schema_resource_name(AuthorSchema) == "author"


def test_resource_name_snake_cases_camel_case():
    class BlogPost(Schema):
        x: int

    assert schema_resource_name(BlogPost) == "blog_post"


def test_build_include_sparse_object():
    sel = FieldSelector(sparse={None: {"name", "email"}})
    out = build_include(sel, UserSchema, is_list_response=False)
    assert out == {"response": {"name": True, "email": True}}


def test_build_include_sparse_list_uses_all_key():
    sel = FieldSelector(sparse={None: {"name"}})
    out = build_include(sel, UserSchema, is_list_response=True)
    assert out == {"response": {"__all__": {"name": True}}}


def test_build_include_returns_none_when_no_sparse():
    sel = FieldSelector(includes={"posts"})
    out = build_include(sel, UserSchema, is_list_response=False)
    assert out is None


def test_build_include_recurses_along_expand_path():
    sel = FieldSelector(
        sparse={None: {"name", "posts"}},
        expands={("posts", "author")},
    )
    out = build_include(sel, UserSchema, is_list_response=False)
    assert out == {
        "response": {
            "name": True,
            "posts": {
                "__all__": {
                    "id": True,
                    "title": True,
                    "body": True,
                    "author": True,
                }
            },
        }
    }


def test_build_include_recurses_into_jsonapi_per_resource_sparse():
    sel = FieldSelector(
        sparse={"user": {"name", "posts"}, "post": {"title"}},
    )
    out = build_include(sel, UserSchema, is_list_response=False)
    assert out == {
        "response": {
            "name": True,
            "posts": {"__all__": {"title": True}},
        }
    }


def test_build_exclude_top_level_omit():
    sel = FieldSelector(omit={None: {"bio"}})
    out = build_exclude(sel, UserSchema, is_list_response=False)
    assert out == {"response": {"bio": True}}


def test_build_exclude_ignores_unknown_field_name():
    sel = FieldSelector(omit={None: {"not_a_field"}})
    assert build_exclude(sel, UserSchema, is_list_response=False) is None


def test_fields_for_falls_back_to_unscoped_only_at_root():
    sel = FieldSelector(sparse={None: {"name"}})
    assert sel.fields_for(UserSchema, is_root=True) == frozenset({"name"})
    # Flat sparse does not propagate into nested resources.
    assert sel.fields_for(PostSchema, is_root=False) is None


def test_fields_for_prefers_resource_match_over_unscoped():
    sel = FieldSelector(sparse={None: {"x"}, "user": {"y"}})
    assert sel.fields_for(UserSchema, is_root=True) == frozenset({"y"})
    assert sel.fields_for(UserSchema, is_root=False) == frozenset({"y"})
