"""Checkpoints, artifact store and observability sinks.

Two interchangeable backends:
- Sql*: Snowflake tables created by setup.sql in APP_DB.APP_SCHEMA.
- Memory*: process-local (tests, dry runs, or before setup.sql has been run).
Artifacts live in a table (ARTIFACTS) rather than a stage: identical behaviour in SiS
and locally, versioned by iteration, and directly queryable.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Protocol

from core.db import Db
from core.state import RunState


class Checkpoints(Protocol):
    def save(self, state: RunState) -> None: ...
    def load(self, run_id: str) -> RunState | None: ...
    def list_runs(self, limit: int = 50) -> list[dict]: ...


class ArtifactStore(Protocol):
    def put(self, run_id: str, stage: str, iteration: int, name: str, content: str) -> None: ...
    def get(self, run_id: str, stage: str, name: str, iteration: int | None = None) -> str | None: ...
    def list(self, run_id: str) -> list[dict]: ...


class Obs(Protocol):
    def llm_call(self, rec: dict) -> None: ...
    def node(self, rec: dict) -> None: ...
    def llm_calls(self, run_id: str | None = None) -> list[dict]: ...
    def nodes(self, run_id: str | None = None) -> list[dict]: ...


# ---------------------------------------------------------------- memory
class MemoryCheckpoints:
    def __init__(self) -> None:
        self._d: dict[str, str] = {}

    def save(self, state: RunState) -> None:
        state.updated_at = time.time()
        self._d[state.run_id] = state.to_json()

    def load(self, run_id: str) -> RunState | None:
        s = self._d.get(run_id)
        return RunState.from_json(s) if s else None

    def list_runs(self, limit: int = 50) -> list[dict]:
        rows = []
        for s in self._d.values():
            st = RunState.from_json(s)
            rows.append({"run_id": st.run_id, "slice_id": st.slice.slice_id, "status": st.status,
                         "stage": st.current_stage, "updated_at": st.updated_at})
        return sorted(rows, key=lambda r: r["updated_at"], reverse=True)[:limit]


class MemoryStore:
    def __init__(self) -> None:
        self._d: dict[tuple, str] = {}

    def put(self, run_id, stage, iteration, name, content) -> None:
        self._d[(run_id, stage, int(iteration), name)] = content

    def get(self, run_id, stage, name, iteration=None):
        keys = [k for k in self._d if k[0] == run_id and k[1] == stage and k[3] == name
                and (iteration is None or k[2] == iteration)]
        return self._d[max(keys, key=lambda k: k[2])] if keys else None

    def list(self, run_id):
        return [{"stage": k[1], "iteration": k[2], "name": k[3], "bytes": len(v)}
                for k, v in sorted(self._d.items()) if k[0] == run_id]


class MemoryObs:
    def __init__(self) -> None:
        self._llm: list[dict] = []
        self._nodes: list[dict] = []
        self._lock = threading.Lock()

    def llm_call(self, rec):
        with self._lock:
            self._llm.append(dict(rec, ts=time.time()))

    def node(self, rec):
        with self._lock:
            self._nodes.append(dict(rec, ts=time.time()))

    def llm_calls(self, run_id=None):
        return [r for r in self._llm if run_id is None or r.get("run_id") == run_id]

    def nodes(self, run_id=None):
        return [r for r in self._nodes if run_id is None or r.get("run_id") == run_id]


# ---------------------------------------------------------------- snowflake
class SqlCheckpoints:
    def __init__(self, db: Db, fq_schema: str) -> None:
        self.db, self.t = db, f"{fq_schema}.RUN_CHECKPOINTS"

    def save(self, state: RunState) -> None:
        state.updated_at = time.time()
        self.db.query(
            f"MERGE INTO {self.t} t USING (SELECT ? run_id, ? slice_id, ? status, ? stage, ? state) s "
            "ON t.run_id = s.run_id "
            "WHEN MATCHED THEN UPDATE SET slice_id=s.slice_id, status=s.status, stage=s.stage, "
            "state=PARSE_JSON(s.state), updated_at=CURRENT_TIMESTAMP() "
            "WHEN NOT MATCHED THEN INSERT (run_id, slice_id, status, stage, state, created_at, updated_at) "
            "VALUES (s.run_id, s.slice_id, s.status, s.stage, PARSE_JSON(s.state), CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP())",
            [state.run_id, state.slice.slice_id, state.status, state.current_stage, state.to_json()])

    def load(self, run_id: str) -> RunState | None:
        rows = self.db.query(f"SELECT state::VARCHAR AS s FROM {self.t} WHERE run_id = ?", [run_id])
        return RunState.from_json(rows[0]["s"]) if rows else None

    def list_runs(self, limit: int = 50) -> list[dict]:
        return self.db.query(
            f"SELECT run_id, slice_id, status, stage, updated_at FROM {self.t} "
            f"ORDER BY updated_at DESC LIMIT {int(limit)}")


class SqlStore:
    def __init__(self, db: Db, fq_schema: str) -> None:
        self.db, self.t = db, f"{fq_schema}.ARTIFACTS"

    def put(self, run_id, stage, iteration, name, content) -> None:
        self.db.query(
            f"MERGE INTO {self.t} t USING (SELECT ? run_id, ? stage, ? iteration, ? name, ? content) s "
            "ON t.run_id=s.run_id AND t.stage=s.stage AND t.iteration=s.iteration AND t.name=s.name "
            "WHEN MATCHED THEN UPDATE SET content=s.content, created_at=CURRENT_TIMESTAMP() "
            "WHEN NOT MATCHED THEN INSERT (run_id, stage, iteration, name, content, created_at) "
            "VALUES (s.run_id, s.stage, s.iteration, s.name, s.content, CURRENT_TIMESTAMP())",
            [run_id, stage, int(iteration), name, content])

    def get(self, run_id, stage, name, iteration=None):
        if iteration is None:
            rows = self.db.query(f"SELECT content FROM {self.t} WHERE run_id=? AND stage=? AND name=? "
                                 "ORDER BY iteration DESC LIMIT 1", [run_id, stage, name])
        else:
            rows = self.db.query(f"SELECT content FROM {self.t} WHERE run_id=? AND stage=? AND name=? AND iteration=?",
                                 [run_id, stage, name, int(iteration)])
        return rows[0]["content"] if rows else None

    def list(self, run_id):
        return self.db.query(f"SELECT stage, iteration, name, LENGTH(content) AS bytes FROM {self.t} "
                             "WHERE run_id=? ORDER BY stage, iteration, name", [run_id])


class SqlObs:
    """Observability writes must never break a run: failures fall back to memory."""

    def __init__(self, db: Db, fq_schema: str) -> None:
        self.db, self.s = db, fq_schema
        self.fallback = MemoryObs()

    def llm_call(self, r):
        vals = [r.get("run_id"), r.get("stage"), r.get("role"), r.get("model"), r.get("tokens_in", 0),
                r.get("tokens_out", 0), r.get("latency_ms", 0), r.get("retries", 0), r.get("status"),
                (r.get("error") or "")[:4000]]
        cols = "run_id, stage, role, model, tokens_in, tokens_out, latency_ms, retries, status, error"
        try:
            self.db.query(f"INSERT INTO {self.s}.OBS_LLM_CALLS ({cols}, query_id, ts) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP())", vals + [r.get("query_id")])
        except Exception:
            try:  # tables created by an older setup.sql have no query_id column
                self.db.query(f"INSERT INTO {self.s}.OBS_LLM_CALLS ({cols}, ts) "
                              "VALUES (?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP())", vals)
            except Exception:
                self.fallback.llm_call(r)

    def node(self, r):
        try:
            self.db.query(
                f"INSERT INTO {self.s}.OBS_NODES (run_id, stage, node, status, duration_ms, detail, ts) "
                "VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP())",
                [r.get("run_id"), r.get("stage"), r.get("node"), r.get("status"), r.get("duration_ms", 0),
                 json.dumps(r.get("detail", {}), default=str)[:16000]])
        except Exception:
            self.fallback.node(r)

    def llm_calls(self, run_id=None):
        w, p = ("WHERE run_id = ?", [run_id]) if run_id else ("", [])
        return self.db.query(f"SELECT * FROM {self.s}.OBS_LLM_CALLS {w} ORDER BY ts DESC LIMIT 1000", p) \
            + self.fallback.llm_calls(run_id)

    def nodes(self, run_id=None):
        w, p = ("WHERE run_id = ?", [run_id]) if run_id else ("", [])
        return self.db.query(f"SELECT * FROM {self.s}.OBS_NODES {w} ORDER BY ts DESC LIMIT 1000", p) \
            + self.fallback.nodes(run_id)


# ---------------------------------------------------------------- local, durable
_LLM_COLS = ("id", "run_id", "stage", "role", "model", "tokens_in", "tokens_out", "latency_ms", "retries", "status",
             "error", "query_id", "ts", "billed_credits", "billed_at")
_NODE_COLS = ("id", "run_id", "stage", "node", "status", "duration_ms", "detail", "ts")


class SqliteObs:
    """Durable observability in the local state file (.state/runs.sqlite): survives restarts.

    Holds every Cortex call with its Snowflake query id, so billed credits can be matched later
    (set_billed). An optional mirror (SqlObs) receives a copy of each write; mirror failures are ignored.
    """

    def __init__(self, path: str, mirror: Obs | None = None) -> None:
        import sqlite3

        self._c = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        self.mirror = mirror
        with self._lock:
            self._c.executescript(
                "CREATE TABLE IF NOT EXISTS obs_llm_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, stage TEXT, "
                "role TEXT, model TEXT, tokens_in INTEGER, tokens_out INTEGER, latency_ms INTEGER, retries INTEGER, "
                "status TEXT, error TEXT, query_id TEXT, ts REAL, billed_credits REAL, billed_at REAL);"
                "CREATE INDEX IF NOT EXISTS ix_llm_run ON obs_llm_calls(run_id);"
                "CREATE INDEX IF NOT EXISTS ix_llm_qid ON obs_llm_calls(query_id);"
                "CREATE TABLE IF NOT EXISTS obs_nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, stage TEXT, "
                "node TEXT, status TEXT, duration_ms INTEGER, detail TEXT, ts REAL);"
                "CREATE INDEX IF NOT EXISTS ix_nodes_run ON obs_nodes(run_id);")
            self._c.commit()

    def _mirror(self, fn: str, rec: dict) -> None:
        if self.mirror is not None:
            try:
                getattr(self.mirror, fn)(rec)
            except Exception:
                pass

    def llm_call(self, r):
        with self._lock:
            self._c.execute(
                "INSERT INTO obs_llm_calls (run_id, stage, role, model, tokens_in, tokens_out, latency_ms, retries, "
                "status, error, query_id, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (r.get("run_id"), r.get("stage"), r.get("role"), r.get("model"), int(r.get("tokens_in") or 0),
                 int(r.get("tokens_out") or 0), int(r.get("latency_ms") or 0), int(r.get("retries") or 0),
                 r.get("status"), (r.get("error") or "")[:4000], r.get("query_id"), time.time()))
            self._c.commit()
        self._mirror("llm_call", r)

    def node(self, r):
        with self._lock:
            self._c.execute(
                "INSERT INTO obs_nodes (run_id, stage, node, status, duration_ms, detail, ts) VALUES (?,?,?,?,?,?,?)",
                (r.get("run_id"), r.get("stage"), r.get("node"), r.get("status"), int(r.get("duration_ms") or 0),
                 json.dumps(r.get("detail", {}), default=str)[:16000], time.time()))
            self._c.commit()
        self._mirror("node", r)

    def _select(self, table: str, cols: tuple, run_id: str | None, since: float | None = None) -> list[dict]:
        where, params = [], []
        if run_id:
            where.append("run_id = ?"), params.append(run_id)
        if since:
            where.append("ts >= ?"), params.append(since)
        sql = f"SELECT {', '.join(cols)} FROM {table}" + (f" WHERE {' AND '.join(where)}" if where else "") + " ORDER BY ts"
        with self._lock:
            return [dict(zip(cols, row)) for row in self._c.execute(sql, params).fetchall()]

    def llm_calls(self, run_id=None, since=None):
        return self._select("obs_llm_calls", _LLM_COLS, run_id, since)

    def nodes(self, run_id=None, since=None):
        return self._select("obs_nodes", _NODE_COLS, run_id, since)

    def unbilled_query_ids(self, max_age_days: float = 30) -> list[str]:
        with self._lock:
            rows = self._c.execute(
                "SELECT DISTINCT query_id FROM obs_llm_calls WHERE status = 'ok' AND query_id IS NOT NULL "
                "AND billed_credits IS NULL AND ts >= ?", (time.time() - max_age_days * 86400,)).fetchall()
        return [r[0] for r in rows]

    def set_billed(self, billed: dict[str, float]) -> int:
        """query_id -> billed credits (from ACCOUNT_USAGE). Returns rows updated."""
        n = 0
        with self._lock:
            for qid, credits in billed.items():
                n += self._c.execute("UPDATE obs_llm_calls SET billed_credits = ?, billed_at = ? WHERE query_id = ?",
                                     (float(credits), time.time(), qid)).rowcount
            self._c.commit()
        return n
