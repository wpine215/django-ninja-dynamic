# Django Ninja Dynamic

**Django Ninja Dynamic** is a fork of [Django Ninja](https://django-ninja.dev) that adds **per-request dynamic response schemas**. Clients shape what the server returns by passing two query parameters — `?fields=` and `?include=` — with the resulting behavior accurately reflected in the auto-generated OpenAPI/Swagger documentation.

The package is a drop-in replacement: the import path remains `ninja`, so existing Django Ninja projects work unchanged.

**Key features inherited from Django Ninja:**

- **Easy**: Designed to be easy to use and intuitive.
- **FAST execution**: Very high performance thanks to **[Pydantic](https://pydantic-docs.helpmanual.io)** and **[async support](/docs/docs/guides/async-support.md)**.
- **Fast to code**: Type hints and automatic docs let you focus only on business logic.
- **Standards-based**: Based on the open standards for APIs: **OpenAPI** (previously known as Swagger) and **JSON Schema**.
- **Django friendly**: Good integration with the Django core and ORM.

**Additions in this fork:**

- **`Includable[T]` field marker** — declares an opt-in field that is hidden from the default response and surfaced with `?include=`. Works for any type (not just FK relations).
- **Sparse fieldsets** via `?fields=` — an allowlist over default-visible fields.
- **Dot-path includes** via `?include=posts.author` — pull in a nested `Includable` field and descend into it in one parameter.
- **`@dynamic_response` decorator** for per-endpoint opt-in.
- **Both flat and JSON:API** query-parameter syntax styles, configurable per API.
- **Automatic Django ORM optimization** (`select_related` / `prefetch_related`) driven by `?include=`.
- **Accurate OpenAPI documentation** of the dynamic behavior: response schema plus the two query parameters with their valid values.

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
    dynamic_response,
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


api = NinjaAPI()


@api.get("/users/{id}", response=UserSchema)
@dynamic_response
def get_user(request, id: int):
    return User.objects.filter(pk=id)
```

Sample requests:

| Request | Response |
| --- | --- |
| `GET /api/users/1` | `id`, `name`, `email` only — `posts` is `Includable`, hidden by default. |
| `GET /api/users/1?fields=name,email` | Only `name` and `email`. |
| `GET /api/users/1?include=posts` | Default fields + the `posts` array. |
| `GET /api/users/1?include=posts.author` | Adds `posts`, with `author` populated on each. |
| `GET /api/users/1?include=posts.author.bio` | All three nested levels populated. |
| `GET /api/users/1?fields=name&include=posts` | `name` + `posts`. |
| `GET /api/users/1?fields=name,posts` | **HTTP 422** — `posts` is includable, use `?include=` instead. |

### Query parameters

| Parameter | Behavior |
| --- | --- |
| `fields` | Comma-separated allowlist over **default-visible** fields. May not include `Includable` fields — use `?include=` for those. |
| `include` | Comma-separated dot-paths over `Includable` fields. Each path segment must be `Includable` on its parent schema. |

The two parameters are **orthogonal**: `?fields=` filters defaults, `?include=` opts in. They compose without ambiguity.

Unknown values raise an HTTP 422 validation error.

### `DynamicSchema` and `Includable`

Subclass `DynamicSchema` instead of `Schema` and annotate opt-in fields with `Includable[T]`. The metaclass strips the marker, rewrites the annotation to `Optional[T]`, and injects a default of `None` — no boilerplate required:

```python
class UserSchema(DynamicSchema):
    id: int            # default-visible
    name: str          # default-visible
    posts: Includable[List[PostSchema]]   # hidden until ?include=posts
```

The discovered markers are exposed on the class as `__dynamic_meta__` and inherited by subclasses.

`Includable` works for any field type, not just FK relations. A scalar like `bio: Includable[str]` is just as valid as a nested `Includable[List[PostSchema]]`.

### `@dynamic_response`

The decorator wires the two query parameters into the operation, validates them against the response schema, and applies any requested ORM optimizations:

```python
@api.get("/users/{id}", response=UserSchema)
@dynamic_response
def get_user(request, id: int):
    ...
```

The decorator can also be called as a factory to override defaults:

```python
@dynamic_response(
    includable=["posts"],
    optimize_queryset=True,
    config=DynamicConfig(style="jsonapi"),
)
```

When the response schema is a `DynamicSchema`, `includable` defaults to the names declared via markers.

### JSON:API syntax style

The fork supports two query-parameter syntaxes:

| Style | Example |
| --- | --- |
| `flat` (default) | `?fields=name,email&include=posts.author` |
| `jsonapi` | `?fields[user]=name,email&fields[post]=title&include=posts.author` |

Configure the style on the API:

```python
api = NinjaAPI(dynamic_fields_style="jsonapi")
```

Precedence for resolving the config: decorator argument → API setting → default (`flat`).

Per-resource sparse fieldsets in `jsonapi` mode apply recursively wherever the matching resource appears in the response graph; flat `?fields=` applies only at the root.

### ORM optimization

When the response is a Django `QuerySet` and the schema is linked to a Django model (either via `ninja.ModelSchema`'s `Meta.model` or by setting `__django_model__` on a plain `DynamicSchema`), `?include=` automatically attaches `select_related` / `prefetch_related` to the queryset before it is evaluated.

Chains where every hop is a `ForeignKey` or `OneToOneField` are resolved with `select_related`; reverse and many-to-many relations use `prefetch_related`. The behavior can be disabled per endpoint with `@dynamic_response(optimize_queryset=False)`.

**Resolver-backed fields and reverse relations**

The optimizer keys off the **schema field name**, not the resolver's body. So `posts: Includable[List[PostSchema]]` will prefetch `posts` whether or not a `resolve_posts` method exists — as long as `posts` is a real reverse-relation name on the Django model.

If you name a resolver field differently from the ORM relation it reaches into — for example, a field `related_event` whose resolver does `obj.event` — the optimizer cannot infer the relationship and the request will N+1. You have two options:

1. Match the field name to the ORM reverse name (cleanest; the optimizer just works).
2. Pre-optimize the queryset in the view: `return Category.objects.prefetch_related("event")`. The fork's auto-optimization is skipped for unknown field names, so your manual call survives intact.

Scalar resolver-backed `Includable` fields (e.g. `Includable[str]` whose value is computed) are always skipped by the optimizer — there's nothing to prefetch.

### OpenAPI documentation

The OpenAPI document renders the **maximal** response schema (every `Includable` field is present and optional) and adds the two query parameters with descriptions listing the valid values. For example:

```yaml
parameters:
  - in: query
    name: fields
    description: "Sparse fieldset: comma-separated allowlist of fields
                  to return. Includable fields are not valid here; use
                  'include' for those. Available: id, name, email."
  - in: query
    name: include
    description: "Comma-separated list of opt-in fields to include.
                  Supports dot-paths (e.g. ``posts.author``) for
                  nested inclusion. Available: posts, posts.author,
                  posts.author.bio."
```

In `jsonapi` mode the `fields` parameter is replaced by per-resource entries (`fields[user]`, `fields[post]`, etc.), one for each schema discovered in the response graph.

### Configuration reference

`DynamicConfig` controls parsing and naming:

| Field | Default | Description |
| --- | --- | --- |
| `style` | `"flat"` | `"flat"` or `"jsonapi"`. |
| `fields_param` | `"fields"` | Query-parameter name for the sparse fieldset. |
| `include_param` | `"include"` | Query-parameter name for the include list. |
| `separator` | `","` | Separator for list values within a single parameter. |
| `strict_unknown` | `True` | When True, unknown field names raise 422; when False, they are silently dropped. |
| `jsonapi_resource_aliases` | `()` | Tuple of `(alias, canonical)` pairs for `fields[alias]` rewriting. |

### Drop-in compatibility

Endpoints that do not use `@dynamic_response` or `DynamicSchema` behave exactly as in upstream Django Ninja. The new code paths are gated on the presence of a per-request selector, and the OpenAPI generator only emits the dynamic query parameters for operations that opt in.

---

## License

MIT, inherited from upstream Django Ninja.
