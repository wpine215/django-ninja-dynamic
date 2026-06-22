from typing import List

from ninja import DynamicSchema, Includable
from ninja.dynamic.schema import get_dynamic_meta, unwrap_response_annotation


class A(DynamicSchema):
    id: int
    name: str


class B(DynamicSchema):
    id: int
    nested: Includable[List[A]]
    detail: Includable[A]


def test_dynamic_meta_lists_includable_fields():
    meta = get_dynamic_meta(B)
    assert meta is not None
    assert meta.includable == {"nested", "detail"}


def test_plain_dynamic_schema_has_empty_meta():
    assert get_dynamic_meta(A).includable == set()


def test_marker_rewrites_annotation_to_optional_with_none_default():
    """User writes Includable[List[A]] without ``= None``; metaclass adds it."""
    nested_field = B.model_fields["nested"]
    assert nested_field.default is None


def test_user_supplied_default_is_preserved():
    """If the user passes an explicit default, it survives the rewrite."""

    class C(DynamicSchema):
        id: int
        items: Includable[List[A]] = []

    assert C.model_fields["items"].default == []


def test_subclass_inherits_dynamic_meta():
    class C(B):
        extra: str = ""

    meta = get_dynamic_meta(C)
    assert meta.includable == {"nested", "detail"}


def test_subclass_can_add_more_includables():
    class C(B):
        extra: Includable[str]

    meta = get_dynamic_meta(C)
    assert meta.includable == {"nested", "detail", "extra"}


def test_unwrap_response_annotation_handles_list():
    schema, is_list = unwrap_response_annotation(List[A])
    assert schema is A
    assert is_list is True


def test_unwrap_response_annotation_handles_plain():
    schema, is_list = unwrap_response_annotation(A)
    assert schema is A
    assert is_list is False


def test_unwrap_response_annotation_returns_none_for_non_schema():
    schema, is_list = unwrap_response_annotation(int)
    assert schema is None
    assert is_list is False
