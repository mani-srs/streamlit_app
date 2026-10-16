"""Snowflake -> Databricks (Delta / Spark SQL) type mapping. Deterministic."""
from __future__ import annotations

import re

# (dbx type, risk message or None)
_SIMPLE = {
    "VARCHAR": ("STRING", None), "STRING": ("STRING", None), "TEXT": ("STRING", None),
    "CHAR": ("STRING", "CHAR padding semantics differ; mapped to STRING"),
    "CHARACTER": ("STRING", "CHAR padding semantics differ; mapped to STRING"),
    "NCHAR": ("STRING", None), "NVARCHAR": ("STRING", None), "NVARCHAR2": ("STRING", None),
    "CHARACTER VARYING": ("STRING", None),
    "BOOLEAN": ("BOOLEAN", None),
    "DATE": ("DATE", None),
    "DATETIME": ("TIMESTAMP_NTZ", None), "TIMESTAMP": ("TIMESTAMP_NTZ", None), "TIMESTAMP_NTZ": ("TIMESTAMP_NTZ", None),
    "TIMESTAMP WITHOUT TIME ZONE": ("TIMESTAMP_NTZ", None),
    "TIMESTAMP_LTZ": ("TIMESTAMP", "TIMESTAMP_LTZ -> TIMESTAMP uses the Spark session time zone"),
    "TIMESTAMP WITH LOCAL TIME ZONE": ("TIMESTAMP", "LTZ -> TIMESTAMP uses the Spark session time zone"),
    "TIMESTAMP_TZ": ("TIMESTAMP", "TIMESTAMP_TZ offset is not stored in Delta; values normalized to UTC"),
    "TIMESTAMP WITH TIME ZONE": ("TIMESTAMP", "TZ offset is not stored in Delta; values normalized to UTC"),
    "TIME": ("STRING", "Databricks has no TIME type; stored as STRING HH:MM:SS"),
    "FLOAT": ("DOUBLE", None), "FLOAT4": ("DOUBLE", None), "FLOAT8": ("DOUBLE", None), "DOUBLE": ("DOUBLE", None),
    "DOUBLE PRECISION": ("DOUBLE", None), "REAL": ("DOUBLE", None),
    "INT": ("DECIMAL(38,0)", None), "INTEGER": ("DECIMAL(38,0)", None), "BIGINT": ("DECIMAL(38,0)", None),
    "SMALLINT": ("DECIMAL(38,0)", None), "TINYINT": ("DECIMAL(38,0)", None), "BYTEINT": ("DECIMAL(38,0)", None),
    "BINARY": ("BINARY", None), "VARBINARY": ("BINARY", None),
    "VARIANT": ("VARIANT", "VARIANT requires DBR 15.3+; path syntax a:b stays, casts may differ"),
    "OBJECT": ("VARIANT", "OBJECT mapped to VARIANT; consider STRUCT/MAP once schema is known"),
    "ARRAY": ("VARIANT", "ARRAY mapped to VARIANT; consider ARRAY<type> once element type is known"),
    "GEOGRAPHY": ("STRING", "GEOGRAPHY stored as WKT/GeoJSON STRING; spatial functions need rework"),
    "GEOMETRY": ("STRING", "GEOMETRY stored as WKT STRING; spatial functions need rework"),
    "VECTOR": ("ARRAY<FLOAT>", "VECTOR mapped to ARRAY<FLOAT>; similarity functions need rework"),
}


def map_type(sf: str) -> tuple[str, str | None]:
    t = re.sub(r"\s+", " ", sf.strip().upper())
    m = re.match(r"^(NUMBER|NUMERIC|DECIMAL|DEC)\s*(?:\(\s*(\d+)\s*(?:,\s*(\d+))?\s*\))?$", t)
    if m:
        p, s = int(m.group(2) or 38), int(m.group(3) or 0)
        return f"DECIMAL({min(p, 38)},{s})", None
    m = re.match(r"^(VARCHAR|STRING|TEXT|CHAR|CHARACTER|NCHAR|NVARCHAR|NVARCHAR2|CHARACTER VARYING|BINARY|VARBINARY)\s*\(\s*\d+\s*\)$", t)
    if m:
        return _SIMPLE[m.group(1)]
    m = re.match(r"^(TIMESTAMP(?:_NTZ|_LTZ|_TZ)?|DATETIME|TIME)\s*\(\s*(\d)\s*\)$", t)
    if m:
        dbx, risk = _SIMPLE[m.group(1)]
        if int(m.group(2)) > 6 and dbx.startswith("TIMESTAMP"):
            risk = (risk + "; " if risk else "") + "nanosecond precision truncated to microseconds"
        return dbx, risk
    if t.startswith("ARRAY(") or t.startswith("OBJECT(") or t.startswith("MAP("):
        return "VARIANT", f"structured type {t} mapped to VARIANT; review"
    if t.startswith("VECTOR("):
        return _SIMPLE["VECTOR"]
    if t in _SIMPLE:
        return _SIMPLE[t]
    return "STRING", f"unknown Snowflake type {t}; defaulted to STRING"
