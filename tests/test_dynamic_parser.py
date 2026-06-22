from django.http import QueryDict

from ninja.dynamic.config import DynamicConfig
from ninja.dynamic.parser import parse_query


def _qd(qs: str) -> QueryDict:
    return QueryDict(qs)


def test_flat_fields_simple():
    sel = parse_query(_qd("fields=name,email"), DynamicConfig(style="flat"))
    assert sel.sparse == {None: {"name", "email"}}
    assert not sel.includes


def test_flat_include_single():
    sel = parse_query(_qd("include=posts"), DynamicConfig(style="flat"))
    assert sel.includes == {("posts",)}


def test_flat_include_with_dot_paths():
    sel = parse_query(
        _qd("include=posts.author,posts.author.bio,comments"),
        DynamicConfig(style="flat"),
    )
    assert sel.includes == {
        ("posts", "author"),
        ("posts", "author", "bio"),
        ("comments",),
    }


def test_flat_repeated_params_concatenate():
    sel = parse_query(_qd("fields=a&fields=b,c"), DynamicConfig(style="flat"))
    assert sel.sparse == {None: {"a", "b", "c"}}


def test_flat_empty_pieces_ignored():
    sel = parse_query(_qd("fields=a,,b,"), DynamicConfig(style="flat"))
    assert sel.sparse == {None: {"a", "b"}}


def test_flat_combined_fields_and_include():
    sel = parse_query(
        _qd("fields=name&include=posts.author"),
        DynamicConfig(style="flat"),
    )
    assert sel.sparse == {None: {"name"}}
    assert sel.includes == {("posts", "author")}


def test_jsonapi_per_resource_sparse():
    sel = parse_query(
        _qd("fields[user]=name,email&fields[post]=title"),
        DynamicConfig(style="jsonapi"),
    )
    assert sel.sparse == {"user": {"name", "email"}, "post": {"title"}}


def test_jsonapi_include_with_dot_paths():
    sel = parse_query(
        _qd("include=posts.author,comments"),
        DynamicConfig(style="jsonapi"),
    )
    assert sel.includes == {("posts", "author"), ("comments",)}


def test_jsonapi_resource_alias_remapping():
    cfg = DynamicConfig(
        style="jsonapi", jsonapi_resource_aliases=(("u", "user"),)
    )
    sel = parse_query(_qd("fields[u]=name"), cfg)
    assert sel.sparse == {"user": {"name"}}


def test_custom_separator_and_param_names():
    cfg = DynamicConfig(
        style="flat",
        fields_param="select",
        include_param="with",
        separator="|",
    )
    sel = parse_query(_qd("select=a|b&with=d|e.f"), cfg)
    assert sel.sparse == {None: {"a", "b"}}
    assert sel.includes == {("d",), ("e", "f")}
