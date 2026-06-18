from django.http import QueryDict

from ninja.dynamic.config import DynamicConfig
from ninja.dynamic.parser import parse_query
from ninja.errors import ValidationError

import pytest


def _qd(qs: str) -> QueryDict:
    return QueryDict(qs)


def test_flat_fields_simple():
    sel = parse_query(_qd("fields=name,email"), DynamicConfig(style="flat"))
    assert sel.sparse == {None: {"name", "email"}}
    assert not sel.omit
    assert not sel.includes
    assert not sel.expands


def test_flat_omit():
    sel = parse_query(_qd("omit=bio"), DynamicConfig(style="flat"))
    assert sel.omit == {None: {"bio"}}
    assert sel.sparse == {}


def test_flat_include_and_expand():
    sel = parse_query(_qd("include=posts,comments&expand=posts.author"), DynamicConfig(style="flat"))
    assert sel.includes == {"posts", "comments"}
    assert sel.expands == {("posts", "author")}


def test_flat_repeated_params_concatenate():
    sel = parse_query(_qd("fields=a&fields=b,c"), DynamicConfig(style="flat"))
    assert sel.sparse == {None: {"a", "b", "c"}}


def test_flat_empty_pieces_ignored():
    sel = parse_query(_qd("fields=a,,b,"), DynamicConfig(style="flat"))
    assert sel.sparse == {None: {"a", "b"}}


def test_flat_fields_and_omit_conflict_raises():
    with pytest.raises(ValidationError):
        parse_query(_qd("fields=a&omit=b"), DynamicConfig(style="flat"))


def test_jsonapi_per_resource_sparse():
    sel = parse_query(
        _qd("fields[user]=name,email&fields[post]=title"),
        DynamicConfig(style="jsonapi"),
    )
    assert sel.sparse == {"user": {"name", "email"}, "post": {"title"}}


def test_jsonapi_include_with_dot_path_splits_into_includes_and_expands():
    sel = parse_query(_qd("include=posts.author,comments"), DynamicConfig(style="jsonapi"))
    assert sel.includes == {"posts", "comments"}
    assert sel.expands == {("posts", "author")}


def test_jsonapi_explicit_expand_param_still_honored():
    sel = parse_query(_qd("expand=meta.flag"), DynamicConfig(style="jsonapi"))
    assert sel.expands == {("meta", "flag")}


def test_jsonapi_resource_alias_remapping():
    cfg = DynamicConfig(style="jsonapi", jsonapi_resource_aliases=(("u", "user"),))
    sel = parse_query(_qd("fields[u]=name"), cfg)
    assert sel.sparse == {"user": {"name"}}


def test_custom_separator_and_param_names():
    cfg = DynamicConfig(
        style="flat",
        fields_param="select",
        omit_param="drop",
        include_param="with",
        expand_param="deep",
        separator="|",
    )
    sel = parse_query(_qd("select=a|b&with=d&deep=e.f"), cfg)
    assert sel.sparse == {None: {"a", "b"}}
    assert sel.includes == {"d"}
    assert sel.expands == {("e", "f")}


def test_custom_separator_omit_only():
    cfg = DynamicConfig(style="flat", omit_param="drop", separator="|")
    sel = parse_query(_qd("drop=a|b"), cfg)
    assert sel.omit == {None: {"a", "b"}}
