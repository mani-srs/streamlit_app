"""Role -> Cortex model registry. Overridable via config/models.yaml."""
from __future__ import annotations

import os

DEFAULTS = {
    "planner": {"model": "claude-4-sonnet", "fallback": ["llama3.1-70b"], "max_tokens": 8000, "temperature": 0},
    "evaluator": {"model": "claude-4-sonnet", "fallback": ["llama3.1-70b"], "max_tokens": 4000, "temperature": 0},
    "translator": {"model": "claude-4-sonnet", "fallback": ["llama3.1-70b"], "max_tokens": 6000, "temperature": 0},
}

_CFG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "models.yaml")
_cache: dict | None = None


def registry() -> dict:
    global _cache
    if _cache is None:
        reg = {k: dict(v) for k, v in DEFAULTS.items()}
        try:
            import yaml

            with open(_CFG, encoding="utf-8") as f:
                for role, spec in (yaml.safe_load(f) or {}).get("roles", {}).items():
                    reg.setdefault(role, {}).update(spec or {})
        except (FileNotFoundError, ImportError):  # no config or no PyYAML in the runtime: defaults
            pass
        _cache = reg
    return _cache


def for_role(role: str) -> dict:
    reg = registry()
    if role not in reg:
        raise KeyError(f"unknown model role {role!r}; known: {sorted(reg)}")
    return reg[role]
