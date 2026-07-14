from __future__ import annotations

import asyncio
import random
import time
from dataclasses import asdict
from typing import Any, Callable

from app.config import SafetyConfig
from app.domain import CooldownPause, SafetyPause


RISK_TEXT = ("访问频繁", "请求过于频繁", "安全验证", "请输入验证码", "人机验证", "访问受限")


class SafetyController:
    """全局串行节奏控制器；所有页面和外链必须共享同一实例。"""

    def __init__(
        self, config: SafetyConfig, sleep=asyncio.sleep, rng: random.Random | None = None,
        event_hook: Callable[[str, dict[str, Any]], None] | None = None, initial_pages: int = 0,
    ):
        config.validate()
        self.config = config
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._lock = asyncio.Lock()
        self._last_finished = 0.0
        self._pages = initial_pages
        self._failures = 0
        self._event_hook = event_hook
        self.started_at = time.monotonic()

    def as_dict(self) -> dict:
        return asdict(self.config)

    def _emit(self, event_type: str, **details: Any) -> None:
        if self._event_hook:
            self._event_hook(event_type, details)

    async def before_request(self) -> None:
        await self._lock.acquire()
        elapsed = time.monotonic() - self.started_at
        if elapsed >= self.config.max_run_seconds:
            self._lock.release()
            raise CooldownPause("已达到单次连续运行上限，进入安全冷却后将自动恢复")
        if self._last_finished:
            delay = self._rng.uniform(self.config.min_delay_seconds, self.config.max_delay_seconds)
            remaining = delay - (time.monotonic() - self._last_finished)
            if remaining > 0:
                await self._sleep(remaining)
        if self._pages and self._pages % self.config.rest_every_pages == 0:
            seconds = self._rng.uniform(self.config.rest_min_seconds, self.config.rest_max_seconds)
            self._emit("rest", seconds=seconds, access_count=self._pages)
            await self._sleep(seconds)

    def after_request(
        self,
        status_code: int | None,
        visible_text: str = "",
        error: Exception | None = None,
        *,
        enforce_risk: bool = True,
        count_failure: bool = True,
    ) -> None:
        try:
            if enforce_risk and status_code in {403, 429}:
                raise CooldownPause(f"目标站返回风控状态码 {status_code}")
            if enforce_risk and any(marker in visible_text for marker in RISK_TEXT):
                raise CooldownPause("页面出现验证码或访问频率提示")
            if error and count_failure:
                self._failures += 1
                if self._failures >= self.config.consecutive_failure_limit:
                    raise CooldownPause("连续网络失败达到安全阈值")
            elif not error:
                self._failures = 0
                self._pages += 1
            self._emit(
                "access", status_code=status_code, error=type(error).__name__ if error else "",
                access_count=self._pages,
            )
        finally:
            self._last_finished = time.monotonic()
            if self._lock.locked():
                self._lock.release()

    async def retry_wait(self, attempt: int) -> None:
        if attempt == 1:
            seconds = self._rng.uniform(30, 60)
        elif attempt == 2:
            seconds = self._rng.uniform(120, 300)
        else:
            return
        self._emit("retry", attempt=attempt, seconds=seconds)
        await self._sleep(seconds)
