import re
from typing import Iterable, Mapping, Optional, Tuple

from ninja.dynamic.config import DynamicConfig
from ninja.dynamic.selector import FieldSelector
from ninja.errors import ValidationError

_BRACKET_PARAM_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\[([A-Za-z0-9_]+)\]$")


def _split(raw: str, sep: str) -> Iterable[str]:
    for piece in raw.split(sep):
        piece = piece.strip()
        if piece:
            yield piece


def _gather(query: Mapping[str, object], name: str) -> Optional[str]:
    """
    Return the raw concatenated string for ``name``, or None if absent.

    Honors both repeated parameters (``?fields=a&fields=b``) and CSV
    (``?fields=a,b``). Django ``QueryDict`` exposes ``getlist``; plain dicts
    are also accepted (single-string value).
    """
    getlist = getattr(query, "getlist", None)
    if getlist is not None:
        vals = getlist(name)
    else:
        v = query.get(name)
        if v is None:
            return None
        vals = v if isinstance(v, list) else [v]
    if not vals:
        return None
    return ",".join(str(v) for v in vals)


def _parse_path_set(raw: str, sep: str) -> "set[tuple[str, ...]]":
    return {tuple(piece.split(".")) for piece in _split(raw, sep)}


def parse_query(
    query: Mapping[str, object], config: DynamicConfig
) -> FieldSelector:
    """
    Parse a Django ``QueryDict`` (or any string-keyed mapping) into a
    ``FieldSelector`` according to the configured syntax style.

    Raises ``ValidationError`` if ``fields`` and ``omit`` are both present,
    or if JSON:API style sparse uses an empty resource name.
    """
    if config.style == "flat":
        sel = _parse_flat(query, config)
    elif config.style == "jsonapi":
        sel = _parse_jsonapi(query, config)
    else:  # pragma: no cover
        raise ValueError(f"Unknown dynamic style: {config.style}")

    if sel.sparse and sel.omit:
        raise ValidationError([{
            "type": "value_error",
            "loc": ("query", config.fields_param),
            "msg": f"Use either '{config.fields_param}' or '{config.omit_param}', not both.",
        }])
    return sel


def _parse_flat(
    query: Mapping[str, object], config: DynamicConfig
) -> FieldSelector:
    sel = FieldSelector()
    sep = config.separator

    raw = _gather(query, config.fields_param)
    if raw is not None:
        sel.sparse[None] = set(_split(raw, sep))

    raw = _gather(query, config.omit_param)
    if raw is not None:
        sel.omit[None] = set(_split(raw, sep))

    raw = _gather(query, config.include_param)
    if raw is not None:
        sel.includes |= set(_split(raw, sep))

    raw = _gather(query, config.expand_param)
    if raw is not None:
        sel.expands |= _parse_path_set(raw, sep)

    return sel


def _parse_jsonapi(
    query: Mapping[str, object], config: DynamicConfig
) -> FieldSelector:
    sel = FieldSelector()
    sep = config.separator

    aliases = dict(config.jsonapi_resource_aliases)

    # JSON:API sparse: ``fields[resource]=a,b``
    for key in list(query.keys() if isinstance(query, dict) else query):
        m = _BRACKET_PARAM_RE.match(key)
        if not m:
            continue
        param, resource = m.group(1), m.group(2)
        resource = aliases.get(resource, resource)
        target: Optional[dict] = None
        if param == config.fields_param:
            target = sel.sparse
        elif param == config.omit_param:
            target = sel.omit
        if target is None:
            continue
        raw = _gather(query, key)
        if raw is None:
            continue
        target.setdefault(resource, set()).update(_split(raw, sep))

    # JSON:API ``include`` is dot-pathed: ``include=posts.author,comments``
    # We split into top-level ``includes`` and the rest as ``expands``.
    raw = _gather(query, config.include_param)
    if raw is not None:
        for path_str in _split(raw, sep):
            parts = tuple(path_str.split("."))
            sel.includes.add(parts[0])
            if len(parts) > 1:
                sel.expands.add(parts)

    # An explicit expand=... param remains supported in JSON:API mode for
    # symmetry with flat — it adds to expands without altering includes.
    raw = _gather(query, config.expand_param)
    if raw is not None:
        sel.expands |= _parse_path_set(raw, sep)

    return sel
