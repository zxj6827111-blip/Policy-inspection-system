"""M5 整改闭环：任务 / 状态历史 / 复查 / 销号 / 超期 / 复发.

Implements V3.0 plan §11.4 + §11.5 guarantees:
- 人工确认 → 创建整改任务 → 指定责任单位 → 设置整改期限 → 发送整改通知
  → 上传反馈 → 自动或人工复查 → 通过 → 销号. 不通过则重新下发继续整改.
- 整改前后证据、状态历史、责任单位、责任人、整改期限、超期提醒、复发问题识别.

Rules:
- RECT-001: 整改任务未指定责任单位
- RECT-002: 整改任务超期
- RECT-003: 整改后证据缺失
- RECT-004: 整改未通过但未重新下发
- RECT-005: 销号后复发
- RECT-006: 整改状态历史缺失
- RECT-007: 整改期限缺失
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.db import Database, utc_now


# Status flow (V3.0 §11.4):
#   created → assigned → notified → feedback_uploaded → rechecking →
#   passed → signoff   (happy path)
#   rechecking → failed → reopened → assigned → ...  (rework loop)
STATUS_FLOW = {
    "created": {"assigned"},
    "assigned": {"notified", "failed", "reopened"},
    "notified": {"feedback_uploaded", "failed"},
    "feedback_uploaded": {"rechecking", "failed"},
    "rechecking": {"passed", "failed"},
    "passed": {"signoff"},
    "failed": {"reopened", "assigned"},
    "reopened": {"assigned"},
    "signoff": set(),  # terminal — but can be reopened on recurrence.
}

# Statuses considered "closed" (terminal).
TERMINAL_STATUSES = {"signoff"}

# Statuses that require before_evidence / after_evidence.
EVIDENCE_REQUIRED = {"feedback_uploaded", "rechecking", "passed", "signoff"}


@dataclass
class RectificationTaskInput:
    finding_id: int | None = None
    document_id: int = 0
    rule_code: str = ""
    title: str = ""
    description: str = ""
    responsible_unit: str = ""
    responsible_person: str = ""
    deadline: str | None = None
    priority: str = "normal"
    parent_task_id: int | None = None
    before_evidence_json: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RectificationTaskResult:
    task_id: int
    document_id: int
    rule_code: str
    rule_codes_emitted: list[str]
    notes: str = ""


def create_task(db: Database, payload: RectificationTaskInput, actor: str = "") -> RectificationTaskResult:
    """Create a rectification task. Emits RECT-001/RECT-007 if config invalid."""
    now = utc_now()
    rule_codes: list[str] = []

    if not payload.responsible_unit:
        rule_codes.append("RECT-001")
    if not payload.deadline:
        rule_codes.append("RECT-007")

    with db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO rectification_tasks(
                finding_id, document_id, rule_code, title, description,
                responsible_unit, responsible_person, deadline,
                status, priority, parent_task_id,
                before_evidence_json, after_evidence_json,
                machine_status, human_status, rule_version, allow_customer_output,
                created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                payload.finding_id, payload.document_id, payload.rule_code,
                payload.title, payload.description,
                payload.responsible_unit, payload.responsible_person, payload.deadline,
                "created", payload.priority, payload.parent_task_id,
                json.dumps(payload.before_evidence_json, ensure_ascii=False), "[]",
                "suspected" if rule_codes else "confirmed",
                "unreviewed", "1.0.0",
                1 if rule_codes else 0,
                now, now,
            ),
        )
        task_id = int(cur.lastrowid)

        # Initial status history entry.
        conn.execute(
            """INSERT INTO rectification_status_history(
                task_id, from_status, to_status, actor, reason, evidence_json, created_at
            ) VALUES(?,?,?,?,?,?,?)""",
            (task_id, "", "created", actor or "system", "Task created", "[]", now),
        )

        # Update document-level rectification_status.
        conn.execute(
            "UPDATE policy_documents SET rectification_status='pending' WHERE id=?",
            (payload.document_id,),
        )

    return RectificationTaskResult(
        task_id=task_id,
        document_id=payload.document_id,
        rule_code=payload.rule_code,
        rule_codes_emitted=rule_codes,
        notes="Task created" + (" with config issues" if rule_codes else ""),
    )


def transition_status(
    db: Database, task_id: int, to_status: str, *,
    actor: str = "", reason: str = "",
    feedback_notes: str = "", after_evidence: list[dict[str, Any]] | None = None,
    deadline: str | None = None,
) -> dict[str, Any]:
    """Transition a task to a new status, recording history.

    V3.0 §11.4: 不通过则重新下发继续整改.
    V3.0 §11.5: 状态可追溯, 整改前后证据完整.
    """
    with db.connect() as conn:
        task = conn.execute(
            "SELECT * FROM rectification_tasks WHERE id=?", (task_id,)
        ).fetchone()
        if not task:
            raise ValueError(f"Rectification task {task_id} not found")
        task = dict(task)
        from_status = task["status"]

        # Validate transition.
        if to_status not in STATUS_FLOW.get(from_status, set()):
            # Allow signoff → reopened for recurrence (special case).
            if not (from_status == "signoff" and to_status == "reopened"):
                raise ValueError(
                    f"Invalid status transition: {from_status} → {to_status}"
                )

        now = utc_now()
        rule_codes_emitted: list[str] = []

        # Build update fields.
        updates: dict[str, Any] = {
            "status": to_status,
            "updated_at": now,
        }
        if feedback_notes:
            updates["feedback_notes"] = feedback_notes
        if after_evidence is not None:
            updates["after_evidence_json"] = json.dumps(after_evidence, ensure_ascii=False)
        if deadline:
            updates["deadline"] = deadline

        # RECT-003: 整改后证据缺失 (if transitioning to rechecking/passed without evidence).
        if to_status in {"rechecking", "passed", "signoff"}:
            existing_after = json.loads(task["after_evidence_json"] or "[]")
            new_after = after_evidence if after_evidence is not None else existing_after
            if not new_after:
                rule_codes_emitted.append("RECT-003")

        # RECT-004: 整改未通过但未重新下发 — emit when entering 'failed' status;
        # caller must explicitly transition to 'reopened' to clear.
        if to_status == "failed":
            rule_codes_emitted.append("RECT-004")

        # RECT-005: 销号后复发 — emit when transitioning from 'signoff' to 'reopened'.
        if from_status == "signoff" and to_status == "reopened":
            rule_codes_emitted.append("RECT-005")
            # Increment recurrence_count.
            updates["recurrence_count"] = int(task["recurrence_count"]) + 1

        # On signoff, set closed_at.
        if to_status == "signoff":
            updates["closed_at"] = now

        # On reopened, clear closed_at.
        if to_status == "reopened" and task.get("closed_at"):
            updates["closed_at"] = None

        # Build SET clause.
        set_clause = ", ".join(f"{k}=?" for k in updates)
        conn.execute(
            f"UPDATE rectification_tasks SET {set_clause} WHERE id=?",
            (*updates.values(), task_id),
        )

        # Record history entry.
        conn.execute(
            """INSERT INTO rectification_status_history(
                task_id, from_status, to_status, actor, reason, evidence_json, created_at
            ) VALUES(?,?,?,?,?,?,?)""",
            (
                task_id, from_status, to_status, actor or "system", reason,
                json.dumps({
                    "rule_codes_emitted": rule_codes_emitted,
                    "feedback_notes": feedback_notes,
                }, ensure_ascii=False),
                now,
            ),
        )

        # RECT-006: 整改状态历史缺失 — verify history exists.
        history_count = conn.execute(
            "SELECT COUNT(*) FROM rectification_status_history WHERE task_id=?",
            (task_id,),
        ).fetchone()[0]
        if history_count < 2:
            rule_codes_emitted.append("RECT-006")

        # Update document-level rectification_status.
        doc_status_map = {
            "created": "pending", "assigned": "in_progress", "notified": "in_progress",
            "feedback_uploaded": "in_progress", "rechecking": "in_progress",
            "passed": "passed", "failed": "failed", "reopened": "reopened",
            "signoff": "passed",
        }
        doc_status = doc_status_map.get(to_status, "pending")
        conn.execute(
            "UPDATE policy_documents SET rectification_status=? WHERE id=?",
            (doc_status, task["document_id"]),
        )

    return {
        "task_id": task_id,
        "from_status": from_status,
        "to_status": to_status,
        "rule_codes_emitted": rule_codes_emitted,
    }


def detect_overdue_tasks(db: Database, *, now: str | None = None) -> list[dict[str, Any]]:
    """Find all non-terminal tasks past their deadline.

    V3.0 §11.5: 整改任务超期 (RECT-002).
    """
    now = now or utc_now()
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT id, document_id, rule_code, title, responsible_unit, deadline,
                      status, recurrence_count
            FROM rectification_tasks
            WHERE status NOT IN ('signoff', 'failed')
              AND deadline IS NOT NULL
              AND deadline != ''
              AND deadline < ?""",
            (now,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_task(db: Database, task_id: int) -> dict[str, Any] | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM rectification_tasks WHERE id=?", (task_id,)
        ).fetchone()
        return dict(row) if row else None


def get_status_history(db: Database, task_id: int) -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT * FROM rectification_status_history
            WHERE task_id=? ORDER BY created_at ASC, id ASC""",
            (task_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_tasks(
    db: Database, *,
    document_id: int | None = None, status: str | None = None,
    overdue_only: bool = False, limit: int = 100,
) -> list[dict[str, Any]]:
    with db.connect() as conn:
        query = "SELECT * FROM rectification_tasks WHERE 1=1"
        params: list[Any] = []
        if document_id is not None:
            query += " AND document_id=?"
            params.append(document_id)
        if status is not None:
            query += " AND status=?"
            params.append(status)
        if overdue_only:
            now = utc_now()
            query += " AND status NOT IN ('signoff','failed') AND deadline IS NOT NULL AND deadline!='' AND deadline<?"
            params.append(now)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def detect_recurrence(db: Database, *, document_id: int, rule_code: str) -> list[dict[str, Any]]:
    """Find signoff tasks for this document+rule that have been reopened (RECT-005).

    V3.0 §11.5: 销号后复发可识别.
    """
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT * FROM rectification_tasks
            WHERE document_id=? AND rule_code=? AND recurrence_count > 0
            ORDER BY id DESC""",
            (document_id, rule_code),
        ).fetchall()
    return [dict(r) for r in rows]
