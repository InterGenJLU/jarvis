"""Structured Event Logger — foundation for JARVIS self-observability.

Three-entity model (research-informed):
  - Observations: what happened (real-time, tree-structured wide events)
  - Scores: how good was it (async, post-hoc evaluation)
  - Reflections: what to do differently (natural language, for LLM consumption)

Singleton access via get_event_logger(config).
"""

import json
import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from core.logger import get_logger

logger = get_logger(__name__)

_instance: Optional["EventLogger"] = None


def get_event_logger(config=None) -> Optional["EventLogger"]:
    """Get or create the singleton EventLogger."""
    global _instance
    if _instance is None and config is not None:
        _instance = EventLogger(config)
    return _instance


# Valid categories (9 from research synthesis)
CATEGORIES = frozenset({
    "user_interaction",   # wake word, utterance, intent, session boundaries
    "decision",           # route selection, skill matching, model choice, fallback triggers
    "inference",          # LLM calls, STT, TTS, embeddings
    "tool_execution",     # tool/skill invocation, arguments, results
    "memory",             # recall, storage, dedup, context management
    "error_recovery",     # errors, retries, fallbacks, watchdog interventions
    "performance",        # end-to-end latency, component timings, cache stats
    "self_assessment",    # response quality scores, routing accuracy
    "learning",           # verbal reflections, strategy updates
})

SEVERITIES = frozenset({"trace", "debug", "info", "warn", "error", "fatal"})

SCORE_TYPES = frozenset({"numeric", "categorical", "boolean"})
SCORE_SOURCES = frozenset({"automated", "self_assessed", "user_signal"})

REFLECTION_CATEGORIES = frozenset({
    "failure_analysis",
    "strategy_update",
    "capability_assessment",
    "user_preference",
})


class EventLogger:
    """Structured event storage with tree-structured traces and wide events."""

    def __init__(self, config):
        self.db_path = Path(config.get(
            "events.db_path",
            "/mnt/storage/jarvis/data/events.db",
        ))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.retention_days = config.get("events.retention_days", 180)
        self._db_lock = threading.Lock()
        self._on_emit_callback = None
        self._init_db()
        logger.info("EventLogger initialized: %s (retention=%dd)",
                     self.db_path, self.retention_days)

    def _init_db(self):
        """Create tables and indexes."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.executescript("""
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS observations (
                    id         TEXT PRIMARY KEY,
                    trace_id   TEXT NOT NULL,
                    parent_id  TEXT,
                    session_id TEXT,

                    timestamp  REAL NOT NULL,
                    duration_ms REAL,

                    category   TEXT NOT NULL,
                    event      TEXT NOT NULL,
                    severity   TEXT DEFAULT 'info',
                    source     TEXT,

                    message    TEXT,
                    metadata   TEXT,

                    stage      TEXT,
                    status     TEXT,
                    speaker_id TEXT,
                    model      TEXT,
                    latency_ms REAL
                );

                CREATE INDEX IF NOT EXISTS idx_obs_timestamp
                    ON observations(timestamp);
                CREATE INDEX IF NOT EXISTS idx_obs_trace
                    ON observations(trace_id);
                CREATE INDEX IF NOT EXISTS idx_obs_category_event
                    ON observations(category, event);
                CREATE INDEX IF NOT EXISTS idx_obs_session
                    ON observations(session_id);
                CREATE INDEX IF NOT EXISTS idx_obs_severity
                    ON observations(severity)
                    WHERE severity IN ('warn', 'error', 'fatal');

                CREATE TABLE IF NOT EXISTS scores (
                    id             TEXT PRIMARY KEY,
                    observation_id TEXT NOT NULL REFERENCES observations(id),
                    name           TEXT NOT NULL,
                    value          REAL,
                    label          TEXT,
                    data_type      TEXT NOT NULL,
                    source         TEXT NOT NULL,
                    comment        TEXT,
                    timestamp      REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_scores_obs
                    ON scores(observation_id);
                CREATE INDEX IF NOT EXISTS idx_scores_name
                    ON scores(name, timestamp);

                CREATE TABLE IF NOT EXISTS reflections (
                    id         TEXT PRIMARY KEY,
                    trace_id   TEXT,
                    timestamp  REAL NOT NULL,
                    category   TEXT NOT NULL,
                    content    TEXT NOT NULL,
                    confidence REAL,
                    applied    INTEGER DEFAULT 0,
                    outcome    TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_reflect_ts
                    ON reflections(timestamp);
                CREATE INDEX IF NOT EXISTS idx_reflect_cat
                    ON reflections(category);
            """)
            conn.commit()
        finally:
            conn.close()

    def _get_conn(self) -> sqlite3.Connection:
        """Get a new SQLite connection with row_factory set."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def set_on_emit(self, callback):
        """Set callback invoked after each emit() — for WebSocket push."""
        self._on_emit_callback = callback

    # ------------------------------------------------------------------
    # Generate IDs
    # ------------------------------------------------------------------

    @staticmethod
    def new_id() -> str:
        """Generate a new UUID for observations, scores, or reflections."""
        return str(uuid.uuid4())

    @staticmethod
    def new_trace_id() -> str:
        """Generate a new trace ID for grouping observations in one turn."""
        return str(uuid.uuid4())

    # ------------------------------------------------------------------
    # Write — Observations
    # ------------------------------------------------------------------

    def emit(self, *, category: str, event: str, message: str = None,
             metadata: dict = None, trace_id: str = None,
             parent_id: str = None, session_id: str = None,
             severity: str = "info", source: str = None,
             duration_ms: float = None, stage: str = None,
             status: str = None, speaker_id: str = None,
             model: str = None, latency_ms: float = None,
             timestamp: float = None, observation_id: str = None) -> str:
        """Emit a structured observation event.

        Returns the observation ID for parent-child linking.
        This is the hot-path call — kept minimal for low latency.
        """
        if category not in CATEGORIES:
            logger.warning("EventLogger: unknown category '%s', recording anyway", category)
        if severity not in SEVERITIES:
            severity = "info"

        obs_id = observation_id or self.new_id()
        if trace_id is None:
            trace_id = self.new_trace_id()
        if timestamp is None:
            timestamp = time.time()

        metadata_json = json.dumps(metadata, default=str) if metadata else None

        row = {
            "id": obs_id,
            "trace_id": trace_id,
            "parent_id": parent_id,
            "session_id": session_id,
            "timestamp": timestamp,
            "duration_ms": duration_ms,
            "category": category,
            "event": event,
            "severity": severity,
            "source": source,
            "message": message,
            "metadata": metadata_json,
            "stage": stage,
            "status": status,
            "speaker_id": speaker_id,
            "model": model,
            "latency_ms": latency_ms,
        }

        with self._db_lock:
            conn = sqlite3.connect(str(self.db_path))
            try:
                conn.execute("""
                    INSERT INTO observations
                        (id, trace_id, parent_id, session_id, timestamp,
                         duration_ms, category, event, severity, source,
                         message, metadata, stage, status, speaker_id,
                         model, latency_ms)
                    VALUES
                        (:id, :trace_id, :parent_id, :session_id, :timestamp,
                         :duration_ms, :category, :event, :severity, :source,
                         :message, :metadata, :stage, :status, :speaker_id,
                         :model, :latency_ms)
                """, row)
                conn.commit()
            finally:
                conn.close()

        # Notify listeners (non-blocking)
        if self._on_emit_callback:
            try:
                self._on_emit_callback(row)
            except Exception:
                pass

        return obs_id

    # ------------------------------------------------------------------
    # Write — Scores
    # ------------------------------------------------------------------

    def add_score(self, *, observation_id: str, name: str,
                  value: float = None, label: str = None,
                  data_type: str = "numeric", source: str = "automated",
                  comment: str = None, timestamp: float = None) -> str:
        """Attach a post-hoc score to an observation."""
        if data_type not in SCORE_TYPES:
            logger.warning("EventLogger: unknown score type '%s'", data_type)
        if source not in SCORE_SOURCES:
            logger.warning("EventLogger: unknown score source '%s'", source)

        score_id = self.new_id()
        if timestamp is None:
            timestamp = time.time()

        with self._db_lock:
            conn = sqlite3.connect(str(self.db_path))
            try:
                conn.execute("""
                    INSERT INTO scores
                        (id, observation_id, name, value, label,
                         data_type, source, comment, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (score_id, observation_id, name, value, label,
                      data_type, source, comment, timestamp))
                conn.commit()
            finally:
                conn.close()

        return score_id

    # ------------------------------------------------------------------
    # Write — Reflections
    # ------------------------------------------------------------------

    def add_reflection(self, *, category: str, content: str,
                       trace_id: str = None, confidence: float = None,
                       timestamp: float = None) -> str:
        """Store a natural language reflection for future LLM consumption."""
        if category not in REFLECTION_CATEGORIES:
            logger.warning("EventLogger: unknown reflection category '%s'", category)

        ref_id = self.new_id()
        if timestamp is None:
            timestamp = time.time()

        with self._db_lock:
            conn = sqlite3.connect(str(self.db_path))
            try:
                conn.execute("""
                    INSERT INTO reflections
                        (id, trace_id, timestamp, category, content,
                         confidence, applied, outcome)
                    VALUES (?, ?, ?, ?, ?, ?, 0, NULL)
                """, (ref_id, trace_id, timestamp, category, content,
                      confidence))
                conn.commit()
            finally:
                conn.close()

        return ref_id

    # ------------------------------------------------------------------
    # Read — Count
    # ------------------------------------------------------------------

    def count(self, category: str = None, event: str = None,
              severity: str = None, hours: float = 24) -> int:
        """Count observations matching filters in a time window."""
        cutoff = time.time() - (hours * 3600)
        clauses = ["timestamp >= ?"]
        params: list[Any] = [cutoff]

        if category:
            clauses.append("category = ?")
            params.append(category)
        if event:
            clauses.append("event = ?")
            params.append(event)
        if severity:
            clauses.append("severity = ?")
            params.append(severity)

        where = " AND ".join(clauses)
        conn = self._get_conn()
        try:
            row = conn.execute(
                f"SELECT COUNT(*) as cnt FROM observations WHERE {where}",
                params,
            ).fetchone()
            return row["cnt"]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Read — Query (generic filtered)
    # ------------------------------------------------------------------

    def query(self, *, category: str = None, event: str = None,
              severity: str = None, stage: str = None,
              status: str = None, session_id: str = None,
              speaker_id: str = None, model: str = None,
              hours: float = None, limit: int = 100,
              offset: int = 0) -> list[dict]:
        """Generic filtered query returning observations as dicts."""
        clauses = []
        params: list[Any] = []

        if hours is not None:
            cutoff = time.time() - (hours * 3600)
            clauses.append("timestamp >= ?")
            params.append(cutoff)
        if category:
            clauses.append("category = ?")
            params.append(category)
        if event:
            clauses.append("event = ?")
            params.append(event)
        if severity:
            clauses.append("severity = ?")
            params.append(severity)
        if stage:
            clauses.append("stage = ?")
            params.append(stage)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if speaker_id:
            clauses.append("speaker_id = ?")
            params.append(speaker_id)
        if model:
            clauses.append("model = ?")
            params.append(model)

        where = " AND ".join(clauses) if clauses else "1=1"

        conn = self._get_conn()
        try:
            rows = conn.execute(
                f"""SELECT * FROM observations
                    WHERE {where}
                    ORDER BY timestamp DESC
                    LIMIT ? OFFSET ?""",
                params + [limit, offset],
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Read — Trend (time-bucketed aggregation)
    # ------------------------------------------------------------------

    def trend(self, event: str = None, category: str = None,
              hours: float = 24, bucket: str = "hour") -> list[dict]:
        """Time-bucketed counts and average latency for charting."""
        cutoff = time.time() - (hours * 3600)
        bucket_seconds = 3600 if bucket == "hour" else 86400

        clauses = ["timestamp >= ?"]
        params: list[Any] = [cutoff]
        if event:
            clauses.append("event = ?")
            params.append(event)
        if category:
            clauses.append("category = ?")
            params.append(category)

        where = " AND ".join(clauses)

        conn = self._get_conn()
        try:
            rows = conn.execute(f"""
                SELECT
                    CAST(timestamp / ? AS INTEGER) * ? as bucket_start,
                    COUNT(*) as count,
                    AVG(latency_ms) as avg_latency_ms,
                    SUM(CASE WHEN severity IN ('error', 'fatal') THEN 1 ELSE 0 END) as error_count
                FROM observations
                WHERE {where}
                GROUP BY bucket_start
                ORDER BY bucket_start
            """, [bucket_seconds, bucket_seconds] + params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Read — Percentile Latency
    # ------------------------------------------------------------------

    def get_percentile_latency(self, percentile: float = 0.5,
                                event: str = None, category: str = None,
                                stage: str = None,
                                hours: float = 24) -> float:
        """Percentile latency (ms) from observations.

        Args:
            percentile: 0.0-1.0 (e.g. 0.5=P50, 0.95=P95)
            event/category/stage: Optional filters
            hours: Time window

        Returns:
            Latency in ms at the given percentile, or 0.0 if no data.
        """
        cutoff = time.time() - (hours * 3600)
        clauses = ["timestamp >= ?", "latency_ms IS NOT NULL"]
        params: list[Any] = [cutoff]
        if event:
            clauses.append("event = ?")
            params.append(event)
        if category:
            clauses.append("category = ?")
            params.append(category)
        if stage:
            clauses.append("stage = ?")
            params.append(stage)

        where = " AND ".join(clauses)
        conn = self._get_conn()
        try:
            rows = conn.execute(
                f"SELECT latency_ms FROM observations "
                f"WHERE {where} ORDER BY latency_ms",
                params,
            ).fetchall()
            if not rows:
                return 0.0
            values = [r["latency_ms"] for r in rows]
            idx = min(int(len(values) * percentile), len(values) - 1)
            return round(values[idx], 1)
        finally:
            conn.close()

    def get_latency_stats(self, event: str = None, category: str = None,
                           stage: str = None,
                           hours: float = 24) -> dict:
        """Full latency stats: P50, P95, P99, mean, min, max, count."""
        cutoff = time.time() - (hours * 3600)
        clauses = ["timestamp >= ?", "latency_ms IS NOT NULL"]
        params: list[Any] = [cutoff]
        if event:
            clauses.append("event = ?")
            params.append(event)
        if category:
            clauses.append("category = ?")
            params.append(category)
        if stage:
            clauses.append("stage = ?")
            params.append(stage)

        where = " AND ".join(clauses)
        conn = self._get_conn()
        try:
            rows = conn.execute(
                f"SELECT latency_ms FROM observations "
                f"WHERE {where} ORDER BY latency_ms",
                params,
            ).fetchall()
            if not rows:
                return {"p50": 0, "p95": 0, "p99": 0,
                        "mean": 0, "min": 0, "max": 0, "count": 0}
            values = [r["latency_ms"] for r in rows]
            n = len(values)
            return {
                "p50": round(values[min(int(n * 0.50), n - 1)], 1),
                "p95": round(values[min(int(n * 0.95), n - 1)], 1),
                "p99": round(values[min(int(n * 0.99), n - 1)], 1),
                "mean": round(sum(values) / n, 1),
                "min": round(values[0], 1),
                "max": round(values[-1], 1),
                "count": n,
            }
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Read — Recent (last N of a category)
    # ------------------------------------------------------------------

    def recent(self, category: str = None, event: str = None,
               n: int = 10) -> list[dict]:
        """Most recent N observations, optionally filtered."""
        clauses = []
        params: list[Any] = []

        if category:
            clauses.append("category = ?")
            params.append(category)
        if event:
            clauses.append("event = ?")
            params.append(event)

        where = " AND ".join(clauses) if clauses else "1=1"

        conn = self._get_conn()
        try:
            rows = conn.execute(
                f"""SELECT * FROM observations
                    WHERE {where}
                    ORDER BY timestamp DESC
                    LIMIT ?""",
                params + [n],
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Read — Trace (all observations for one conversation turn)
    # ------------------------------------------------------------------

    def get_trace(self, trace_id: str) -> list[dict]:
        """All observations for a single trace, ordered by timestamp."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT * FROM observations
                   WHERE trace_id = ?
                   ORDER BY timestamp ASC""",
                (trace_id,),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Read — Summary (dashboard-ready aggregates)
    # ------------------------------------------------------------------

    def get_summary(self, hours: float = 24) -> dict:
        """Aggregated summary by category for dashboard display."""
        cutoff = time.time() - (hours * 3600)
        conn = self._get_conn()
        try:
            # Total counts
            totals = conn.execute("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN severity = 'error' THEN 1 ELSE 0 END) as errors,
                    SUM(CASE WHEN severity = 'fatal' THEN 1 ELSE 0 END) as fatals,
                    SUM(CASE WHEN severity = 'warn' THEN 1 ELSE 0 END) as warnings,
                    AVG(latency_ms) as avg_latency_ms
                FROM observations
                WHERE timestamp >= ?
            """, (cutoff,)).fetchone()

            # Per-category breakdown
            categories = {}
            for row in conn.execute("""
                SELECT
                    category,
                    COUNT(*) as count,
                    AVG(latency_ms) as avg_latency_ms,
                    SUM(CASE WHEN severity IN ('error', 'fatal') THEN 1 ELSE 0 END) as errors
                FROM observations
                WHERE timestamp >= ?
                GROUP BY category
                ORDER BY count DESC
            """, (cutoff,)):
                categories[row["category"]] = {
                    "count": row["count"],
                    "avg_latency_ms": round(row["avg_latency_ms"] or 0, 1),
                    "errors": row["errors"],
                }

            # Top events
            top_events = []
            for row in conn.execute("""
                SELECT event, COUNT(*) as count
                FROM observations
                WHERE timestamp >= ?
                GROUP BY event
                ORDER BY count DESC
                LIMIT 10
            """, (cutoff,)):
                top_events.append({"event": row["event"], "count": row["count"]})

            return {
                "total": totals["total"] or 0,
                "errors": (totals["errors"] or 0) + (totals["fatals"] or 0),
                "warnings": totals["warnings"] or 0,
                "avg_latency_ms": round(totals["avg_latency_ms"] or 0, 1),
                "categories": categories,
                "top_events": top_events,
                "hours": hours,
            }
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Read — Scores for an observation
    # ------------------------------------------------------------------

    def get_scores(self, observation_id: str) -> list[dict]:
        """All scores attached to an observation."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM scores WHERE observation_id = ? ORDER BY timestamp",
                (observation_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Read — Reflections
    # ------------------------------------------------------------------

    def get_reflections(self, category: str = None, applied: bool = None,
                        limit: int = 20) -> list[dict]:
        """Retrieve reflections, optionally filtered by category and status."""
        clauses = []
        params: list[Any] = []

        if category:
            clauses.append("category = ?")
            params.append(category)
        if applied is not None:
            clauses.append("applied = ?")
            params.append(1 if applied else 0)

        where = " AND ".join(clauses) if clauses else "1=1"

        conn = self._get_conn()
        try:
            rows = conn.execute(
                f"""SELECT * FROM reflections
                    WHERE {where}
                    ORDER BY timestamp DESC
                    LIMIT ?""",
                params + [limit],
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def mark_reflection_applied(self, reflection_id: str,
                                 outcome: str = None) -> None:
        """Mark a reflection as applied with optional outcome."""
        with self._db_lock:
            conn = sqlite3.connect(str(self.db_path))
            try:
                conn.execute(
                    "UPDATE reflections SET applied = 1, outcome = ? WHERE id = ?",
                    (outcome, reflection_id),
                )
                conn.commit()
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Read — DB Stats (for health check)
    # ------------------------------------------------------------------

    def get_db_stats(self) -> dict:
        """Database size and row counts for health check."""
        conn = self._get_conn()
        try:
            obs_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM observations"
            ).fetchone()["cnt"]
            score_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM scores"
            ).fetchone()["cnt"]
            reflect_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM reflections"
            ).fetchone()["cnt"]

            size_bytes = self.db_path.stat().st_size if self.db_path.exists() else 0

            return {
                "observations": obs_count,
                "scores": score_count,
                "reflections": reflect_count,
                "size_kb": round(size_bytes / 1024, 1),
                "db_path": str(self.db_path),
            }
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def prune(self, retention_days: int = None) -> dict:
        """Delete records older than retention period. Returns counts deleted."""
        days = retention_days or self.retention_days
        cutoff = time.time() - (days * 86400)

        deleted = {"observations": 0, "scores": 0, "reflections": 0}

        with self._db_lock:
            conn = sqlite3.connect(str(self.db_path))
            try:
                # Delete orphaned scores first (observations about to be deleted)
                cursor = conn.execute("""
                    DELETE FROM scores WHERE observation_id IN (
                        SELECT id FROM observations WHERE timestamp < ?
                    )
                """, (cutoff,))
                deleted["scores"] = cursor.rowcount

                cursor = conn.execute(
                    "DELETE FROM observations WHERE timestamp < ?",
                    (cutoff,),
                )
                deleted["observations"] = cursor.rowcount

                cursor = conn.execute(
                    "DELETE FROM reflections WHERE timestamp < ?",
                    (cutoff,),
                )
                deleted["reflections"] = cursor.rowcount

                conn.commit()
            finally:
                conn.close()

        total = sum(deleted.values())
        if total > 0:
            logger.info("EventLogger pruned %d records older than %d days: %s",
                        total, days, deleted)
        return deleted

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_dict(row) -> dict:
        """Convert a Row to dict, parsing metadata JSON."""
        d = dict(row)
        if d.get("metadata"):
            try:
                d["metadata"] = json.loads(d["metadata"])
            except (json.JSONDecodeError, TypeError):
                pass
        return d
