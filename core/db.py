"""Thin SQL access layer. Works in SiS (Snowpark session) and locally (connector).

All statements use qmark (?) binding. Result rows are dicts with lower-cased keys.
"""
from __future__ import annotations

import os
from typing import Any, Protocol


APP_QUERY_TAG = "sf2dbx_migration_app"  # lets Snowflake billing views tell this app's Cortex calls apart


class Db(Protocol):
    def query(self, sql: str, params: list | None = None) -> list[dict]: ...


def _lower(d: dict) -> dict:
    return {str(k).lower(): v for k, v in d.items()}


class SnowparkDb:
    def __init__(self, session: Any) -> None:
        self.session = session

    def query(self, sql: str, params: list | None = None) -> list[dict]:
        rows = self.session.sql(sql, params=params or None).collect()
        return [_lower(r.as_dict()) for r in rows]

    def query_with_id(self, sql: str, params: list | None = None) -> tuple[list[dict], str | None]:
        try:  # async job exposes the query id; fall back to a plain collect (no id) if unsupported
            job = self.session.sql(sql, params=params or None).collect_nowait()
            return [_lower(r.as_dict()) for r in job.result()], job.query_id
        except AttributeError:
            return self.query(sql, params), None


class ConnectorDb:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def query(self, sql: str, params: list | None = None) -> list[dict]:
        return self.query_with_id(sql, params)[0]

    def query_with_id(self, sql: str, params: list | None = None) -> tuple[list[dict], str | None]:
        """Rows plus the Snowflake query id (used to match Cortex calls with billed credits)."""
        from snowflake.connector import DictCursor

        with self.conn.cursor(DictCursor) as cur:  # one cursor per call: safe across threads
            cur.execute(sql, params or None)
            try:
                rows = [_lower(r) for r in cur.fetchall()]
            except Exception:  # statements without a result set
                rows = []
            return rows, cur.sfqid


def _snowflake_home() -> str:
    return os.environ.get("SNOWFLAKE_HOME") or os.path.join(os.path.expanduser("~"), ".snowflake")


def list_connections() -> list[str]:
    """Connection names from connections.toml (and [connections.*] in config.toml)."""
    import tomllib

    names: list[str] = []
    home = _snowflake_home()
    for fname, key in (("connections.toml", None), ("config.toml", "connections")):
        try:
            with open(os.path.join(home, fname), "rb") as f:
                data = tomllib.load(f)
        except (FileNotFoundError, tomllib.TOMLDecodeError):
            continue
        section = data.get(key, {}) if key else data
        names += [n for n, v in section.items() if isinstance(v, dict) and n not in names]
    return names


def resolve_connection_name(requested: str | None = None) -> str | None:
    """requested > $SNOWFLAKE_DEFAULT_CONNECTION_NAME > config.toml default_connection_name > the only connection."""
    import tomllib

    name = requested or os.environ.get("SNOWFLAKE_DEFAULT_CONNECTION_NAME") or os.environ.get("SNOWFLAKE_CONNECTION_NAME")
    if name:
        return name
    try:
        with open(os.path.join(_snowflake_home(), "config.toml"), "rb") as f:
            name = tomllib.load(f).get("default_connection_name")
    except (FileNotFoundError, tomllib.TOMLDecodeError):
        name = None
    if name:
        return name
    known = list_connections()
    return known[0] if len(known) == 1 else None


def connect(connection_name: str | None = None) -> Db:
    """Active SiS session if present, else a local connector connection."""
    try:
        from snowflake.snowpark.context import get_active_session

        return SnowparkDb(get_active_session())
    except Exception:
        pass
    import snowflake.connector

    snowflake.connector.paramstyle = "qmark"
    name = resolve_connection_name(connection_name)
    params = {"session_parameters": {"QUERY_TAG": APP_QUERY_TAG}}
    conn = snowflake.connector.connect(connection_name=name, **params) if name else snowflake.connector.connect(**params)
    return ConnectorDb(conn)


def qident(name: str) -> str:
    """Quote one identifier part safely for statements that cannot bind (SHOW ... IN)."""
    return '"' + str(name).replace('"', '""') + '"'


def qfqn(*parts: str) -> str:
    return ".".join(qident(p) for p in parts if p)
