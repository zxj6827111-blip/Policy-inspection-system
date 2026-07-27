from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from app.auth.models import ALL_ROLES, CurrentUser, normalize_roles
from app.auth.passwords import hash_password, verify_password
from app.db import Database, utc_now


SESSION_COOKIE = "pis_session"
CSRF_COOKIE = "pis_csrf"
CSRF_HEADER = "X-CSRF-Token"
SESSION_DAYS = 7


class AuthService:
    def __init__(self, db: Database):
        self.db = db

    def ensure_role_rows(self) -> None:
        now = utc_now()
        with self.db.connect() as conn:
            for role in ALL_ROLES:
                conn.execute(
                    "INSERT OR IGNORE INTO roles(name, description, created_at) VALUES(?,?,?)",
                    (role, role, now),
                )

    def create_user(
        self,
        username: str,
        password: str,
        roles: list[str] | tuple[str, ...],
        *,
        display_name: str = "",
        created_by: str = "system",
    ) -> int:
        username = (username or "").strip()
        if not username:
            raise ValueError("用户名不能为空")
        role_names = normalize_roles(roles)
        password_hash = hash_password(password)
        now = utc_now()
        self.ensure_role_rows()
        with self.db.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM users WHERE username=?", (username,)
            ).fetchone()
            if existing:
                raise ValueError(f"用户已存在: {username}")
            cur = conn.execute(
                """INSERT INTO users(username, password_hash, display_name, is_active, created_at, updated_at)
                VALUES(?,?,?,?,?,?)""",
                (username, password_hash, display_name or username, 1, now, now),
            )
            user_id = int(cur.lastrowid)
            for role in role_names:
                role_row = conn.execute("SELECT id FROM roles WHERE name=?", (role,)).fetchone()
                conn.execute(
                    "INSERT OR IGNORE INTO user_roles(user_id, role_id, assigned_at, assigned_by) VALUES(?,?,?,?)",
                    (user_id, int(role_row["id"]), now, created_by),
                )
            self._audit(
                conn,
                actor_user_id=user_id,
                actor_username=username,
                action="user.create",
                resource_type="user",
                resource_id=str(user_id),
                detail={"roles": list(role_names)},
            )
            return user_id

    def list_users(self) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT u.id, u.username, u.display_name, u.is_active, u.created_at, u.last_login_at,
                GROUP_CONCAT(r.name) AS roles
                FROM users u
                LEFT JOIN user_roles ur ON ur.user_id=u.id
                LEFT JOIN roles r ON r.id=ur.role_id
                GROUP BY u.id
                ORDER BY u.id"""
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["roles"] = [part for part in (item.pop("roles") or "").split(",") if part]
                result.append(item)
            return result

    def authenticate(self, username: str, password: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE username=? AND is_active=1", (username,)
            ).fetchone()
            if not row:
                return None
            if not verify_password(password, row["password_hash"]):
                return None
            return dict(row)

    def create_session(
        self,
        user_id: int,
        *,
        ip: str = "",
        user_agent: str = "",
    ) -> tuple[str, str, datetime]:
        session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        expires = now + timedelta(days=SESSION_DAYS)
        with self.db.connect() as conn:
            conn.execute(
                """INSERT INTO sessions(id, user_id, csrf_token, created_at, expires_at, last_seen_at, ip, user_agent, revoked)
                VALUES(?,?,?,?,?,?,?,?,0)""",
                (
                    session_id,
                    user_id,
                    csrf_token,
                    now.isoformat(),
                    expires.isoformat(),
                    now.isoformat(),
                    ip,
                    user_agent[:300],
                ),
            )
            conn.execute(
                "UPDATE users SET last_login_at=?, updated_at=? WHERE id=?",
                (now.isoformat(), now.isoformat(), user_id),
            )
        return session_id, csrf_token, expires

    def revoke_session(self, session_id: str | None) -> None:
        if not session_id:
            return
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE sessions SET revoked=1, last_seen_at=? WHERE id=?",
                (utc_now(), session_id),
            )

    def resolve_session(self, session_id: str | None) -> CurrentUser | None:
        if not session_id:
            return None
        with self.db.connect() as conn:
            row = conn.execute(
                """SELECT s.id AS session_id, s.csrf_token, s.expires_at, s.revoked,
                u.id AS user_id, u.username, u.is_active
                FROM sessions s JOIN users u ON u.id=s.user_id
                WHERE s.id=?""",
                (session_id,),
            ).fetchone()
            if not row or int(row["revoked"]) or not int(row["is_active"]):
                return None
            expires = datetime.fromisoformat(row["expires_at"])
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires < datetime.now(timezone.utc):
                return None
            roles = [
                r["name"]
                for r in conn.execute(
                    """SELECT r.name FROM user_roles ur JOIN roles r ON r.id=ur.role_id
                    WHERE ur.user_id=? ORDER BY r.name""",
                    (row["user_id"],),
                ).fetchall()
            ]
            conn.execute(
                "UPDATE sessions SET last_seen_at=? WHERE id=?",
                (utc_now(), session_id),
            )
            return CurrentUser(
                id=int(row["user_id"]),
                username=row["username"],
                roles=tuple(roles),
                session_id=row["session_id"],
            )

    def session_csrf(self, session_id: str | None) -> str | None:
        if not session_id:
            return None
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT csrf_token, revoked, expires_at FROM sessions WHERE id=?",
                (session_id,),
            ).fetchone()
            if not row or int(row["revoked"]):
                return None
            return row["csrf_token"]

    def audit(
        self,
        *,
        actor: CurrentUser | None,
        action: str,
        resource_type: str = "",
        resource_id: str = "",
        before: dict | None = None,
        after: dict | None = None,
        detail: dict | None = None,
        ip: str = "",
        user_agent: str = "",
    ) -> None:
        with self.db.connect() as conn:
            self._audit(
                conn,
                actor_user_id=actor.id if actor else None,
                actor_username=actor.username if actor else "",
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                before=before,
                after=after,
                detail=detail,
                ip=ip,
                user_agent=user_agent,
            )

    @staticmethod
    def _audit(
        conn,
        *,
        actor_user_id: int | None,
        actor_username: str,
        action: str,
        resource_type: str = "",
        resource_id: str = "",
        before: dict | None = None,
        after: dict | None = None,
        detail: dict | None = None,
        ip: str = "",
        user_agent: str = "",
    ) -> None:
        conn.execute(
            """INSERT INTO operation_audits(
                actor_user_id, actor_username, action, resource_type, resource_id,
                before_json, after_json, detail_json, ip, user_agent, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                actor_user_id,
                actor_username,
                action,
                resource_type,
                resource_id,
                json.dumps(before or {}, ensure_ascii=False),
                json.dumps(after or {}, ensure_ascii=False),
                json.dumps(detail or {}, ensure_ascii=False),
                ip,
                user_agent[:300],
                utc_now(),
            ),
        )
