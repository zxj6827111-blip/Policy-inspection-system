"""M6-4 OCR 队列管理器：入队 / 认领 / 完成 / 重试 / 取消 / 批处理.

提供持久化的 OCR 异步处理队列，支持：
- 优先级排序（priority 数字越小越优先）与最早请求优先（FIFO）。
- 最大重试次数（max_attempts）控制：失败后自动重新入队，超过次数则终态失败。
- Worker 认领（next_pending 原子置 running）与去重（同 document_id 已有 pending/running 时复用）。
- 串行/并行批处理（run_batch），并行度由 system_settings.ocr_max_workers 控制（默认 1=串行）。

解耦设计：run_one 接受可注入的 `processor` 可调用对象，便于测试替换；
processor 为 None 时仅认领并标记 running（用于测试）。需要真实 OCR 时，
使用 make_default_processor(db) 构造一个读取 policy_documents.pdf_path
并调用 app.ocr.ocr_pdf 的处理器。

数据库访问约定：`from app.db import Database, utc_now`，`with db.connect() as conn:`。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from app.db import Database, utc_now

logger = logging.getLogger(__name__)

# ocr_status 中视为「成功终态」的集合：队列行 status 置 'ok'。
_OK_TERMINAL = {"ok", "skipped", "not_needed"}

DEFAULT_WORKER = "worker-1"
DEFAULT_BATCH_WORKER = "worker-batch"

# 处理器签名：processor(document_id) -> dict，dict 约定键：
#   ocr_status: str, ocr_confidence: float, pages_processed: int,
#   pages_total: int, error: str, ocr_text: str
Processor = Callable[[int], dict[str, Any]]

__all__ = [
    "enqueue",
    "next_pending",
    "complete",
    "cancel",
    "list_queue",
    "queue_depth",
    "run_one",
    "run_batch",
    "make_default_processor",
]


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

def _read_setting(conn, key: str, default: str) -> str:
    """从 system_settings 读取一个键值，缺失或空时返回 default。"""
    row = conn.execute(
        "SELECT value FROM system_settings WHERE key=?", (key,)
    ).fetchone()
    if row is None:
        return default
    value = row["value"]
    return value if value not in (None, "") else default


def _resolve_priority(conn, priority: int | None) -> int:
    """priority 为 None 时从 system_settings.ocr_default_priority 读取（默认 100）。"""
    if priority is not None:
        return priority
    raw = _read_setting(conn, "ocr_default_priority", "100")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 100


def _resolve_max_attempts(conn, max_attempts: int | None) -> int:
    """max_attempts 为 None 时从 system_settings.ocr_default_max_attempts 读取（默认 3）。"""
    if max_attempts is not None:
        return max_attempts
    raw = _read_setting(conn, "ocr_default_max_attempts", "3")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 3


def _resolve_max_workers(conn) -> int:
    """读取 system_settings.ocr_max_workers（默认 1=串行），至少为 1。"""
    raw = _read_setting(conn, "ocr_max_workers", "1")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 1


# ---------------------------------------------------------------------------
# 1. enqueue
# ---------------------------------------------------------------------------

def enqueue(
    db: Database,
    document_id: int,
    job_id: int | None = None,
    priority: int | None = None,
    max_attempts: int | None = None,
    requested_by: str = "system",
) -> int:
    """将一个文档入队等待 OCR。

    priority / max_attempts 为 None 时，分别从 system_settings 的
    ocr_default_priority（默认 100）与 ocr_default_max_attempts（默认 3）读取。

    去重：若该 document_id 已存在 status 为 pending/running 的队列项，
    直接返回既有项 id，不重复入队。

    返回：队列项 id。
    """
    now = utc_now()
    with db.connect() as conn:
        existing = conn.execute(
            """SELECT id FROM ocr_queue
               WHERE document_id=? AND status IN ('pending','running')
               ORDER BY id DESC LIMIT 1""",
            (document_id,),
        ).fetchone()
        if existing is not None:
            return int(existing["id"])

        pri = _resolve_priority(conn, priority)
        attempts_max = _resolve_max_attempts(conn, max_attempts)
        cur = conn.execute(
            """INSERT INTO ocr_queue(
                   document_id, job_id, status, priority, attempts, max_attempts,
                   requested_at, requested_by
               ) VALUES(?, ?, 'pending', ?, 0, ?, ?, ?)""",
            (document_id, job_id, pri, attempts_max, now, requested_by),
        )
        return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# 2. next_pending
# ---------------------------------------------------------------------------

def next_pending(db: Database, worker_id: str = DEFAULT_WORKER) -> dict[str, Any] | None:
    """原子地认领最高优先级（priority 最小）、最早的 pending 项。

    使用 `UPDATE ... WHERE id=(SELECT ... LIMIT 1) RETURNING *` 模式，
    认领的同时将 status 置 'running'、started_at=now、worker_id=worker_id、
    attempts=attempts+1。SQLite >= 3.35 支持 RETURNING。

    返回：认领到的队列行 dict，无 pending 项时返回 None。
    """
    now = utc_now()
    with db.connect() as conn:
        row = conn.execute(
            """UPDATE ocr_queue
               SET status='running', started_at=?, worker_id=?, attempts=attempts+1
               WHERE id=(
                   SELECT id FROM ocr_queue
                   WHERE status='pending'
                   ORDER BY priority ASC, requested_at ASC
                   LIMIT 1
               )
               RETURNING *""",
            (now, worker_id),
        ).fetchone()
        return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# 3. complete
# ---------------------------------------------------------------------------

def complete(
    db: Database,
    item_id: int,
    ocr_status: str,
    ocr_confidence: float | None = None,
    pages_processed: int = 0,
    pages_total: int = 0,
    error: str = "",
) -> dict[str, Any]:
    """标记一个队列项的处理结果。

    规则：
    - ocr_status in ('ok','skipped','not_needed')：status='ok'，completed_at=now。
    - ocr_status == 'failed' 且 attempts < max_attempts：status='pending'（重试），
      started_at=NULL，worker_id=''，last_error=error。
    - ocr_status == 'failed' 且 attempts >= max_attempts：status='failed'，
      completed_at=now，last_error=error。

    本函数仅更新 ocr_queue 行；policy_documents 的 ocr 字段更新由调用方
    （如 run_one）负责。

    返回：{"item_id": int, "final_status": str, "retried": bool}。
    """
    now = utc_now()
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM ocr_queue WHERE id=?", (item_id,)
        ).fetchone()
        if row is None:
            return {"item_id": item_id, "final_status": "missing", "retried": False}

        attempts = int(row["attempts"])
        max_attempts = int(row["max_attempts"])

        if ocr_status in _OK_TERMINAL:
            final_status = "ok"
            conn.execute(
                """UPDATE ocr_queue
                   SET status='ok', completed_at=?, ocr_status=?, ocr_confidence=?,
                       pages_processed=?, pages_total=?, last_error=?
                   WHERE id=?""",
                (now, ocr_status, ocr_confidence,
                 pages_processed, pages_total, error, item_id),
            )
            retried = False
        elif ocr_status == "failed":
            if attempts < max_attempts:
                # 重新入队等待重试
                final_status = "pending"
                conn.execute(
                    """UPDATE ocr_queue
                       SET status='pending', started_at=NULL, worker_id='',
                           last_error=?, ocr_status=?, ocr_confidence=?,
                           pages_processed=?, pages_total=?
                       WHERE id=?""",
                    (error, ocr_status, ocr_confidence,
                     pages_processed, pages_total, item_id),
                )
                retried = True
            else:
                final_status = "failed"
                conn.execute(
                    """UPDATE ocr_queue
                       SET status='failed', completed_at=?, last_error=?,
                           ocr_status=?, ocr_confidence=?,
                           pages_processed=?, pages_total=?
                       WHERE id=?""",
                    (now, error, ocr_status, ocr_confidence,
                     pages_processed, pages_total, item_id),
                )
                retried = False
        else:
            # 未预期的 ocr_status：按成功终态处理，保留 ocr_status 以便排查。
            final_status = "ok"
            conn.execute(
                """UPDATE ocr_queue
                   SET status='ok', completed_at=?, ocr_status=?, ocr_confidence=?,
                       pages_processed=?, pages_total=?, last_error=?
                   WHERE id=?""",
                (now, ocr_status, ocr_confidence,
                 pages_processed, pages_total, error, item_id),
            )
            retried = False

        return {"item_id": item_id, "final_status": final_status, "retried": retried}


# ---------------------------------------------------------------------------
# 4. cancel
# ---------------------------------------------------------------------------

def cancel(db: Database, item_id: int, reason: str = "") -> bool:
    """取消一个队列项：status='cancelled'，completed_at=now，last_error=reason。

    返回：是否实际更新了一行（item 存在则 True）。
    """
    now = utc_now()
    with db.connect() as conn:
        cur = conn.execute(
            """UPDATE ocr_queue
               SET status='cancelled', completed_at=?, last_error=?
               WHERE id=?""",
            (now, reason, item_id),
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# 5. list_queue
# ---------------------------------------------------------------------------

def list_queue(
    db: Database, status: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    """列出队列项，按 priority ASC、requested_at ASC 排序。

    status 非 None 时仅返回该状态的行。limit 限制返回行数。
    """
    with db.connect() as conn:
        if status:
            rows = conn.execute(
                """SELECT * FROM ocr_queue
                   WHERE status=?
                   ORDER BY priority ASC, requested_at ASC
                   LIMIT ?""",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM ocr_queue
                   ORDER BY priority ASC, requested_at ASC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 6. queue_depth
# ---------------------------------------------------------------------------

def queue_depth(db: Database) -> dict[str, int]:
    """返回各状态计数：pending/running/ok/failed/cancelled + total。"""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM ocr_queue GROUP BY status"
        ).fetchall()
    counts: dict[str, int] = {
        "pending": 0, "running": 0, "ok": 0, "failed": 0, "cancelled": 0,
    }
    total = 0
    for r in rows:
        s = r["status"]
        n = int(r["n"])
        if s in counts:
            counts[s] = n
        total += n
    counts["total"] = total
    return counts


# ---------------------------------------------------------------------------
# 7. 默认处理器 + run_one
# ---------------------------------------------------------------------------

def make_default_processor(db: Database) -> Processor:
    """构造默认 OCR 处理器。

    从 policy_documents.pdf_path 读取缓存的 PDF 字节，调用 app.ocr.ocr_pdf
    执行同步 Tesseract OCR，返回约定键的 dict。

    若文档不存在、无 pdf_path 或缓存文件丢失，返回 ocr_status='failed' 的结果。
    """
    from app.ocr import OcrResult, ocr_pdf  # 延迟导入，避免循环依赖与启动期开销。

    def _processor(document_id: int) -> dict[str, Any]:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT pdf_path FROM policy_documents WHERE id=?", (document_id,)
            ).fetchone()
        if row is None:
            return {
                "ocr_status": "failed", "ocr_confidence": 0.0,
                "pages_processed": 0, "pages_total": 0,
                "error": f"document {document_id} not found", "ocr_text": "",
            }
        pdf_path = row["pdf_path"] or ""
        if not pdf_path:
            return {
                "ocr_status": "failed", "ocr_confidence": 0.0,
                "pages_processed": 0, "pages_total": 0,
                "error": "no pdf_path cached", "ocr_text": "",
            }
        p = Path(pdf_path)
        if not p.exists():
            return {
                "ocr_status": "failed", "ocr_confidence": 0.0,
                "pages_processed": 0, "pages_total": 0,
                "error": f"pdf cache missing: {pdf_path}", "ocr_text": "",
            }
        payload = p.read_bytes()
        result: OcrResult = ocr_pdf(payload)
        return {
            "ocr_status": result.status,
            "ocr_confidence": result.avg_confidence,
            "pages_processed": result.page_count,
            "pages_total": result.page_count,
            "error": result.error,
            "ocr_text": result.full_text,
        }

    return _processor


def run_one(
    db: Database,
    worker_id: str = DEFAULT_WORKER,
    processor: Processor | None = None,
) -> dict[str, Any] | None:
    """认领并处理一个 pending 队列项。

    流程：
    1. next_pending 认领最高优先级 pending 项（无则返回 None）。
    2. 若 processor 为 None：仅完成认领（已置 running），用于测试，直接返回。
    3. 否则调用 processor(document_id)，捕获异常并以 failed 结果兜底。
    4. 调用 complete() 更新队列行。
    5. 若 processor 返回非空 ocr_text，同步更新 policy_documents 的
       ocr_status / ocr_confidence / ocr_text。

    返回：运行摘要 dict，或 None（无 pending 项）。
    """
    item = next_pending(db, worker_id=worker_id)
    if item is None:
        return None

    item_id = int(item["id"])
    document_id = int(item["document_id"])

    if processor is None:
        # 测试路径：仅认领，不执行真实 OCR。
        return {
            "item_id": item_id,
            "document_id": document_id,
            "queue_status": "running",
            "ocr_status": "running",
            "ocr_confidence": None,
            "pages_processed": 0,
            "pages_total": 0,
            "error": "",
            "retried": False,
        }

    try:
        proc_result = processor(document_id)
    except Exception as exc:  # noqa: BLE001 — 处理器异常兜底，避免整批中断。
        logger.exception("processor failed for document_id=%s", document_id)
        proc_result = {
            "ocr_status": "failed", "ocr_confidence": 0.0,
            "pages_processed": 0, "pages_total": 0,
            "error": f"processor_exception: {exc}", "ocr_text": "",
        }

    ocr_status = str(proc_result.get("ocr_status", "failed"))
    ocr_confidence = proc_result.get("ocr_confidence")
    pages_processed = int(proc_result.get("pages_processed", 0) or 0)
    pages_total = int(proc_result.get("pages_total", 0) or 0)
    error = str(proc_result.get("error", "") or "")
    ocr_text = str(proc_result.get("ocr_text", "") or "")

    complete_result = complete(
        db, item_id, ocr_status,
        ocr_confidence=ocr_confidence,
        pages_processed=pages_processed,
        pages_total=pages_total,
        error=error,
    )

    # processor 返回非空 ocr_text 时，同步落库到 policy_documents。
    if ocr_text:
        with db.connect() as conn:
            conn.execute(
                """UPDATE policy_documents
                   SET ocr_status=?, ocr_confidence=?, ocr_text=?
                   WHERE id=?""",
                (ocr_status, float(ocr_confidence or 0.0), ocr_text, document_id),
            )

    return {
        "item_id": item_id,
        "document_id": document_id,
        "queue_status": complete_result["final_status"],
        "ocr_status": ocr_status,
        "ocr_confidence": ocr_confidence,
        "pages_processed": pages_processed,
        "pages_total": pages_total,
        "error": error,
        "retried": complete_result["retried"],
    }


# ---------------------------------------------------------------------------
# 8. run_batch
# ---------------------------------------------------------------------------

def run_batch(
    db: Database,
    max_items: int = 10,
    worker_id: str = DEFAULT_BATCH_WORKER,
) -> dict[str, int]:
    """串行/并行处理最多 max_items 个 pending 队列项。

    并行度由 system_settings.ocr_max_workers 控制：等于 1 时串行执行，
    大于 1 时使用 concurrent.futures.ThreadPoolExecutor 并行认领与处理。
    每个 worker 使用默认 OCR 处理器（make_default_processor）。

    返回：{"processed": N, "ok": N, "failed": N, "retried": N}。
    """
    with db.connect() as conn:
        workers = _resolve_max_workers(conn)

    processed = 0
    ok = 0
    failed = 0
    retried = 0

    if workers <= 1:
        # 串行
        for _ in range(max_items):
            res = run_one(db, worker_id=worker_id, processor=make_default_processor(db))
            if res is None:
                break
            processed += 1
            if res.get("queue_status") == "ok":
                ok += 1
            elif res.get("queue_status") == "failed":
                failed += 1
            if res.get("retried"):
                retried += 1
    else:
        # 并行：一次性提交 max_items 个任务，run_one 在无 pending 时返回 None。
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [
                ex.submit(
                    run_one, db,
                    worker_id=f"{worker_id}-{i}",
                    processor=make_default_processor(db),
                )
                for i in range(max_items)
            ]
            for fut in futures:
                try:
                    res = fut.result()
                except Exception as exc:  # noqa: BLE001
                    logger.exception("batch worker raised: %s", exc)
                    continue
                if res is None:
                    continue
                processed += 1
                if res.get("queue_status") == "ok":
                    ok += 1
                elif res.get("queue_status") == "failed":
                    failed += 1
                if res.get("retried"):
                    retried += 1

    return {"processed": processed, "ok": ok, "failed": failed, "retried": retried}
