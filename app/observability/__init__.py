"""Observability: structured logging, metrics, and deep health checks.

M6-6 (日志与监控): provides a single import surface for:

- :func:`configure_logging` — root logger setup driven by
  ``system_settings.log_level``;
- :func:`record_db_health_snapshot` — persist a DB health snapshot row;
- :func:`get_metrics` / :func:`format_metrics_prometheus` — ``/metrics``
  endpoint helpers (Prometheus exposition format);
- :func:`deep_health_check` — synthesized status for a deep health endpoint;
- :class:`RequestContextMiddleware` + :func:`get_request_logger` —
  per-request id tracing and structured request logs.

All public functions are safe to call from FastAPI routes and the
``JobManager`` background loop. They never raise on transient DB errors:
metrics degrade to zero counts and health checks report ``unhealthy``
instead.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.db import Database, utc_now

__all__ = [
    "configure_logging",
    "record_db_health_snapshot",
    "get_metrics",
    "format_metrics_prometheus",
    "deep_health_check",
    "RequestContextMiddleware",
    "get_request_logger",
]

logger = logging.getLogger(__name__)

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_DEFAULT_LEVEL = "INFO"

# Tables tracked in every DB health snapshot (M6-6 spec).
_HEALTH_SNAPSHOT_TABLES = (
    "rule_findings",
    "policy_documents",
    "scan_jobs",
    "ocr_queue",
    "rectification_tasks",
    "operation_audits",
)

# scan_jobs statuses counted by get_metrics.
_JOB_STATUSES = ("pending", "running", "completed", "failed")
# ocr_queue statuses counted by get_metrics.
_OCR_STATUSES = ("pending", "running", "ok", "failed")
# rectification_tasks status groupings (see m5 schema CHECK constraint).
_RECT_PENDING = ("created", "assigned", "notified", "reopened")
_RECT_IN_PROGRESS = ("feedback_uploaded", "rechecking")
_RECT_PASSED = ("passed", "signoff")
_RECT_FAILED = ("failed",)
# rule_findings status values (see findings_model.MACHINE_* / HUMAN_*).
_FINDING_CONFIRMED = "confirmed_by_rule"
_FINDING_SUSPECTED = "suspected"
_FINDING_AUTO_EXCLUDED = "auto_excluded"
_FINDING_HUMAN_EXCLUDED = "human_dismissed"


# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------


def _resolve_log_level(settings: Any | None) -> str:
    """Resolve the configured log level string.

    Precedence:
    1. ``settings.log_level`` (or ``settings["log_level"]``) when provided;
    2. ``system_settings.log_level`` row;
    3. ``INFO`` default.

    Failures (DB not yet initialized, table missing) silently fall back to
    the default — logging must never crash startup.
    """
    if settings is not None:
        level = getattr(settings, "log_level", None)
        if level is None and isinstance(settings, dict):
            level = settings.get("log_level")
        if level:
            return str(level).upper()
    try:
        db = Database()
        with db.connect() as conn:
            row = conn.execute(
                "SELECT value FROM system_settings WHERE key=?", ("log_level",)
            ).fetchone()
        if row and row["value"]:
            return str(row["value"]).upper()
    except Exception:
        # DB not initialized yet or table missing — fall back to default.
        pass
    return _DEFAULT_LEVEL


def configure_logging(settings: Any | None = None) -> None:
    """Configure the root logger.

    Idempotent: repeated calls replace the owned handler instead of stacking
    handlers. Reads ``log_level`` from ``system_settings`` when ``settings``
    is not provided. Silences noisy third-party loggers (urllib3).
    """
    level_name = _resolve_log_level(settings)
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)

    formatter = logging.Formatter(_LOG_FORMAT)

    # Reuse the handler we previously installed to avoid duplicate lines.
    own_handler: logging.StreamHandler | None = None
    for handler in root.handlers:
        if getattr(handler, "_policy_observability", False):
            own_handler = handler  # type: ignore[assignment]
            break
    if own_handler is None:
        own_handler = logging.StreamHandler()
        own_handler._policy_observability = True  # type: ignore[attr-defined]
        root.addHandler(own_handler)
    own_handler.setFormatter(formatter)
    own_handler.setLevel(level)

    # Silence noisy libraries.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# DB health snapshot
# ---------------------------------------------------------------------------


def _pragma_scalar(conn, pragma: str) -> Any:
    """Return the first column of the first row of a PRAGMA."""
    row = conn.execute(f"PRAGMA {pragma}").fetchone()
    if not row:
        return None
    return row[0]


def _safe_getsize(path: Path) -> int:
    try:
        return int(os.path.getsize(path))
    except OSError:
        return 0


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _table_counts(conn, tables: tuple[str, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in tables:
        try:
            counts[table] = int(
                conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
        except Exception:
            counts[table] = 0
    return counts


def _index_list(conn) -> list[str]:
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' ORDER BY name"
        ).fetchall()
        return [str(r[0]) for r in rows]
    except Exception:
        return []


def record_db_health_snapshot(db: Database, notes: str = "") -> dict[str, Any]:
    """Capture a DB health snapshot and persist it to ``db_health_snapshots``.

    Captures file sizes, key PRAGMAs (``page_count``, ``freelist_count``,
    ``journal_mode``, ``quick_check``), table counts for the operational
    tables, and the full index list. Returns the snapshot as a dict
    (also INSERTed into the table). PRAGMA failures degrade to 0 / "".
    """
    path = Path(db.path)
    db_size = _safe_getsize(path)
    wal_path = Path(str(path) + "-wal")
    wal_size = _safe_getsize(wal_path)

    with db.connect() as conn:
        page_count = _to_int(_pragma_scalar(conn, "page_count"))
        freelist_count = _to_int(_pragma_scalar(conn, "freelist_count"))
        journal_mode = str(_pragma_scalar(conn, "journal_mode") or "")
        integrity = str(_pragma_scalar(conn, "quick_check") or "")
        table_counts = _table_counts(conn, _HEALTH_SNAPSHOT_TABLES)
        index_list = _index_list(conn)

        snapshot_at = utc_now()
        cur = conn.execute(
            """INSERT INTO db_health_snapshots(
                snapshot_at, db_size_bytes, wal_size_bytes, page_count,
                freelist_count, journal_mode, table_counts_json,
                index_list_json, notes
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                snapshot_at,
                db_size,
                wal_size,
                page_count,
                freelist_count,
                journal_mode,
                json.dumps(table_counts, ensure_ascii=False),
                json.dumps(index_list, ensure_ascii=False),
                notes,
            ),
        )
        snapshot_id = int(cur.lastrowid)

    return {
        "id": snapshot_id,
        "snapshot_at": snapshot_at,
        "db_size_bytes": db_size,
        "wal_size_bytes": wal_size,
        "page_count": page_count,
        "freelist_count": freelist_count,
        "journal_mode": journal_mode,
        "integrity_check": integrity,
        "table_counts": table_counts,
        "index_list": index_list,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _count_total(conn, table: str) -> int:
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except Exception:
        return 0


def _count_where(conn, table: str, where: str, params: tuple) -> int:
    try:
        return int(
            conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {where}", params
            ).fetchone()[0]
        )
    except Exception:
        return 0


def _count_in(conn, table: str, column: str, values: tuple[str, ...]) -> int:
    if not values:
        return 0
    placeholders = ",".join("?" * len(values))
    try:
        return int(
            conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({placeholders})",
                values,
            ).fetchone()[0]
        )
    except Exception:
        return 0


def _count_finding_status(conn, column: str, value: str) -> int:
    try:
        return int(
            conn.execute(
                f"SELECT COUNT(*) FROM rule_findings "
                f"WHERE COALESCE(is_current,1)=1 AND {column}=?",
                (value,),
            ).fetchone()[0]
        )
    except Exception:
        return 0


def _count_overdue(conn) -> int:
    now = utc_now()
    try:
        return int(
            conn.execute(
                """SELECT COUNT(*) FROM rectification_tasks
                WHERE status NOT IN ('signoff','failed')
                  AND deadline IS NOT NULL AND deadline!='' AND deadline<?""",
                (now,),
            ).fetchone()[0]
        )
    except Exception:
        return 0


def get_metrics(db: Database) -> dict[str, Any]:
    """Collect a metrics snapshot suitable for a Prometheus ``/metrics`` endpoint.

    Returns a nested dict with ``db``, ``jobs``, ``ocr_queue``,
    ``rectification``, ``findings``, and ``audits`` buckets. All counts are
    integers; transient DB errors degrade to 0.
    """
    metrics: dict[str, Any] = {}
    path = Path(db.path)
    with db.connect() as conn:
        metrics["db"] = {
            "size_bytes": _safe_getsize(path),
            "wal_size_bytes": _safe_getsize(Path(str(path) + "-wal")),
            "page_count": _to_int(_pragma_scalar(conn, "page_count")),
            "freelist_count": _to_int(_pragma_scalar(conn, "freelist_count")),
            "journal_mode": str(_pragma_scalar(conn, "journal_mode") or ""),
        }

        metrics["jobs"] = {
            "total": _count_total(conn, "scan_jobs"),
            **{
                status: _count_where(conn, "scan_jobs", "status=?", (status,))
                for status in _JOB_STATUSES
            },
        }

        metrics["ocr_queue"] = {
            status: _count_where(conn, "ocr_queue", "status=?", (status,))
            for status in _OCR_STATUSES
        }

        metrics["rectification"] = {
            "pending": _count_in(conn, "rectification_tasks", "status", _RECT_PENDING),
            "in_progress": _count_in(conn, "rectification_tasks", "status", _RECT_IN_PROGRESS),
            "passed": _count_in(conn, "rectification_tasks", "status", _RECT_PASSED),
            "failed": _count_in(conn, "rectification_tasks", "status", _RECT_FAILED),
            "overdue": _count_overdue(conn),
        }

        # total: all current findings (no status filter); buckets filtered by status.
        try:
            findings_total = int(
                conn.execute(
                    "SELECT COUNT(*) FROM rule_findings WHERE COALESCE(is_current,1)=1"
                ).fetchone()[0]
            )
        except Exception:
            findings_total = 0
        metrics["findings"] = {
            "total": findings_total,
            "confirmed": _count_finding_status(conn, "machine_status", _FINDING_CONFIRMED),
            "suspected": _count_finding_status(conn, "machine_status", _FINDING_SUSPECTED),
            "auto_excluded": _count_finding_status(conn, "machine_status", _FINDING_AUTO_EXCLUDED),
            "human_excluded": _count_finding_status(conn, "human_status", _FINDING_HUMAN_EXCLUDED),
        }

        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        metrics["audits"] = {
            "total_24h": _count_where(
                conn, "operation_audits", "created_at > ?", (cutoff,)
            ),
        }
    return metrics


def _emit_gauge(lines: list[str], name: str, help_text: str, value: Any) -> None:
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} gauge")
    lines.append(f"{name} {_to_int(value)}")


def format_metrics_prometheus(metrics: dict[str, Any]) -> str:
    """Render a metrics dict (from :func:`get_metrics`) as Prometheus text.

    Emits ``# HELP`` / ``# TYPE`` comments followed by ``name value`` lines
    for every gauge. Non-numeric fields (e.g. ``journal_mode``) are omitted
    because Prometheus exposition requires numeric values.
    """
    lines: list[str] = []

    db = metrics.get("db", {})
    _emit_gauge(lines, "policy_db_size_bytes", "Database file size in bytes", db.get("size_bytes", 0))
    _emit_gauge(lines, "policy_db_wal_size_bytes", "WAL file size in bytes", db.get("wal_size_bytes", 0))
    _emit_gauge(lines, "policy_db_page_count", "Database page count", db.get("page_count", 0))
    _emit_gauge(lines, "policy_db_freelist_count", "Database freelist page count", db.get("freelist_count", 0))

    jobs = metrics.get("jobs", {})
    _emit_gauge(lines, "policy_jobs_total", "Total scan jobs", jobs.get("total", 0))
    for status in _JOB_STATUSES:
        _emit_gauge(lines, f"policy_jobs_{status}", f"Scan jobs in {status} state", jobs.get(status, 0))

    ocr = metrics.get("ocr_queue", {})
    for status in _OCR_STATUSES:
        _emit_gauge(
            lines,
            f"policy_ocr_queue_{status}",
            f"OCR queue items in {status} state",
            ocr.get(status, 0),
        )

    rect = metrics.get("rectification", {})
    for label in ("pending", "in_progress", "passed", "failed", "overdue"):
        _emit_gauge(
            lines,
            f"policy_rectification_{label}",
            f"Rectification tasks {label}",
            rect.get(label, 0),
        )

    findings = metrics.get("findings", {})
    _emit_gauge(lines, "policy_findings_total", "Current findings (is_current=1)", findings.get("total", 0))
    for label in ("confirmed", "suspected", "auto_excluded", "human_excluded"):
        _emit_gauge(
            lines,
            f"policy_findings_{label}",
            f"Findings bucket {label}",
            findings.get(label, 0),
        )

    audits = metrics.get("audits", {})
    _emit_gauge(
        lines,
        "policy_audits_total_24h",
        "Operation audits in the last 24h",
        audits.get("total_24h", 0),
    )

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Deep health check
# ---------------------------------------------------------------------------


def deep_health_check(db: Database) -> dict[str, Any]:
    """Synthesize a deep health snapshot.

    Returns a dict with ``status`` (``ok`` | ``degraded`` | ``unhealthy``),
    ``database`` (connected flag, journal mode, integrity), ``disk`` sizes,
    ``counts`` (active jobs, pending OCR, overdue rectifications), and a UTC
    ISO ``timestamp``.

    ``status`` is ``unhealthy`` when the DB is unreachable or the integrity
    check does not return ``ok``; ``degraded`` when queues back up
    (``ocr_pending > 50`` or ``rectification_overdue > 10``); otherwise ``ok``.

    The ``database`` sub-dict exposes ``integrity_check`` / ``quick_check`` /
    ``foreign_key_check`` explicitly so callers can audit each PRAGMA result
    independently (the legacy ``integrity`` alias is retained for backward
    compatibility and mirrors ``quick_check``).
    """
    connected = False
    journal_mode = ""
    integrity = ""
    integrity_check = ""
    quick_check = ""
    fk_check: list[Any] = []
    db_size = 0
    wal_size = 0
    jobs_active = 0
    ocr_pending = 0
    rectification_overdue = 0
    try:
        with db.connect() as conn:
            connected = True
            journal_mode = str(_pragma_scalar(conn, "journal_mode") or "")
            quick_check = str(_pragma_scalar(conn, "quick_check") or "")
            integrity_check = str(_pragma_scalar(conn, "integrity_check") or "")
            integrity = quick_check  # backward-compat alias
            try:
                fk_check = [
                    (r[0] if not isinstance(r, tuple) else r) for r in
                    conn.execute("PRAGMA foreign_key_check").fetchall()
                ]
            except Exception:
                fk_check = []
            path = Path(db.path)
            db_size = _safe_getsize(path)
            wal_size = _safe_getsize(Path(str(path) + "-wal"))
            jobs_active = _count_in(
                conn, "scan_jobs", "status", ("pending", "running", "cooling")
            )
            ocr_pending = _count_where(conn, "ocr_queue", "status=?", ("pending",))
            rectification_overdue = _count_overdue(conn)
    except Exception:
        connected = False

    if not connected or integrity.strip() != "ok":
        status = "unhealthy"
    elif ocr_pending > 50 or rectification_overdue > 10:
        status = "degraded"
    else:
        status = "ok"

    return {
        "status": status,
        "database": {
            "connected": connected,
            "journal_mode": journal_mode,
            "integrity": integrity.strip(),
            "integrity_check": integrity_check.strip(),
            "quick_check": quick_check.strip(),
            "foreign_key_check": fk_check,
        },
        "disk": {
            "db_size_bytes": db_size,
            "wal_size_bytes": wal_size,
        },
        "counts": {
            "jobs_active": jobs_active,
            "ocr_pending": ocr_pending,
            "rectification_overdue": rectification_overdue,
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Request context middleware
# ---------------------------------------------------------------------------


class _RequestLoggerAdapter(logging.LoggerAdapter):
    """Inject ``request_id`` into every log message."""

    def process(self, msg, kwargs):
        request_id = self.extra.get("request_id", "") if self.extra else ""
        return f"[{request_id}] {msg}", kwargs


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach a per-request UUID and log request start/end.

    Generates ``request_id`` (uuid4 hex), stores it on
    ``request.state.request_id`` and a bound LoggerAdapter on
    ``request.state.request_logger``. Emits two INFO log lines per request
    with method, path, status, and duration in milliseconds. The id is also
    returned to clients via the ``X-Request-ID`` response header.
    """

    async def dispatch(self, request: Request, call_next):
        request_id = uuid.uuid4().hex
        request.state.request_id = request_id
        request_logger = _RequestLoggerAdapter(logger, {"request_id": request_id})
        request.state.request_logger = request_logger

        method = request.method
        path = request.url.path
        start = perf_counter()
        request_logger.info("request.start %s %s", method, path)
        try:
            response: Response = await call_next(request)
        except Exception:
            duration_ms = (perf_counter() - start) * 1000
            request_logger.exception(
                "request.error %s %s status=500 duration_ms=%.2f",
                method,
                path,
                duration_ms,
            )
            raise
        duration_ms = (perf_counter() - start) * 1000
        response.headers["X-Request-ID"] = request_id
        request_logger.info(
            "request.end %s %s status=%s duration_ms=%.2f",
            method,
            path,
            response.status_code,
            duration_ms,
        )
        return response


def get_request_logger(request: Request) -> logging.LoggerAdapter:
    """Return a :class:`logging.LoggerAdapter` bound to the request's id.

    Prefer the adapter stored on ``request.state`` by
    :class:`RequestContextMiddleware`; fall back to constructing one from
    ``request.state.request_id`` (empty string when middleware has not run,
    e.g. background tasks).
    """
    stored = getattr(request.state, "request_logger", None)
    if stored is not None:
        return stored
    request_id = getattr(request.state, "request_id", "")
    return _RequestLoggerAdapter(logger, {"request_id": request_id})
