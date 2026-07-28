#!/usr/bin/env python3
"""M7-9 生产冒烟脚本。

在生产切换完成后立即执行，对核心端点进行端到端冒烟测试。
所有断言通过 => exit 0；任一失败 => exit 1 并打印失败列表。

用法:
    python scripts/smoke_test.py                 # 默认 http://localhost:8000
    python scripts/smoke_test.py --base-url http://prod-host:8000
    python scripts/smoke_test.py --admin-user admin --admin-pass 'PASS'

注意：脚本不会修改任何数据，仅执行 GET 探测和一个 POST /api/auth/login。
如未提供登录凭据，登录相关步骤将被跳过（仅做匿名健康检查）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _get(base_url: str, path: str, headers: dict[str, str] | None = None, timeout: int = 10) -> tuple[int, Any]:
    url = base_url.rstrip("/") + path
    req = Request(url, method="GET")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(body)
            except Exception:
                parsed = body
            return resp.status, parsed
    except HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
            parsed = json.loads(body)
        except Exception:
            parsed = body
        return e.code, parsed
    except URLError as e:
        return 0, str(e.reason)
    except Exception as e:
        return 0, str(e)


def _post_json(base_url: str, path: str, payload: dict, headers: dict[str, str] | None = None, timeout: int = 10) -> tuple[int, Any, dict[str, str]]:
    url = base_url.rstrip("/") + path
    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(body)
            except Exception:
                parsed = body
            return resp.status, parsed, dict(resp.headers)
    except HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
            parsed = json.loads(body)
        except Exception:
            parsed = body
        return e.code, parsed, dict(e.headers)
    except URLError as e:
        return 0, str(e.reason), {}
    except Exception as e:
        return 0, str(e), {}


def main() -> int:
    parser = argparse.ArgumentParser(description="M7 生产冒烟脚本")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--admin-user", default="")
    parser.add_argument("--admin-pass", default="")
    parser.add_argument("--timeout", type=int, default=10)
    args = parser.parse_args()

    results: list[dict[str, Any]] = []
    cookies: dict[str, str] = {}
    csrf_token: str = ""

    def record(name: str, ok: bool, detail: Any = None) -> None:
        results.append({"name": name, "ok": ok, "detail": detail})

    # 1. 基础健康
    code, body = _get(args.base_url, "/health", timeout=args.timeout)
    record("1_health_basic", code == 200 and isinstance(body, dict) and body.get("status") == "ok", {"code": code, "body": body})

    # 2. 深度健康
    code, body = _get(args.base_url, "/health/deep", timeout=args.timeout)
    deep_ok = code == 200 and isinstance(body, dict) and body.get("status") in {"ok", "healthy"}
    record("2_health_deep", deep_ok, {"code": code, "body": body})

    # 3. 生产环境 /docs 必须返回 404（fail-closed）
    code, _ = _get(args.base_url, "/docs", timeout=args.timeout)
    record("3_docs_404_in_prod", code == 404, {"code": code})

    # 4. /openapi.json 在生产必须返回 404
    code, _ = _get(args.base_url, "/openapi.json", timeout=args.timeout)
    record("4_openapi_404_in_prod", code == 404, {"code": code})

    # 5. /metrics 端点可达
    code, body = _get(args.base_url, "/metrics", timeout=args.timeout)
    record("5_metrics", code == 200, {"code": code})

    # 6. 未登录访问 /api/nav-items 应返回空列表（或 401）
    code, body = _get(args.base_url, "/api/nav-items", timeout=args.timeout)
    nav_ok = code in (200, 401)
    if code == 200:
        nav_ok = isinstance(body, list) and len(body) == 0
    record("6_nav_items_anonymous_empty", nav_ok, {"code": code, "body": body})

    # 7. 登录（如提供凭据）
    if args.admin_user and args.admin_pass:
        code, body, hdrs = _post_json(
            args.base_url,
            "/api/auth/login",
            {"username": args.admin_user, "password": args.admin_pass},
            timeout=args.timeout,
        )
        login_ok = code == 200 and isinstance(body, dict) and body.get("ok") is True
        record("7_admin_login", login_ok, {"code": code})

        if login_ok:
            # 提取 Set-Cookie
            for k, v in hdrs.items():
                if k.lower() == "set-cookie":
                    for cookie_pair in v.split(","):
                        for c in cookie_pair.split(";"):
                            c = c.strip()
                            if "=" in c and not c.lower().startswith(("path=", "domain=", "expires=", "max-age=", "samesite=", "secure", "httponly")):
                                name, _, value = c.partition("=")
                                cookies[name.strip()] = value.strip()
            csrf_token = body.get("csrf_token", "")
            cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
            headers = {"Cookie": cookie_header, CSRF_HEADER_NAME: csrf_token} if csrf_token else {"Cookie": cookie_header}

            # 8. /api/auth/me 应返回当前用户
            code, body = _get(args.base_url, "/api/auth/me", headers=headers, timeout=args.timeout)
            record("8_auth_me", code == 200 and isinstance(body, dict) and body.get("authenticated") is True, {"code": code, "body": body})

            # 9. /api/nav-items 应返回 21 项
            code, body = _get(args.base_url, "/api/nav-items", headers=headers, timeout=args.timeout)
            record("9_nav_items_admin", code == 200 and isinstance(body, list) and len(body) == 21, {"code": code, "count": len(body) if isinstance(body, list) else 0})

            # 10. /api/overview 全局总览
            code, body = _get(args.base_url, "/api/overview", headers=headers, timeout=args.timeout)
            record("10_overview", code == 200 and isinstance(body, dict), {"code": code})

            # 11. /api/ocr-queue
            code, body = _get(args.base_url, "/api/ocr-queue?limit=5", headers=headers, timeout=args.timeout)
            record("11_ocr_queue", code == 200, {"code": code})

            # 12. /api/system-settings
            code, body = _get(args.base_url, "/api/system-settings", headers=headers, timeout=args.timeout)
            record("12_system_settings", code == 200 and isinstance(body, list), {"code": code})

            # 13. /api/backups
            code, body = _get(args.base_url, "/api/backups", headers=headers, timeout=args.timeout)
            record("13_backups_list", code == 200 and isinstance(body, list), {"code": code})

            # 14. /api/audits
            code, body = _get(args.base_url, "/api/audits?limit=5", headers=headers, timeout=args.timeout)
            record("14_audits", code == 200 and isinstance(body, list), {"code": code})

            # 15. 登出
            code, body, _ = _post_json(
                args.base_url,
                "/api/auth/logout-simple",
                {},
                headers=headers,
                timeout=args.timeout,
            )
            record("15_logout", code == 200, {"code": code})
    else:
        record("7_admin_login", True, {"skipped": "no credentials provided"})

    # 汇总
    passed = sum(1 for r in results if r["ok"])
    failed = sum(1 for r in results if not r["ok"])
    print(f"\n=== M7 冒烟测试 ===")
    print(f"BASE_URL: {args.base_url}")
    print(f"PASS: {passed} / FAIL: {failed} / TOTAL: {len(results)}\n")
    for r in results:
        status = "PASS" if r["ok"] else "FAIL"
        print(f"  [{status}] {r['name']}")
        if not r["ok"]:
            print(f"         detail: {r['detail']}")
    print()

    overall = failed == 0
    print(f"总体结论: {'PASS' if overall else 'FAIL'}")
    return 0 if overall else 1


CSRF_HEADER_NAME = "X-CSRF-Token"


if __name__ == "__main__":
    sys.exit(main())
