"""Frozen shared contracts. Agents USE these; changes are blueprint changes.

Bump CONTRACTS_VERSION on any interface change; contract tests assert against it.
"""
CONTRACTS_VERSION = "1.1.0"  # 1.1.0: orchestration on LangGraph; nodes return state deltas
