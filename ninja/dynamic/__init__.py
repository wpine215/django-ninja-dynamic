"""
ninja.dynamic — per-request dynamic schemas for django-ninja-dynamic.

Adds two query-parameter behaviors that reshape the response and its
OpenAPI documentation at request time:

* ``?fields=a,b`` — sparse fieldset allowlist over the schema's
  default-visible fields. Cannot list ``Includable`` fields.
* ``?include=a,a.b`` — dot-paths opting ``Includable`` fields into the
  response, with optional deep inclusion.

Two opt-in patterns are supported and may be used together:

* per-endpoint decorator: ``@dynamic_response(...)``
* schema base class: ``class UserSchema(DynamicSchema): ...`` with
  ``Includable[T]`` annotated fields (hidden by default).

See ``ninja.dynamic.decorator`` for the decorator and
``ninja.dynamic.schema`` for the base class.
"""

from ninja.dynamic.config import DynamicConfig
from ninja.dynamic.decorator import dynamic_response
from ninja.dynamic.schema import DynamicSchema
from ninja.dynamic.selector import FieldSelector
from ninja.dynamic.types import Includable

__all__ = [
    "DynamicConfig",
    "DynamicSchema",
    "FieldSelector",
    "Includable",
    "dynamic_response",
]
