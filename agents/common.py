"""Helpers shared by agents (read prior stage outputs, name mapping)."""
from __future__ import annotations

from converters.dialect import NameMapper
from core.schemas import ContractViolation
from core.state import RunState


def need(state: RunState, stage: str) -> dict:
    out = state.outputs.get(stage)
    if out is None:
        raise ContractViolation(f"stage output {stage!r} missing; stages must run in AGENT_ORDER")
    return out


def default_ns(state: RunState) -> tuple[str, str]:
    s = state.slice
    if s.database:
        return s.database.upper(), s.schema.upper()
    objs = state.outputs.get("standardization", {}).get("objects", [])
    if objs:
        return objs[0]["database"] or "DB", objs[0]["schema"] or "PUBLIC"
    return "DB", "PUBLIC"


def mapper_for(state: RunState) -> NameMapper:
    db, sch = default_ns(state)
    return NameMapper(db, sch, catalog_override=(state.slice.target_catalog or "").lower())


def in_scope_objects(state: RunState) -> list[dict]:
    objs = need(state, "standardization")["objects"]
    excluded = set(state.outputs.get("discovery", {}).get("excluded", []))
    return [o for o in objs if o["id"] not in excluded]
