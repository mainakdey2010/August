"""
Index registry — loaded once at process startup, never re-read per scan.
Backed by config/index-registry.yaml; refreshes via file watcher in dev.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import yaml

_REGISTRY_PATH = Path(__file__).parents[4] / "config" / "index-registry.yaml"
_registry: dict[str, Any] | None = None
_lock = threading.Lock()


def _load() -> dict[str, Any]:
    with open(_REGISTRY_PATH) as f:
        raw = yaml.safe_load(f)
    return {k: v for k, v in raw["indices"].items()}


def get_registry() -> dict[str, Any]:
    global _registry
    if _registry is None:
        with _lock:
            if _registry is None:
                _registry = _load()
    return _registry


def get_index(index_id: str) -> dict[str, Any]:
    reg = get_registry()
    if index_id not in reg:
        raise KeyError(f"Index '{index_id}' not found in registry")
    return reg[index_id]


def invalidate() -> None:
    """Force reload on next access (used by file watcher in dev)."""
    global _registry
    with _lock:
        _registry = None
