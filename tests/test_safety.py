import random
import asyncio
import time

import pytest

from app.config import SafetyConfig
from app.collector import BrowserCollector
from app.domain import SafetyPause
from app.safety import SafetyController


class FakeSleep:
    def __init__(self):
        self.calls = []

    async def __call__(self, seconds):
        self.calls.append(seconds)


def test_safety_config_rejects_fast_interval():
    with pytest.raises(ValueError, match="不得低于"):
        SafetyConfig(min_delay_seconds=4).validate()


@pytest.mark.asyncio
async def test_403_and_429_pause_immediately():
    controller = SafetyController(SafetyConfig(), sleep=FakeSleep(), rng=random.Random(1))
    await controller.before_request()
    with pytest.raises(SafetyPause):
        controller.after_request(429, "")
    assert not controller._lock.locked()


@pytest.mark.asyncio
async def test_captcha_text_pauses():
    controller = SafetyController(SafetyConfig(), sleep=FakeSleep(), rng=random.Random(1))
    await controller.before_request()
    with pytest.raises(SafetyPause, match="验证码"):
        controller.after_request(200, "请输入验证码后继续")


@pytest.mark.asyncio
async def test_three_consecutive_failures_pause():
    controller = SafetyController(SafetyConfig(), sleep=FakeSleep(), rng=random.Random(1))
    for _ in range(2):
        await controller.before_request()
        controller.after_request(None, error=OSError("offline"))
    await controller.before_request()
    with pytest.raises(SafetyPause, match="连续网络失败"):
        controller.after_request(None, error=OSError("offline"))


class FakeRequest:
    resource_type = "xhr"


class FakeResponse:
    request = FakeRequest()
    status = 429


class FakeBody:
    async def inner_text(self):
        return "访问频繁"


class FakeClickPage:
    def __init__(self):
        self.listeners = []

    def on(self, event, callback):
        assert event == "response"
        self.listeners.append(callback)

    def remove_listener(self, event, callback):
        self.listeners.remove(callback)

    async def wait_for_timeout(self, _milliseconds):
        return None

    def locator(self, selector):
        assert selector == "body"
        return FakeBody()

    async def click(self):
        for listener in self.listeners:
            listener(FakeResponse())


@pytest.mark.asyncio
async def test_real_click_is_limited_before_429_is_observed():
    sleep = FakeSleep()
    controller = SafetyController(SafetyConfig(), sleep=sleep, rng=random.Random(1))
    await controller.before_request()
    controller.after_request(200, "正常页面")
    page = FakeClickPage()
    collector = BrowserCollector(None, controller)

    with pytest.raises(SafetyPause):
        await collector._click_and_observe(page, page.click, 0)

    assert sleep.calls and sleep.calls[0] >= 5
    assert not controller._lock.locked()


@pytest.mark.asyncio
async def test_global_lock_keeps_concurrency_at_one():
    controller = SafetyController(SafetyConfig(), sleep=FakeSleep(), rng=random.Random(1))
    await controller.before_request()
    second = asyncio.create_task(controller.before_request())
    await asyncio.sleep(0)
    assert second.done() is False
    controller.after_request(200, "正常")
    await second
    assert controller._lock.locked()
    controller.after_request(200, "正常")


@pytest.mark.asyncio
async def test_minimum_delay_and_fiftieth_access_rest_are_enforced():
    sleep = FakeSleep()
    events = []
    controller = SafetyController(
        SafetyConfig(), sleep=sleep, rng=random.Random(1), initial_pages=49,
        event_hook=lambda kind, details: events.append((kind, details)),
    )
    await controller.before_request()
    controller.after_request(200, "正常")
    await controller.before_request()
    assert any(5 <= seconds <= 10 for seconds in sleep.calls)
    assert any(180 <= seconds <= 300 for seconds in sleep.calls)
    assert any(kind == "rest" and details["access_count"] == 50 for kind, details in events)
    controller.after_request(200, "正常")


@pytest.mark.asyncio
async def test_four_hour_run_limit_pauses_before_another_request():
    controller = SafetyController(SafetyConfig(), sleep=FakeSleep(), rng=random.Random(1))
    controller.started_at = time.monotonic() - controller.config.max_run_seconds
    with pytest.raises(SafetyPause, match="运行上限"):
        await controller.before_request()
    assert not controller._lock.locked()


@pytest.mark.asyncio
async def test_retry_wait_ranges_are_bounded():
    sleep = FakeSleep()
    controller = SafetyController(SafetyConfig(), sleep=sleep, rng=random.Random(1))
    await controller.retry_wait(1)
    await controller.retry_wait(2)
    assert 30 <= sleep.calls[0] <= 60
    assert 120 <= sleep.calls[1] <= 300
