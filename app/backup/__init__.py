"""备份/恢复/校验/清理/回滚库（M6-7）。

依据 V3.0 §十二（M6）：
- 使用 SQLite 在线 backup() API 拷贝数据库，避免 WAL 不一致；
- 备份前对源库执行 PRAGMA wal_checkpoint(TRUNCATE)；
- 备份后计算 SHA256 sidecar，并在源库与备份库各记录一条 db_health_snapshots；
- 提供恢复、校验、列举、清理、回滚能力。

本模块独立，不依赖 app.observability（可能由其他 agent 创建）。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.db import Database, DB_PATH, utc_now  # noqa: F401  (DB_PATH/utc_now 供外部复用)

__all__ = [
    "backup_database",
    "restore_database",
    "validate_backup",
    "list_backups",
    "prune_old_backups",
    "rollback_to_pre_migration",
]

# 校验时统计行数的关键表
_KEY_TABLES = ("rule_findings", "policy_documents", "scan_jobs", "operation_audits")


# ---------------------------------------------------------------------------
# 内部辅助函数
# ---------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    """计算文件 SHA256 哈希（分块读取，支持大文件）。"""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """统计所有用户表的行数，跳过 sqlite 内部表。"""
    tables = [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    counts: dict[str, int] = {}
    for t in tables:
        try:
            counts[t] = conn.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
        except sqlite3.DatabaseError:
            counts[t] = -1
    return counts


def _index_list(conn: sqlite3.Connection) -> list[dict[str, str]]:
    """获取所有用户索引的元数据。"""
    rows = conn.execute(
        "SELECT name, tbl_name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [{"name": r[0], "table": r[1]} for r in rows]


def _wal_checkpoint_enabled(conn: sqlite3.Connection) -> bool:
    """从 system_settings 读取 backup_wal_checkpoint_on_success（默认 true）。表不存在或未设置时默认 true。"""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='system_settings'"
    ).fetchone()
    if not exists:
        return True
    row = conn.execute(
        "SELECT value FROM system_settings WHERE key='backup_wal_checkpoint_on_success'"
    ).fetchone()
    if row is None:
        return True
    return str(row[0]).strip().lower() in {"1", "true", "yes", "on"}


def _record_health_snapshot(conn: sqlite3.Connection, notes: str) -> None:
    """在 db_health_snapshots 表记录一条健康快照。表不存在则静默跳过。"""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='db_health_snapshots'"
    ).fetchone()
    if not exists:
        return
    now = utc_now()
    # 通过 PRAGMA database_list 获取主库文件路径
    db_row = conn.execute("PRAGMA database_list").fetchone()
    db_path_str = db_row[2] if db_row else ""
    db_path = Path(db_path_str) if db_path_str else None
    db_size = db_path.stat().st_size if db_path and db_path.exists() else 0
    wal_size = 0
    if db_path:
        wal_path = db_path.with_name(db_path.name + "-wal")
        if wal_path.exists():
            wal_size = wal_path.stat().st_size
    page_count = conn.execute("PRAGMA page_count").fetchone()[0]
    freelist_count = conn.execute("PRAGMA freelist_count").fetchone()[0]
    journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    counts = _table_counts(conn)
    indexes = _index_list(conn)
    conn.execute(
        """INSERT INTO db_health_snapshots
           (snapshot_at, db_size_bytes, wal_size_bytes, page_count, freelist_count,
            journal_mode, table_counts_json, index_list_json, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            now, db_size, wal_size, page_count, freelist_count,
            str(journal_mode),
            json.dumps(counts, ensure_ascii=False),
            json.dumps(indexes, ensure_ascii=False),
            notes,
        ),
    )


def _integrity_check(path: Path) -> str:
    """运行 PRAGMA integrity_check，返回结果字符串（'ok' 表示通过）。"""
    conn = sqlite3.connect(str(path))
    try:
        return str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        conn.close()


def _parse_label(filename: str) -> str:
    """从文件名 policy_inspector_{label}_{YYYYMMDD_HHMMSS}.db 解析 label。"""
    name = filename
    if name.startswith("policy_inspector_"):
        name = name[len("policy_inspector_"):]
    if name.endswith(".db"):
        name = name[:-3]
    m = re.match(r"^(.*)_(\d{8}_\d{6})$", name)
    return m.group(1) if m else name


def _cleanup_wal_sidecars(db_path: Path) -> None:
    """删除数据库主文件旁的 -wal/-shm 文件，确保备份自包含。"""
    for suffix in ("-wal", "-shm"):
        side = db_path.with_name(db_path.name + suffix)
        if side.exists():
            try:
                side.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 公共 API
# ---------------------------------------------------------------------------

def backup_database(db: Database, dest_dir: Path | str, label: str = "manual") -> Path:
    """在线备份源数据库到 dest_dir，返回备份文件路径。

    使用 SQLite backup() API 在线拷贝，避免 WAL 不一致；
    备份前对源库执行 PRAGMA wal_checkpoint(TRUNCATE)（受 system_settings
    backup_wal_checkpoint_on_success 控制，默认 true）；
    备份后在源库与备份库各记录一条 db_health_snapshots（notes=f"backup:{label}"）；
    计算备份文件 SHA256 并写入 {backup_file}.sha256 sidecar。

    Args:
        db: 已初始化的 Database 实例。
        dest_dir: 备份输出目录（不存在则创建）。
        label: 备份标签，用于文件名与快照备注。

    Returns:
        备份文件 Path。

    Raises:
        RuntimeError: 备份过程中发生错误，附带清晰消息。
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = dest_dir / f"policy_inspector_{label}_{timestamp}.db"

    try:
        with db.connect() as src_conn:
            # 备份前对源库执行 WAL checkpoint，将 WAL 数据合并进主库
            if _wal_checkpoint_enabled(src_conn):
                src_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            # 使用 SQLite 在线 backup API 拷贝（避免 WAL 不一致）
            dest_conn = sqlite3.connect(str(backup_path))
            try:
                src_conn.backup(dest_conn)
                # 在备份库记录健康快照
                _record_health_snapshot(dest_conn, notes=f"backup:{label}")
                dest_conn.commit()
                # 确保备份文件自包含：checkpoint 后清理 -wal/-shm
                dest_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                dest_conn.close()
            # 在源库记录健康快照
            _record_health_snapshot(src_conn, notes=f"backup:{label}")
        # 清理备份文件可能的 -wal/-shm，确保主文件自包含
        _cleanup_wal_sidecars(backup_path)
        # 计算备份文件 SHA256 并写入 sidecar
        digest = _sha256(backup_path)
        sidecar = backup_path.with_name(backup_path.name + ".sha256")
        sidecar.write_text(f"{digest}  {backup_path.name}\n", encoding="utf-8")
        return backup_path
    except Exception as exc:
        # 清理半成品备份文件与 sidecar
        if backup_path.exists():
            try:
                backup_path.unlink()
            except OSError:
                pass
        sidecar = backup_path.with_name(backup_path.name + ".sha256")
        if sidecar.exists():
            try:
                sidecar.unlink()
            except OSError:
                pass
        _cleanup_wal_sidecars(backup_path)
        raise RuntimeError(f"备份失败: {exc}") from exc


def restore_database(
    backup_path: Path | str,
    dest_db_path: Path | str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """从备份恢复到 dest_db_path。

    先校验备份完整性（PRAGMA integrity_check）与 SHA256（如有 sidecar）；
    dry_run=True 时仅返回校验结果不覆盖目标；否则覆盖目标并清理 stale -wal/-shm。

    Args:
        backup_path: 备份文件路径。
        dest_db_path: 恢复目标路径。
        dry_run: 为 True 时只校验不覆盖。

    Returns:
        {"restored": bool, "dest": str, "integrity": str, "sha256": str,
         "sha256_match": bool, "dry_run": bool}
    """
    backup_path = Path(backup_path)
    dest_db_path = Path(dest_db_path)
    if not backup_path.exists():
        raise FileNotFoundError(f"备份文件不存在: {backup_path}")

    # 校验完整性
    integrity = _integrity_check(backup_path)
    # 计算并校验 SHA256
    sha = _sha256(backup_path)
    sidecar = backup_path.with_name(backup_path.name + ".sha256")
    sha_match = True
    expected = ""
    if sidecar.exists():
        sidecar_text = sidecar.read_text(encoding="utf-8").strip()
        expected = sidecar_text.split()[0] if sidecar_text else ""
        sha_match = (expected == sha)

    if dry_run:
        return {
            "restored": False,
            "dest": str(dest_db_path),
            "integrity": integrity,
            "sha256": sha,
            "sha256_match": sha_match,
            "dry_run": True,
        }

    if integrity != "ok":
        raise RuntimeError(f"备份完整性检查失败，拒绝恢复: {integrity}")
    if not sha_match:
        raise RuntimeError(f"SHA256 校验不匹配，拒绝恢复: 备份={sha} sidecar={expected}")

    # 覆盖目标数据库
    dest_db_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup_path, dest_db_path)
    # 清理 stale -wal/-shm 文件
    _cleanup_wal_sidecars(dest_db_path)

    return {
        "restored": True,
        "dest": str(dest_db_path),
        "integrity": integrity,
        "sha256": sha,
        "sha256_match": sha_match,
        "dry_run": False,
    }


def validate_backup(backup_path: Path | str) -> dict[str, Any]:
    """校验备份文件：完整性、快速检查、外键检查、关键表行数。

    以只读方式打开备份，运行 PRAGMA integrity_check / quick_check /
    foreign_key_check，并统计关键表（rule_findings、policy_documents、
    scan_jobs、operation_audits）行数。

    Args:
        backup_path: 备份文件路径。

    Returns:
        {"valid": bool, "integrity": str, "quick_check": str,
         "fk_check": list, "table_counts": dict}
    """
    backup_path = Path(backup_path)
    if not backup_path.exists():
        raise FileNotFoundError(f"备份文件不存在: {backup_path}")

    conn = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
    try:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        quick = str(conn.execute("PRAGMA quick_check").fetchone()[0])
        fk_rows = [r[0] for r in conn.execute("PRAGMA foreign_key_check").fetchall()]
        table_counts: dict[str, Any] = {}
        for t in _KEY_TABLES:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
            ).fetchone()
            if exists:
                table_counts[t] = conn.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
            else:
                table_counts[t] = None
    finally:
        conn.close()

    valid = (integrity == "ok") and (quick == "ok") and (len(fk_rows) == 0)
    return {
        "valid": valid,
        "integrity": integrity,
        "quick_check": quick,
        "fk_check": fk_rows,
        "table_counts": table_counts,
    }


def list_backups(backup_dir: Path | str) -> list[dict[str, Any]]:
    """列出备份目录中所有 .db 备份文件的元数据，按修改时间倒序。

    Args:
        backup_dir: 备份目录。

    Returns:
        每个元素包含 name, path, size_bytes, mtime(ISO), sha256, label。
    """
    backup_dir = Path(backup_dir)
    if not backup_dir.exists():
        return []
    results: list[dict[str, Any]] = []
    for p in sorted(backup_dir.glob("*.db"), key=lambda x: x.stat().st_mtime, reverse=True):
        sidecar = p.with_name(p.name + ".sha256")
        sha = ""
        if sidecar.exists():
            text = sidecar.read_text(encoding="utf-8").strip()
            sha = text.split()[0] if text else ""
        st = p.stat()
        results.append({
            "name": p.name,
            "path": str(p),
            "size_bytes": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            "sha256": sha,
            "label": _parse_label(p.name),
        })
    return results


def prune_old_backups(backup_dir: Path | str, retention_days: int = 30) -> list[str]:
    """删除超过保留期的备份及其 .sha256 sidecar。

    Args:
        backup_dir: 备份目录。
        retention_days: 保留天数（默认 30）。

    Returns:
        已删除文件路径列表（含 .db 与 .sha256）。
    """
    backup_dir = Path(backup_dir)
    if not backup_dir.exists():
        return []
    cutoff = datetime.now(timezone.utc).timestamp() - retention_days * 86400
    deleted: list[str] = []
    for p in backup_dir.glob("*.db"):
        if p.stat().st_mtime < cutoff:
            sidecar = p.with_name(p.name + ".sha256")
            try:
                p.unlink()
                deleted.append(str(p))
            except OSError:
                pass
            if sidecar.exists():
                try:
                    sidecar.unlink()
                    deleted.append(str(sidecar))
                except OSError:
                    pass
    return deleted


def rollback_to_pre_migration(
    db: Database,
    backup_path: Path | str,
    migration_name: str,
) -> dict[str, Any]:
    """回滚到迁移前的备份。仅应在非生产副本上执行。

    流程：校验备份 → 恢复为当前数据库路径 → 在 db_health_snapshots
    记录 notes=f"rollback:{migration_name}"。

    Args:
        db: Database 实例（其 path 将被覆盖）。
        backup_path: 迁移前备份路径。
        migration_name: 迁移名称（用于快照备注）。

    Returns:
        {"rolled_back": bool, "from": str, "to": str}
    """
    warnings.warn(
        "rollback_to_pre_migration 仅应在非生产副本上执行；"
        "在生产环境回滚可能导致数据丢失，请先确认已停止服务并备份当前状态。",
        stacklevel=2,
    )
    backup_path = Path(backup_path)
    # 校验备份
    validation = validate_backup(backup_path)
    if not validation["valid"]:
        raise RuntimeError(
            f"备份校验失败，拒绝回滚: integrity={validation['integrity']}, "
            f"fk_check={validation['fk_check']}"
        )
    from_path = str(db.path)
    # 恢复为当前数据库
    restore_database(backup_path, db.path, dry_run=False)
    # 记录回滚健康快照
    with db.connect() as conn:
        _record_health_snapshot(conn, notes=f"rollback:{migration_name}")
    return {
        "rolled_back": True,
        "from": from_path,
        "to": str(backup_path),
    }
