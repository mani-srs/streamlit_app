"""Snowflake task SCHEDULE -> Databricks Quartz cron."""
from __future__ import annotations

import re


def to_quartz(schedule: str | None) -> tuple[dict | None, str | None]:
    """Returns ({quartz_cron_expression, timezone_id}, risk) or (None, reason)."""
    if not schedule:
        return None, None
    s = schedule.strip()
    m = re.match(r"^USING\s+CRON\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*$", s, re.I)
    if m:
        minute, hour, dom, mon, dow, tz = m.groups()
        risk = None
        # Quartz: sec min hour dom mon dow ; exactly one of dom/dow must be '?'
        if dow != "*" and dom == "*":
            dom = "?"
            dow = _dow(dow)
        elif dow != "*" and dom != "*":
            risk = "cron uses both day-of-month and day-of-week; Quartz supports only one - kept day-of-month"
            dow = "?"
        else:
            dow = "?"
        if "L" in dom.upper() and dom != "L":
            risk = "day-of-month 'L' variant needs review"
        return {"quartz_cron_expression": f"0 {minute} {hour} {dom} {mon} {dow}", "timezone_id": tz}, risk
    m = re.match(r"^(\d+)\s*(MINUTE|MINUTES|M|SECOND|SECONDS|S|HOUR|HOURS|H)$", s, re.I)
    if m:
        n, unit = int(m.group(1)), m.group(2).upper()[0]
        if unit == "S":
            n, unit = max(1, round(n / 60)), "M"
            risk = "second-level schedule rounded to minutes"
        else:
            risk = None
        if unit == "H":
            n *= 60
        if n < 60 and 60 % n == 0:
            expr = f"0 0/{n} * * * ?"
        elif n % 60 == 0 and 24 % (n // 60) == 0:
            expr = f"0 0 0/{n // 60} * * ?"
        else:
            return {"quartz_cron_expression": "0 0 * * * ?", "timezone_id": "UTC"}, \
                f"interval {s} not expressible in Quartz; defaulted to hourly - use a continuous/periodic trigger"
        return {"quartz_cron_expression": expr, "timezone_id": "UTC"}, risk
    return None, f"unrecognized SCHEDULE {s!r}"


_DOW = {"0": "SUN", "7": "SUN", "1": "MON", "2": "TUE", "3": "WED", "4": "THU", "5": "FRI", "6": "SAT"}


def _dow(dow: str) -> str:
    # Snowflake (unix) 0/7=SUN; Quartz numeric is 1=SUN, so emit names
    return re.sub(r"\d", lambda m: _DOW[m.group(0)], dow)
