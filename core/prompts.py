"""Prompt assembly with fixed priority and a hard-constraint post-check.

Priority (highest first, never truncated in this order of protection):
  P1 system/role  >  P4 human hard constraints + feedback  >  P2 task  >  P3 context
Only P3 context is truncated to fit the budget.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_PROMPT_CHARS = 120_000


@dataclass
class PromptParts:
    system: str  # P1
    task: str  # P2
    context: str = ""  # P3
    feedback: list[str] = field(default_factory=list)  # P4 freeform notes + accepted issues
    hard_constraints: list[str] = field(default_factory=list)  # P4, verbatim, checked afterwards


def assemble(p: PromptParts, budget: int = MAX_PROMPT_CHARS) -> list[dict]:
    sys_txt = p.system.strip()
    if p.hard_constraints:
        sys_txt += "\n\nHARD CONSTRAINTS (override everything else; violating any is a failure):\n" + \
            "\n".join(f"- {c}" for c in p.hard_constraints)
    fb = ""
    if p.feedback:
        fb = "\n\nREVIEWER FEEDBACK (apply all of it; it overrides the task defaults):\n" + \
            "\n".join(f"- {x}" for x in p.feedback)
    fixed = len(sys_txt) + len(p.task) + len(fb) + 200
    room = max(0, budget - fixed)
    ctx = p.context
    if len(ctx) > room:
        ctx = ctx[:room] + "\n...[context truncated]"
    user = p.task.strip() + fb + (f"\n\nCONTEXT:\n{ctx}" if ctx else "")
    return [{"role": "system", "content": sys_txt}, {"role": "user", "content": user}]


_MUST_NOT = re.compile(r"^\s*(?:must\s+not\s+contain|never\s+use|do\s+not\s+use)\s*[:=]\s*(.+)$", re.I)
_MUST = re.compile(r"^\s*(?:must\s+contain|always\s+include)\s*[:=]\s*(.+)$", re.I)


def post_check(text: str, hard_constraints: list[str]) -> list[str]:
    """Machine-checkable constraints: 'must not contain: X', 'must contain: X'.

    Other constraints are passed to the model verbatim but cannot be verified here.
    Returns human-readable violations.
    """
    out = []
    low = text.lower()
    for c in hard_constraints:
        m = _MUST_NOT.match(c)
        if m and m.group(1).strip().strip("'\"").lower() in low:
            out.append(f"hard constraint violated: {c}")
        m = _MUST.match(c)
        if m and m.group(1).strip().strip("'\"").lower() not in low:
            out.append(f"hard constraint violated: {c}")
    return out


_FENCE = re.compile(r"^\s*```[\w+-]*\s*\n(.*?)\n\s*```\s*$", re.S)


def strip_fences(text: str) -> str:
    m = _FENCE.match(text.strip())
    return m.group(1) if m else text.strip()
