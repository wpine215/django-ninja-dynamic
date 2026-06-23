import re
from typing import Iterable, Mapping, Optional

from ninja.dynamic.config import DynamicConfig
from ninja.dynamic.selector import FieldSelector

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
    """
    if config.style == "flat":
        return _parse_flat(query, config)
    if config.style == "jsonapi":
        return _parse_jsonapi(query, config)
    raise ValueError(f"Unknown dynamic style: {config.style}")  # pragma: no cover


def _parse_flat(
    query: Mapping[str, object], config: DynamicConfig
) -> FieldSelector:
    sel = FieldSelector()
    sep = config.separator

    raw = _gather(query, config.fields_param)
    if raw is not None:
        # ``?fields=`` (no values) or ``?fields=,,,`` is treated as if the
        # parameter wasn't supplied — there's no use case for "return zero
        # fields" and silently doing so would surprise users.
        values = set(_split(raw, sep))
        if values:
            sel.sparse[None] = values

    # ``?include=`` accepts dot-paths in flat mode too. ``?include=posts``
    # opts the field in; ``?include=posts.author`` opts in and descends.
    raw = _gather(query, config.include_param)
    if raw is not None:
        sel.includes |= _parse_path_set(raw, sep)

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
        if param != config.fields_param:
            continue
        raw = _gather(query, key)
        if raw is None:
            continue
        values = set(_split(raw, sep))
        if not values:
            # Empty per-resource bucket — treat as if not supplied.
            continue
        sel.sparse.setdefault(resource, set()).update(values)

    # JSON:API ``include`` is dot-pathed: ``include=posts.author,comments``.
    raw = _gather(query, config.include_param)
    if raw is not None:
        sel.includes |= _parse_path_set(raw, sep)

    return sel
