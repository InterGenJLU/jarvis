"""LLM Metrics Tracker — persistent SQLite storage for token usage and performance data.

Tracks every LLM interaction: provider, method, token counts, latency,
skill/intent context, and input method. Provides aggregation queries
for the metrics dashboard.

Singleton access via get_metrics_tracker(config).
"""

import csv
import io
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_instance: Optional["MetricsTracker"] = None


def get_metrics_tracker(config=None) -> Optional["MetricsTracker"]:
    """Get or create the singleton MetricsTracker."""
    global _instance
    if _instance is None and config is not None:
        _instance = MetricsTracker(config)
    return _instance


class MetricsTracker:
    """Persistent LLM metrics storage with aggregation queries."""

    def __init__(self, config):
        self.db_path = Path(config.get(
            "metrics.db_path",
            "/mnt/storage/jarvis/data/metrics.db",
        ))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.retention_days = config.get("metrics.retention_days", 180)
        self._db_lock = threading.Lock()
        self._on_record_callback = None
        self._init_db()
        logger.info(f"MetricsTracker initialized: {self.db_path} "
                     f"(retention={self.retention_days}d)")

    def _init_db(self):
        """Create tables and indexes."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS llm_interactions (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp         REAL NOT NULL,
                    provider          TEXT NOT NULL,
                    method            TEXT NOT NULL,
                    prompt_tokens     INTEGER,
                    completion_tokens INTEGER,
                    estimated_tokens  INTEGER,
                    model             TEXT,
                    latency_ms        REAL,
                    ttft_ms           REAL,
                    skill             TEXT,
                    intent            TEXT,
                    input_method      TEXT,
                    quality_gate      INTEGER DEFAULT 0,
                    is_fallback       INTEGER DEFAULT 0,
                    error             TEXT,
                    session_id        TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_llm_ts ON llm_interactions(timestamp);
                CREATE INDEX IF NOT EXISTS idx_llm_provider ON llm_interactions(provider);
                CREATE INDEX IF NOT EXISTS idx_llm_skill ON llm_interactions(skill);
                CREATE INDEX IF NOT EXISTS idx_llm_method ON llm_interactions(method);
                CREATE INDEX IF NOT EXISTS idx_llm_session ON llm_interactions(session_id);
            """)
            conn.commit()
        finally:
            conn.close()

    def _get_conn(self) -> sqlite3.Connection:
        """Get a new SQLite connection with row_factory set."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def set_on_record(self, callback):
        """Set callback invoked after each record() — used for WebSocket push."""
        self._on_record_callback = callback

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def record(self, *, timestamp=None, provider="unknown", method="unknown",
               prompt_tokens=None, completion_tokens=None, estimated_tokens=None,
               model=None, latency_ms=None, ttft_ms=None, skill=None,
               intent=None, input_method=None, quality_gate=False,
               is_fallback=False, error=None, session_id=None):
        """Insert a single LLM interaction record."""
        if timestamp is None:
            timestamp = time.time()
        row = {
            "timestamp": timestamp,
            "provider": provider,
            "method": method,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "estimated_tokens": estimated_tokens,
            "model": model,
            "latency_ms": latency_ms,
            "ttft_ms": ttft_ms,
            "skill": skill,
            "intent": intent,
            "input_method": input_method,
            "quality_gate": 1 if quality_gate else 0,
            "is_fallback": 1 if is_fallback else 0,
            "error": error,
            "session_id": session_id,
        }
        with self._db_lock:
            conn = sqlite3.connect(str(self.db_path))
            try:
                conn.execute("""
                    INSERT INTO llm_interactions
                        (timestamp, provider, method, prompt_tokens, completion_tokens,
                         estimated_tokens, model, latency_ms, ttft_ms, skill, intent,
                         input_method, quality_gate, is_fallback, error, session_id)
                    VALUES
                        (:timestamp, :provider, :method, :prompt_tokens, :completion_tokens,
                         :estimated_tokens, :model, :latency_ms, :ttft_ms, :skill, :intent,
                         :input_method, :quality_gate, :is_fallback, :error, :session_id)
                """, row)
                conn.commit()
            finally:
                conn.close()

        # Notify dashboard (non-blocking)
        if self._on_record_callback:
            try:
                self._on_record_callback(row)
            except Exception:
                pass  # Dashboard push failure should never break recording

    # ------------------------------------------------------------------
    # Read — Summary
    # ------------------------------------------------------------------

    def get_summary(self, hours=24) -> dict:
        """Aggregated summary for dashboard cards."""
        cutoff = time.time() - (hours * 3600)
        conn = self._get_conn()
        try:
            row = conn.execute("""
                SELECT
                    COUNT(*) as total,
                    SUM(COALESCE(prompt_tokens, 0) + COALESCE(completion_tokens, 0)
                        + COALESCE(estimated_tokens, 0)) as total_tokens,
                    AVG(latency_ms) as avg_latency,
                    AVG(CASE WHEN ttft_ms IS NOT NULL THEN ttft_ms END) as avg_ttft,
                    SUM(CASE WHEN is_fallback = 1 THEN 1 ELSE 0 END) as fallback_count,
                    SUM(CASE WHEN quality_gate = 1 THEN 1 ELSE 0 END) as quality_gate_count,
                    SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) as error_count
                FROM llm_interactions
                WHERE timestamp >= ?
            """, (cutoff,)).fetchone()

            total = row["total"] or 0
            fallback_count = row["fallback_count"] or 0

            # Claude cost estimate (Sonnet pricing: $3/$15 per MTok)
            claude_row = conn.execute("""
                SELECT
                    SUM(COALESCE(prompt_tokens, 0)) as input_tok,
                    SUM(COALESCE(completion_tokens, 0)) as output_tok
                FROM llm_interactions
                WHERE timestamp >= ? AND provider = 'claude'
            """, (cutoff,)).fetchone()
            input_tok = claude_row["input_tok"] or 0
            output_tok = claude_row["output_tok"] or 0
            claude_cost = (input_tok * 3.0 / 1_000_000) + (output_tok * 15.0 / 1_000_000)

            # Provider breakdown
            providers = {}
            for p in conn.execute("""
                SELECT provider, COUNT(*) as cnt
                FROM llm_interactions
                WHERE timestamp >= ?
                GROUP BY provider
            """, (cutoff,)):
                providers[p["provider"]] = p["cnt"]

            # Input method breakdown
            input_methods = {}
            for m in conn.execute("""
                SELECT input_method, COUNT(*) as cnt
                FROM llm_interactions
                WHERE timestamp >= ? AND input_method IS NOT NULL
                GROUP BY input_method
            """, (cutoff,)):
                input_methods[m["input_method"]] = m["cnt"]

            return {
                "total_interactions": total,
                "total_tokens": row["total_tokens"] or 0,
                "avg_latency_ms": round(row["avg_latency"] or 0, 1),
                "avg_ttft_ms": round(row["avg_ttft"] or 0, 1),
                "fallback_count": fallback_count,
                "fallback_rate": round(fallback_count / total * 100, 1) if total else 0,
                "quality_gate_count": row["quality_gate_count"] or 0,
                "error_count": row["error_count"] or 0,
                "claude_cost_estimate": round(claude_cost, 4),
                "provider_breakdown": providers,
                "input_method_breakdown": input_methods,
                "hours": hours,
            }
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Read — Time Series
    # ------------------------------------------------------------------

    def get_timeseries(self, hours=24, bucket="hour") -> list:
        """Grouped aggregates for chart rendering.

        bucket: 'hour' or 'day'
        Returns list of dicts with bucket_start, interactions, tokens, avg_latency.
        """
        cutoff = time.time() - (hours * 3600)
        bucket_seconds = 3600 if bucket == "hour" else 86400
        conn = self._get_conn()
        try:
            rows = conn.execute("""
                SELECT
                    CAST(timestamp / ? AS INTEGER) * ? as bucket_start,
                    COUNT(*) as interactions,
                    SUM(COALESCE(prompt_tokens, 0)) as prompt_tok,
                    SUM(COALESCE(completion_tokens, 0)) as completion_tok,
                    SUM(COALESCE(estimated_tokens, 0)) as estimated_tok,
                    AVG(latency_ms) as avg_latency,
                    SUM(CASE WHEN provider = 'qwen' THEN 1 ELSE 0 END) as qwen_count,
                    SUM(CASE WHEN provider = 'claude' THEN 1 ELSE 0 END) as claude_count
                FROM llm_interactions
                WHERE timestamp >= ?
                GROUP BY bucket_start
                ORDER BY bucket_start
            """, (bucket_seconds, bucket_seconds, cutoff)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Read — Skill Average Latency
    # ------------------------------------------------------------------

    def get_skill_avg_latency(self, skill_name: str, hours: int = 24) -> float:
        """Average latency (ms) for a specific skill over the given window.

        Returns 0.0 if no data available.
        """
        cutoff = time.time() - (hours * 3600)
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT AVG(latency_ms) as avg_lat FROM llm_interactions "
                "WHERE skill = ? AND timestamp >= ? AND latency_ms IS NOT NULL",
                (skill_name, cutoff),
            ).fetchone()
            return round(row["avg_lat"] or 0.0, 1)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Read — Skill Error Rate
    # ------------------------------------------------------------------

    def get_skill_error_rate(self, skill_name: str, hours: int = 24) -> float:
        """Error rate (0.0-1.0) for a specific skill over the given window.

        Returns 0.0 if no data or no interactions.
        """
        cutoff = time.time() - (hours * 3600)
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) as total, "
                "SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) as errors "
                "FROM llm_interactions "
                "WHERE skill = ? AND timestamp >= ?",
                (skill_name, cutoff),
            ).fetchone()
            total = row["total"] or 0
            errors = row["errors"] or 0
            return round(errors / total, 3) if total > 0 else 0.0
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Read — Skill Breakdown
    # ------------------------------------------------------------------

    def get_skill_breakdown(self, hours=24) -> list:
        """Interactions grouped by skill."""
        cutoff = time.time() - (hours * 3600)
        conn = self._get_conn()
        try:
            rows = conn.execute("""
                SELECT
                    COALESCE(skill, 'LLM Fallback') as skill,
                    COUNT(*) as interactions,
                    SUM(COALESCE(prompt_tokens, 0) + COALESCE(completion_tokens, 0)
                        + COALESCE(estimated_tokens, 0)) as total_tokens,
                    AVG(latency_ms) as avg_latency
                FROM llm_interactions
                WHERE timestamp >= ?
                GROUP BY skill
                ORDER BY interactions DESC
            """, (cutoff,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Read — Paginated Interactions (data explorer)
    # ------------------------------------------------------------------

    def _build_where(self, filters: dict) -> tuple:
        """Build WHERE clause and params from filter dict."""
        clauses = []
        params = []

        if filters.get("start"):
            clauses.append("timestamp >= ?")
            params.append(float(filters["start"]))
        if filters.get("end"):
            clauses.append("timestamp <= ?")
            params.append(float(filters["end"]))
        if filters.get("provider"):
            clauses.append("provider = ?")
            params.append(filters["provider"])
        if filters.get("skill"):
            clauses.append("skill = ?")
            params.append(filters["skill"])
        if filters.get("method"):
            clauses.append("method = ?")
            params.append(filters["method"])
        if filters.get("input_method"):
            clauses.append("input_method = ?")
            params.append(filters["input_method"])
        if filters.get("error_only"):
            clauses.append("error IS NOT NULL")
        if filters.get("fallback_only"):
            clauses.append("is_fallback = 1")

        where = " AND ".join(clauses) if clauses else "1=1"
        return where, params

    def get_interactions(self, offset=0, limit=50, filters=None) -> dict:
        """Paginated raw interaction data for the data explorer."""
        filters = filters or {}
        where, params = self._build_where(filters)
        conn = self._get_conn()
        try:
            total = conn.execute(
                f"SELECT COUNT(*) as cnt FROM llm_interactions WHERE {where}",
                params,
            ).fetchone()["cnt"]

            rows = conn.execute(
                f"""SELECT * FROM llm_interactions
                    WHERE {where}
                    ORDER BY timestamp DESC
                    LIMIT ? OFFSET ?""",
                params + [limit, offset],
            ).fetchall()

            return {
                "total": total,
                "offset": offset,
                "limit": limit,
                "rows": [dict(r) for r in rows],
            }
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Read — Filter Options (for dropdown population)
    # ------------------------------------------------------------------

    def get_filter_options(self) -> dict:
        """Distinct values for filter dropdowns."""
        conn = self._get_conn()
        try:
            providers = [r[0] for r in conn.execute(
                "SELECT DISTINCT provider FROM llm_interactions ORDER BY provider"
            ).fetchall()]
            skills = [r[0] for r in conn.execute(
                "SELECT DISTINCT skill FROM llm_interactions WHERE skill IS NOT NULL ORDER BY skill"
            ).fetchall()]
            methods = [r[0] for r in conn.execute(
                "SELECT DISTINCT method FROM llm_interactions ORDER BY method"
            ).fetchall()]
            input_methods = [r[0] for r in conn.execute(
                "SELECT DISTINCT input_method FROM llm_interactions WHERE input_method IS NOT NULL ORDER BY input_method"
            ).fetchall()]
            return {
                "providers": providers,
                "skills": skills,
                "methods": methods,
                "input_methods": input_methods,
            }
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def export_csv(self, filters=None) -> str:
        """Generate CSV string of filtered interactions."""
        filters = filters or {}
        where, params = self._build_where(filters)
        conn = self._get_conn()
        try:
            rows = conn.execute(
                f"""SELECT * FROM llm_interactions
                    WHERE {where}
                    ORDER BY timestamp DESC""",
                params,
            ).fetchall()

            output = io.StringIO()
            if rows:
                writer = csv.DictWriter(output, fieldnames=rows[0].keys())
                writer.writeheader()
                for row in rows:
                    writer.writerow(dict(row))
            return output.getvalue()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def prune(self, retention_days=None):
        """Delete interactions older than retention period."""
        days = retention_days or self.retention_days
        cutoff = time.time() - (days * 86400)
        with self._db_lock:
            conn = sqlite3.connect(str(self.db_path))
            try:
                cursor = conn.execute(
                    "DELETE FROM llm_interactions WHERE timestamp < ?",
                    (cutoff,),
                )
                conn.commit()
                if cursor.rowcount > 0:
                    logger.info(f"Pruned {cursor.rowcount} metrics older than {days} days")
            finally:
                conn.close()

    def get_db_stats(self) -> dict:
        """Database size and row count for health check."""
        conn = self._get_conn()
        try:
            count = conn.execute(
                "SELECT COUNT(*) as cnt FROM llm_interactions"
            ).fetchone()["cnt"]
            size_bytes = self.db_path.stat().st_size if self.db_path.exists() else 0
            return {
                "row_count": count,
                "size_kb": round(size_bytes / 1024, 1),
                "db_path": str(self.db_path),
            }
        finally:
            conn.close()
