from dataclasses import dataclass, field
from typing import Literal, Tuple

SyntaxStyle = Literal["flat", "jsonapi"]


@dataclass(frozen=True)
class DynamicConfig:
    """
    Configuration for dynamic-schema query-parameter parsing.

    Resolved per-operation in this order: explicit decorator arg → Router → NinjaAPI.
    """

    style: SyntaxStyle = "flat"

    fields_param: str = "fields"
    omit_param: str = "omit"
    include_param: str = "include"
    expand_param: str = "expand"

    separator: str = ","

    strict_unknown: bool = True

    jsonapi_resource_aliases: Tuple[Tuple[str, str], ...] = field(default_factory=tuple)


DEFAULT_CONFIG = DynamicConfig()
