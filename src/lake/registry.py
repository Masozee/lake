"""Source registry: configs/sources.yaml -> instantiated scraper.

Adding a source is a YAML edit plus a package under lake/sources/. No change to
the scheduler, the CLI, or any dispatch table.
"""

from __future__ import annotations

import importlib
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from lake.core.exceptions import ConfigError
from lake.core.storage import Storage, default_storage
from lake.metadata.repo import MetadataRepo
from lake.settings import get_settings

_ENV_REF = re.compile(r"\$\{env:([A-Z_][A-Z0-9_]*)\}")


def _expand_env(value: Any) -> Any:
    """Resolve ${env:VAR} references so secrets stay out of the YAML."""
    if isinstance(value, str):

        def sub(m: re.Match[str]) -> str:
            var = m.group(1)
            resolved = os.environ.get(var)
            if resolved is None:
                raise ConfigError(f"{value!r} references ${{env:{var}}} but it is unset")
            return resolved

        return _ENV_REF.sub(sub, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        out[k] = (
            _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
        )
    return out


@lru_cache(maxsize=1)
def _load_raw(path_str: str) -> dict[str, dict[str, Any]]:
    path = Path(path_str)
    if not path.is_file():
        raise ConfigError(f"source registry not found: {path}")

    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    defaults = doc.get("defaults", {}) or {}
    sources = doc.get("sources", []) or []

    merged: dict[str, dict[str, Any]] = {}
    for entry in sources:
        if "source_id" not in entry:
            raise ConfigError(f"source entry missing source_id: {entry!r}")
        cfg = _deep_merge(defaults, entry)
        merged[cfg["source_id"]] = cfg
    return merged


def load_sources(path: Path | None = None) -> dict[str, dict[str, Any]]:
    settings = get_settings()
    return _load_raw(str(path or settings.sources_config))


def get_source_config(source_id: str, path: Path | None = None) -> dict[str, Any]:
    sources = load_sources(path)
    if source_id not in sources:
        known = ", ".join(sorted(sources)) or "<none>"
        raise ConfigError(f"unknown source_id {source_id!r}. known: {known}")
    # Expand env refs at use time, never at load time — keeps secrets out of any
    # cached config that might get logged or dumped.
    return _expand_env(sources[source_id])


def sources_for_schedule(schedule: str, path: Path | None = None) -> list[str]:
    return sorted(
        sid
        for sid, cfg in load_sources(path).items()
        if cfg.get("schedule") == schedule and cfg.get("enabled", True)
    )


def _import_scraper_class(module_ref: str) -> type:
    """'lake.sources.x.scraper:XScraper' -> the class object."""
    if ":" not in module_ref:
        raise ConfigError(f"module must be 'pkg.mod:ClassName', got {module_ref!r}")
    mod_name, cls_name = module_ref.split(":", 1)
    try:
        module = importlib.import_module(mod_name)
    except ImportError as exc:
        raise ConfigError(f"cannot import {mod_name!r}: {exc}") from exc
    try:
        return getattr(module, cls_name)
    except AttributeError as exc:
        raise ConfigError(f"{mod_name!r} has no attribute {cls_name!r}") from exc


def build_scraper(
    source_id: str, *, storage: Storage | None = None, meta: MetadataRepo | None = None
):
    """Instantiate the scraper for a source, fully wired."""
    cfg = get_source_config(source_id)
    module_ref = cfg.get("module")
    if not module_ref:
        raise ConfigError(f"source {source_id!r} has no 'module' key")

    storage = storage or default_storage()
    meta = meta or MetadataRepo()

    cls = _import_scraper_class(module_ref)
    return cls(cfg, storage, meta)
