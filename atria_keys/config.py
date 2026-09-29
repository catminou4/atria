"""YAML configuration loading with dotted-path access."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class Config:
    def __init__(self, raw: dict[str, Any], base_dir: Path):
        self._raw = raw
        self.base_dir = Path(base_dir)

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        path = Path(path)
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        return cls(raw, path.parent.parent if path.parent.name == "config" else path.parent)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], base_dir: str | Path) -> "Config":
        return cls(raw, Path(base_dir))

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def path(self, dotted: str, default: str) -> Path:
        """Config path resolved against the project base dir."""
        val = self.get(dotted, default)
        p = Path(val)
        return p if p.is_absolute() else self.base_dir / p
