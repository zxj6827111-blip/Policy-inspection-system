import random

import httpx
import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeout

from app.collector import LinkChecker
from app.config import SafetyConfig
from app.domain import RelatedLink, SafetyPause
from app.safety import SafetyController


class FakeSleep:
    def __init__(self):
        self.calls = []

    async def __call__(self, seconds):
        self.calls.append(seconds)


def checker_with(handler):
    safety = SafetyController(
        SafetyConfig(consecutive_failure_limit=10), sleep=FakeSleep(), rng=random.Random(1)
    )
    checker = LinkChecker(safety, resolver=lambda _host: ["1.1.1.1"])
    checker.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    return checker


@pytest.mark.asyncio
async def test_link_redirect_and_pdf_are_recorded():
    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/file.pdf"})
        return httpx.Response(200, headers={"Content-Type": "application/pdf"}, content=b"%PDF-1.7 test")

    checker = checker_with(handler)
    try:
        result = await checker.check("附件", "https://links.test/start")
    finally:
        await checker.client.aclose()
    assert result["result"] == "ok"
    assert result["status_code"] == 200
    assert result["final_url"] == "https://links.test/file.pdf"
    assert result["redirect_chain"] == ["https://links.test/start", "https://links.test/file.pdf"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "content_type", "error_type"),
    [
        (404, b"not found", "text/plain", "http_error"),
        (500, b"server error", "text/plain", "http_error"),
        (200, b"<html>   </html>", "text/html", "empty_page"),
        (200, "<html>页面不存在</html>".encode(), "text/html; charset=utf-8", "business_error"),
    ],
)
async def test_broken_link_types(status, body, content_type, error_type):
    checker = checker_with(lambda _request: httpx.Response(status, headers={"Content-Type": content_type}, content=body))
    try:
        result = await checker.check("政策解读", "https://links.test/page")
    finally:
        await checker.client.aclose()
    assert result["result"] == "broken"
    assert result["error_type"] == error_type


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exception", "error_type"),
    [
        (httpx.ReadTimeout("slow"), "timeout"),
        (httpx.ConnectError("SSL certificate verify failed"), "certificate"),
        (httpx.ConnectError("getaddrinfo failed"), "dns"),
    ],
)
async def test_network_error_types_after_bounded_retries(exception, error_type):
    def handler(request):
        raise exception

    checker = checker_with(handler)
    try:
        result = await checker.check("阅办联动", "https://links.test/error")
    finally:
        await checker.client.aclose()
    assert result["result"] == ("review_required" if error_type == "timeout" else "error")
    assert result["error_type"] == error_type
    if error_type == "timeout":
        assert result["review_status"] == "manual_review"


@pytest.mark.asyncio
async def test_one_failed_related_link_does_not_trigger_main_scan_cooldown():
    calls = 0

    def handler(_request):
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("offline")

    safety = SafetyController(SafetyConfig(), sleep=FakeSleep(), rng=random.Random(1))
    checker = LinkChecker(safety, resolver=lambda _host: ["1.1.1.1"])
    checker.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    try:
        result = await checker.check("阅办联动", "https://links.test/offline")
    finally:
        await checker.client.aclose()

    assert calls == 3
    assert result["result"] == "error"
    assert result["error_type"] == "connection"


@pytest.mark.asyncio
async def test_other_transport_error_is_recorded_without_stopping_scan():
    def handler(_request):
        raise httpx.RemoteProtocolError("peer closed connection")

    checker = checker_with(handler)
    try:
        result = await checker.check("附件", "https://links.test/unstable")
    finally:
        await checker.client.aclose()

    assert result["result"] == "review_required"
    assert result["error_type"] == "transport"
    assert result["review_status"] == "manual_review"


@pytest.mark.asyncio
async def test_direct_link_result_keeps_visible_page_evidence():
    related = RelatedLink(
        "阅办联动",
        "https://links.test/apply",
        source_area="详情页侧栏",
        link_text="在线办理",
        source_page_url="https://www.shanghai.gov.cn/zhengce/detail?id=1",
    )
    checker = checker_with(
        lambda _request: httpx.Response(200, headers={"Content-Type": "text/plain"}, content=b"ok")
    )
    try:
        result = await checker.check(related.kind, related.url, related)
    finally:
        await checker.client.aclose()

    assert result["result"] == "ok"
    assert result["source_area"] == "详情页侧栏"
    assert result["link_text"] == "在线办理"
    assert result["source_page_url"] == related.source_page_url


@pytest.mark.asyncio
async def test_external_link_is_checked_directly_without_requesting_robots_txt():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, headers={"Content-Type": "application/pdf"}, content=b"%PDF-1.7 test")

    checker = checker_with(handler)
    try:
        result = await checker.check("附件", "https://links.test/private/file.pdf")
    finally:
        await checker.client.aclose()
    assert result["result"] == "ok"
    assert calls == ["/private/file.pdf"]


@pytest.mark.asyncio
async def test_http_to_https_broken_link_is_recorded_without_requesting_robots_txt():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.scheme == "http":
            return httpx.Response(302, headers={"Location": str(request.url.copy_with(scheme="https"))})
        return httpx.Response(404, headers={"Content-Type": "text/html"}, content=b"not found")

    checker = checker_with(handler)
    url = "http://links.test/affairs/missing.html"
    try:
        result = await checker.check("阅办联动", url)
    finally:
        await checker.client.aclose()
    assert result["result"] == "broken"
    assert result["error_type"] == "http_error"
    assert result["final_url"] == "https://links.test/affairs/missing.html"
    assert calls == [url, "https://links.test/affairs/missing.html"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "error_type"),
    [
        (403, b"forbidden", "access_restricted"),
        (429, b"too many requests", "access_restricted"),
        (200, "<html>请输入验证码后继续</html>".encode(), "access_restricted"),
    ],
)
async def test_external_access_restriction_is_recorded_without_cooling(status, body, error_type):
    checker = checker_with(
        lambda _request: httpx.Response(status, headers={"Content-Type": "text/html; charset=utf-8"}, content=body)
    )
    try:
        result = await checker.check("阅办联动", "https://links.test/restricted")
    finally:
        await checker.client.aclose()
    assert result["result"] == "review_required"
    assert result["error_type"] == error_type
    assert result["review_status"] == "manual_review"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/private",
        "http://169.254.169.254/latest",
        "http://198.18.0.58/fake-ip-must-not-be-literal",
        "http://[::1]/",
    ],
)
async def test_private_or_local_destinations_are_rejected_before_request(url):
    called = False

    def handler(request):
        nonlocal called
        called = True
        return httpx.Response(200, content=b"unexpected")

    safety = SafetyController(SafetyConfig(), sleep=FakeSleep(), rng=random.Random(1))
    checker = LinkChecker(safety)
    checker.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    try:
        result = await checker.check("附件", url)
    finally:
        await checker.client.aclose()
    assert called is False
    assert result["result"] == "error"
    assert result["error_type"] == "unsafe_destination"


@pytest.mark.asyncio
async def test_tun_fake_ip_is_allowed_only_when_resolved_from_public_hostname():
    called = False

    def handler(_request):
        nonlocal called
        called = True
        return httpx.Response(200, headers={"Content-Type": "text/plain"}, content=b"ok")

    safety = SafetyController(SafetyConfig(), sleep=FakeSleep(), rng=random.Random(1))
    checker = LinkChecker(safety, resolver=lambda _host: ["198.18.0.58"])
    checker.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    try:
        result = await checker.check("阅办联动", "https://services.example.test/apply")
    finally:
        await checker.client.aclose()

    assert called is True
    assert result["result"] == "ok"


@pytest.mark.asyncio
async def test_local_hostname_is_rejected_even_when_tun_returns_fake_ip():
    called = False

    def handler(_request):
        nonlocal called
        called = True
        return httpx.Response(200, content=b"unexpected")

    safety = SafetyController(SafetyConfig(), sleep=FakeSleep(), rng=random.Random(1))
    checker = LinkChecker(safety, resolver=lambda _host: ["198.18.0.58"])
    checker.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    try:
        result = await checker.check("阅办联动", "https://portal.internal/apply")
    finally:
        await checker.client.aclose()

    assert called is False
    assert result["error_type"] == "unsafe_destination"


@pytest.mark.asyncio
async def test_redirect_target_is_validated_before_following():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/secret"})

    checker = checker_with(handler)
    try:
        result = await checker.check("政策解读", "https://links.test/start")
    finally:
        await checker.client.aclose()
    assert calls == ["https://links.test/start"]
    assert result["result"] == "error"
    assert result["error_type"] == "unsafe_destination"


@pytest.mark.asyncio
async def test_same_site_interactive_link_uses_rendered_browser_checker():
    calls = []

    async def rendered(kind, url):
        calls.append((kind, url))
        return {"kind": kind, "url": url, "final_url": url, "status_code": 200, "result": "ok"}

    safety = SafetyController(SafetyConfig(), sleep=FakeSleep(), rng=random.Random(1))
    checker = LinkChecker(safety, rendered_checker=rendered)
    url = "https://www.shanghai.gov.cn/zhengce/detail?businessId=read"
    result = await checker.check("政策解读", url)
    assert result["result"] == "ok"
    assert calls == [("政策解读", url)]


@pytest.mark.asyncio
async def test_same_site_attachment_uses_browser_context_instead_of_plain_http():
    calls = []

    async def rendered(kind, url):
        calls.append((kind, url))
        return {"kind": kind, "url": url, "final_url": url, "status_code": 200, "result": "ok"}

    checker = LinkChecker(
        SafetyController(SafetyConfig(), sleep=FakeSleep(), rng=random.Random(1)),
        rendered_checker=rendered,
    )
    url = "https://www.shanghai.gov.cn/gwk/resource/file?filename=test.pdf"
    result = await checker.check("附件", url)
    assert result["result"] == "ok"
    assert calls == [("附件", url)]


@pytest.mark.asyncio
async def test_rendered_link_timeout_is_recorded_without_raising():
    async def rendered(_kind, _url):
        raise PlaywrightTimeout("Request timed out")

    checker = LinkChecker(
        SafetyController(SafetyConfig(), sleep=FakeSleep(), rng=random.Random(1)),
        rendered_checker=rendered,
    )
    result = await checker.check("附件", "https://www.shanghai.gov.cn/gwk/resource/file?filename=test.pdf")
    assert result["result"] == "review_required"
    assert result["error_type"] == "timeout"
    assert result["review_status"] == "manual_review"


@pytest.mark.asyncio
async def test_fixed_target_host_uses_target_rules_without_generic_dns_classification():
    allowed = []

    def resolver(_host):
        raise AssertionError("固定目标域名不应进入通用外链 DNS 分类")

    checker = LinkChecker(
        SafetyController(SafetyConfig(), sleep=FakeSleep(), rng=random.Random(1)),
        target_allowed_check=allowed.append,
        resolver=resolver,
    )
    url = "https://www.shanghai.gov.cn/gwk/resource/file?filename=test.pdf"
    await checker._ensure_allowed(url)
    assert allowed == [url]
