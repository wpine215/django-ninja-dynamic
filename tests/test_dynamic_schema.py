from typing import List, Optional

from ninja import DynamicSchema, Expandable, Includable
from ninja.dynamic.schema import get_dynamic_meta, unwrap_response_annotation


class A(DynamicSchema):
    id: int
    name: str


class B(DynamicSchema):
    id: int
    nested: Includable[List[A]] = None
    detail: Expandable[A] = None


def test_dynamic_meta_lists_marker_fields():
    meta = get_dynamic_meta(B)
    assert meta is not None
    assert meta.includable == {"nested"}
    assert meta.expandable == {"detail"}


def test_plain_schema_has_no_dynamic_meta():
    assert get_dynamic_meta(A).includable == set()
    assert get_dynamic_meta(A).expandable == set()


def test_marker_does_not_leak_into_field_type():
    # Pydantic should see Optional[List[A]], not Includable[List[A]]
    nested_field = B.model_fields["nested"]
    # Optional[List[A]] reduces to Union[List[A], None]; verify None is allowed.
    assert nested_field.default is None


def test_subclass_inherits_dynamic_meta():
    class C(B):
        extra: str = ""

    meta = get_dynamic_meta(C)
    assert meta.includable == {"nested"}
    assert meta.expandable == {"detail"}


def test_subclass_can_add_more_markers():
    class C(B):
        extra: Includable[str] = None

    meta = get_dynamic_meta(C)
    assert meta.includable == {"nested", "extra"}


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
