"""Python port of Legado's fixed-history ``CustomUrl`` value object."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


_PARAM_PATTERN = re.compile(r"\s*,\s*(?=\{)")


@dataclass
class CustomUrl:
    _url: str
    _attributes: dict[str, Any] = field(default_factory=dict)

    def __init__(self, value: str):
        text = str(value)
        match = _PARAM_PATTERN.search(text)
        self._url = text if match is None else text[:match.start()]
        self._attributes = {}
        if match is not None:
            try:
                attributes = json.loads(text[match.end():])
            except (TypeError, json.JSONDecodeError):
                attributes = None
            if isinstance(attributes, dict):
                self._attributes.update(attributes)

    def put_attribute(self, key: str, value: Any) -> "CustomUrl":
        if value is None:
            self._attributes.pop(str(key), None)
        else:
            self._attributes[str(key)] = value
        return self

    def get_url(self) -> str:
        return self._url

    def get_attr(self) -> dict[str, Any]:
        return self._attributes

    def __str__(self) -> str:
        if not self._attributes:
            return self._url
        return self._url + "," + json.dumps(
            self._attributes, ensure_ascii=False, separators=(",", ":"))
