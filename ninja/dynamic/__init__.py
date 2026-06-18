"""
ninja.dynamic — per-request dynamic schemas for django-ninja-dynamic.

Adds four query-parameter behaviors that reshape the response and its
OpenAPI documentation at request time:

* ``?fields=a,b``   — sparse fieldsets
* ``?omit=a``       — drop named fields
* ``?include=rel``  — pull in opt-in relations
* ``?expand=a.b``   — deep dot-path expansion

Two opt-in patterns are supported and may be used together:

* per-endpoint decorator: ``@dynamic_response(...)``
* schema base class: ``class UserSchema(DynamicSchema): ...`` with
  ``Includable[T]`` / ``Expandable[T]`` annotated fields

See ``ninja.dynamic.decorator`` for the decorator and
``ninja.dynamic.schema`` for the base class.
"""

from ninja.dynamic.config import DynamicConfig
from ninja.dynamic.decorator import dynamic_response
from ninja.dynamic.schema import DynamicSchema
from ninja.dynamic.selector import FieldSelector
from ninja.dynamic.types import Expandable, Includable

__all__ = [
    "DynamicConfig",
    "DynamicSchema",
    "Expandable",
    "FieldSelector",
    "Includable",
    "dynamic_response",
]
