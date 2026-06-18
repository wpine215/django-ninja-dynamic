"""
django-ninja-dynamic — Django Ninja fork with per-request dynamic response schemas.

Drop-in replacement for django-ninja. Adds opt-in ``?fields=``, ``?omit=``,
``?include=``, and ``?expand=`` query-parameter support via the
``@dynamic_response`` decorator and the ``DynamicSchema`` base class. See
``ninja.dynamic`` for details.
"""

__version__ = "1.6.2.dev0+dynamic.1"

from pydantic import Field

from ninja.files import UploadedFile
from ninja.filter_schema import FilterConfigDict, FilterLookup, FilterSchema
from ninja.main import NinjaAPI
from ninja.openapi.docs import Redoc, Swagger
from ninja.orm import ModelSchema
from ninja.params import (
    Body,
    BodyEx,
    Cookie,
    CookieEx,
    File,
    FileEx,
    Form,
    FormEx,
    Header,
    HeaderEx,
    P,
    Path,
    PathEx,
    Query,
    QueryEx,
)
from ninja.patch_dict import PatchDict
from ninja.responses import Status
from ninja.router import Router
from ninja.schema import Schema
from ninja.streaming import JSONL, SSE

# Dynamic-schema fork additions:
from ninja.dynamic import (  # noqa: E402
    DynamicConfig,
    DynamicSchema,
    Expandable,
    Includable,
    dynamic_response,
)

__all__ = [
    "Field",
    "UploadedFile",
    "NinjaAPI",
    "Body",
    "Cookie",
    "File",
    "Form",
    "Header",
    "Path",
    "Query",
    "BodyEx",
    "CookieEx",
    "FileEx",
    "FormEx",
    "HeaderEx",
    "PathEx",
    "QueryEx",
    "Router",
    "P",
    "Schema",
    "ModelSchema",
    "FilterSchema",
    "FilterLookup",
    "FilterConfigDict",
    "Swagger",
    "Redoc",
    "PatchDict",
    "SSE",
    "JSONL",
    "Status",
    "DynamicConfig",
    "DynamicSchema",
    "Expandable",
    "Includable",
    "dynamic_response",
]
