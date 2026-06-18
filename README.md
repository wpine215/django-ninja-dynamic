# Django Ninja Dynamic

**Django Ninja Dynamic** is a fork of [Django Ninja](https://django-ninja.dev) that adds **per-request dynamic response schemas**. Clients shape what the server returns by passing query parameters such as `?fields=`, `?omit=`, `?include=`, and `?expand=`, with the resulting behavior accurately reflected in the auto-generated OpenAPI/Swagger documentation.

The package is a drop-in replacement: the import path remains `ninja`, so existing Django Ninja projects work unchanged.

**Key features inherited from Django Ninja:**

- **Easy**: Designed to be easy to use and intuitive.
- **FAST execution**: Very high performance thanks to **[Pydantic](https://pydantic-docs.helpmanual.io)** and **[async support](/docs/docs/guides/async-support.md)**.
- **Fast to code**: Type hints and automatic docs let you focus only on business logic.
- **Standards-based**: Based on the open standards for APIs: **OpenAPI** (previously known as Swagger) and **JSON Schema**.
- **Django friendly**: Good integration with the Django core and ORM.

**Additions in this fork:**

- **Sparse fieldsets** via `?fields=` and `?omit=`.
- **Optional relations** via `?include=` and **deep expansion** via `?expand=`.
- **`DynamicSchema` base class** with `Includable[T]` / `Expandable[T]` field markers.
- **`@dynamic_response` decorator** for per-endpoint opt-in.
- **Both flat and JSON:API** query-parameter syntax styles, configurable per API or per Router.
- **Automatic Django ORM optimization** (`select_related` / `prefetch_related`) driven by the request.
- **Accurate OpenAPI documentation** of the dynamic behavior (response schema plus the four query parameters with their valid values).

**Upstream documentation**: https://django-ninja.dev

---

## Installation

```
pip install django-ninja-dynamic
```

The package installs the `ninja` module, replacing any prior `django-ninja` installation in the environment.

## Basic usage

In your Django project, next to `urls.py`, create an `api.py` file:

```python
from ninja import NinjaAPI

api = NinjaAPI()


@api.get("/add")
def add(request, a: int, b: int):
    return {"result": a + b}
```

In `urls.py`:

```python
from .api import api

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", api.urls),
]
```

Interactive docs are available at `/api/docs`.

---

## Dynamic schemas

### Quick example

```python
from typing import List

from ninja import (
    NinjaAPI,
    DynamicSchema,
    Includable,
    Expandable,
    dynamic_response,
)


class AuthorSchema(DynamicSchema):
    id: int
    name: str


class PostSchema(DynamicSchema):
    id: int
    title: str
    body: str
    author: Expandable[AuthorSchema] = None


class UserSchema(DynamicSchema):
    id: int
    name: str
    email: str
    posts: Includable[List[PostSchema]] = None


api = NinjaAPI()


@api.get("/users/{id}", response=UserSchema)
@dynamic_response
def get_user(request, id: int):
    return User.objects.filter(pk=id)
```

Sample requests:

| Request | Response |
| --- | --- |
| `GET /api/users/1` | All default fields, with `posts: null`. |
| `GET /api/users/1?fields=name,email` | Only `name` and `email`. |
| `GET /api/users/1?omit=email` | All default fields except `email`. |
| `GET /api/users/1?include=posts` | Default fields plus the `posts` array. |
| `GET /api/users/1?include=posts&expand=posts.author` | As above, with each post's `author` populated. |

### Query parameters

| Parameter | Behavior |
| --- | --- |
| `fields` | Comma-separated list of fields to return. Acts as a sparse fieldset on the root response. |
| `omit` | Comma-separated list of fields to drop. Cannot be combined with `fields`. |
| `include` | Comma-separated list of opt-in relations declared `Includable[T]`. |
| `expand` | Comma-separated dot-paths declared `Expandable[T]`, e.g. `posts.author`. |

Unknown values raise an HTTP 422 validation error. Mixing `fields` and `omit` is rejected.

### `DynamicSchema`, `Includable`, and `Expandable`

Subclass `DynamicSchema` instead of `Schema` and annotate optional relations with `Includable[T]` or `Expandable[T]`. These markers are stripped before Pydantic sees the field; the field's runtime type becomes `Optional[T]`.

- **`Includable[T]`** declares an opt-in relation that the client requests with `?include=field_name`.
- **`Expandable[T]`** declares a nested field that the client reaches with `?expand=path.to.field`.

The discovered markers are exposed on the class as `__dynamic_meta__` and inherited by subclasses.

### `@dynamic_response`

The decorator wires the four query parameters into the operation, validates them against the response schema, and applies any requested ORM optimizations:

```python
@api.get("/users/{id}", response=UserSchema)
@dynamic_response
def get_user(request, id: int):
    ...
```

The decorator can also be used as a factory to set explicit lists or override config:

```python
@dynamic_response(
    includable=["posts"],
    expandable=["posts.author"],
    optimize_queryset=True,
    config=DynamicConfig(style="jsonapi"),
)
```

When the response schema is a `DynamicSchema`, `includable` and `expandable` default to the values declared via markers.

### JSON:API syntax style

The fork supports two query-parameter syntaxes:

| Style | Example |
| --- | --- |
| `flat` (default) | `?fields=name,email&include=posts` |
| `jsonapi` | `?fields[user]=name,email&fields[post]=title&include=posts` |

Configure the style on the API or Router:

```python
api = NinjaAPI(dynamic_fields_style="jsonapi")
router = Router(dynamic_fields_style="flat")
```

Precedence for resolving the config is: decorator argument > API setting > default (`flat`).

In `jsonapi` mode, `?include=posts.author` is automatically split into `includes={"posts"}` and `expands={("posts", "author")}`. Per-resource sparse fieldsets apply recursively wherever the matching resource appears in the response graph; flat `?fields=` applies only at the root.

### ORM optimization

When the response is a Django `QuerySet` and the schema is linked to a Django model (either via `ninja.ModelSchema`'s `Meta.model` or by setting `__django_model__` on a plain `DynamicSchema`), `?include=` and `?expand=` automatically attach `select_related` / `prefetch_related` to the queryset before it is evaluated.

Chains where every hop is a `ForeignKey` or `OneToOneField` are resolved with `select_related`; reverse and many-to-many relations use `prefetch_related`. The behavior can be disabled per endpoint with `@dynamic_response(optimize_queryset=False)`.

### OpenAPI documentation

The OpenAPI document renders the **maximal** response schema (every includable and expandable field is present and optional) and adds the four query parameters with descriptions listing the valid values. For example:

```yaml
parameters:
  - in: query
    name: fields
    description: "Sparse fieldset: comma-separated list of fields to return.
                  Available on top-level response: id, name, email, posts."
  - in: query
    name: include
    description: "Comma-separated list of optional relations to include.
                  Available: posts."
  - in: query
    name: expand
    description: "Comma-separated dot-paths to expand nested relations.
                  Available: posts.author."
```

In `jsonapi` mode the parameter list becomes `fields[user]`, `fields[post]`, etc., with one entry per resource discovered in the schema graph.

### Configuration reference

`DynamicConfig` controls parsing and naming:

| Field | Default | Description |
| --- | --- | --- |
| `style` | `"flat"` | `"flat"` or `"jsonapi"`. |
| `fields_param` | `"fields"` | Query-parameter name for the sparse fieldset. |
| `omit_param` | `"omit"` | Query-parameter name for the omit list. |
| `include_param` | `"include"` | Query-parameter name for include. |
| `expand_param` | `"expand"` | Query-parameter name for expand. |
| `separator` | `","` | Separator for list values within a single parameter. |
| `strict_unknown` | `True` | When True, unknown field names raise 422; when False, they are silently dropped. |
| `jsonapi_resource_aliases` | `()` | Tuple of `(alias, canonical)` pairs for `fields[alias]` rewriting. |

### Drop-in compatibility

Endpoints that do not use `@dynamic_response` or `DynamicSchema` behave exactly as in upstream Django Ninja. The new code paths are gated on the presence of a per-request selector, and the OpenAPI generator only emits the dynamic query parameters for operations that opt in.

---

## License

MIT, inherited from upstream Django Ninja.
