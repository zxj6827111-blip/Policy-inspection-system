from __future__ import annotations

import os
from typing import Callable

from fastapi import Depends, HTTPException, Request

from app.auth.models import CurrentUser
from app.auth.service import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE, AuthService
from app.runtime_config import is_auth_disabled


def get_auth_service(request: Request) -> AuthService:
    db = request.app.state.db
    return AuthService(db)


def get_optional_user(
    request: Request,
    auth: AuthService = Depends(get_auth_service),
) -> CurrentUser | None:
    if is_auth_disabled():
        return CurrentUser(
            id=0,
            username="auth-disabled",
            roles=("admin",),
            session_id="disabled",
        )
    session_id = request.cookies.get(SESSION_COOKIE)
    return auth.resolve_session(session_id)


def require_login(user: CurrentUser | None = Depends(get_optional_user)) -> CurrentUser:
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或会话已失效")
    return user


def require_roles(*roles: str) -> Callable:
    def dependency(user: CurrentUser = Depends(require_login)) -> CurrentUser:
        if user.has_role("admin"):
            return user
        if not user.has_role(*roles):
            raise HTTPException(status_code=403, detail="权限不足")
        return user

    return dependency


def require_capability(capability: str) -> Callable:
    def dependency(user: CurrentUser = Depends(require_login)) -> CurrentUser:
        if not user.has_capability(capability) and not user.has_role("admin"):
            raise HTTPException(status_code=403, detail="权限不足")
        return user

    return dependency


def require_write(capability: str = "jobs:write") -> Callable:
    """Authenticate + authorize + CSRF for cookie-session mutating requests."""

    def dependency(
        request: Request,
        user: CurrentUser = Depends(require_capability(capability)),
        auth: AuthService = Depends(get_auth_service),
    ) -> CurrentUser:
        if is_auth_disabled():
            return user
        header_token = request.headers.get(CSRF_HEADER) or request.headers.get(CSRF_HEADER.lower())
        cookie_token = request.cookies.get(CSRF_COOKIE)
        session_token = auth.session_csrf(request.cookies.get(SESSION_COOKIE))
        if not header_token or not cookie_token or not session_token:
            raise HTTPException(status_code=403, detail="缺少 CSRF 令牌")
        if header_token != cookie_token or header_token != session_token:
            raise HTTPException(status_code=403, detail="CSRF 校验失败")
        return user

    return dependency
