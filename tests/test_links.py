import random

import httpx
import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeout

from app.collector import LINK_CHECK_USER_AGENT, LinkChecker
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
    checker.client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False,
        headers={"User-Agent": LINK_CHECK_USER_AGENT},
    )
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
async def test_link_checker_sends_a_browser_user_agent():
    # 伊金霍洛旗对 python-httpx 默认 UA 返回 550，一次扫描就误报过 119 条「附件打不开」。
    safety = SafetyController(SafetyConfig(), sleep=FakeSleep(), rng=random.Random(1))
    async with LinkChecker(safety) as checker:
        request = checker.client.build_request("GET", "https://links.test/a.pdf")
    assert request.headers["user-agent"].startswith("Mozilla/5.0")


@pytest.mark.asyncio
async def test_range_header_does_not_drop_the_user_agent():
    # check() 逐请求带 Range，必须与客户端默认 UA 共存而不是覆盖掉。
    seen = {}

    def handler(request):
        seen["ua"] = request.headers.get("user-agent", "")
        seen["range"] = request.headers.get("range", "")
        return httpx.Response(206, headers={"Content-Type": "application/pdf"}, content=b"%PDF-1.7")

    checker = checker_with(handler)
    try:
        result = await checker.check("附件", "https://links.test/a.pdf")
    finally:
        await checker.client.aclose()
    assert seen["ua"].startswith("Mozilla/5.0")
    assert seen["range"] == "bytes=0-65535"
    assert result["result"] == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "mailto:bdrg0908@163.com.%E8%AF%A6%E6%83%85%E9%99%84%E4%BB%B6",
        "tel:047712345678",
        "",
    ],
)
async def test_non_web_link_is_recorded_without_pausing_the_job(url):
    # 鄂尔多斯部门文件里有「附件」写成 mailto: 的条目，早先会抛 SafetyPause 停掉整个任务。
    called = False

    def handler(_request):
        nonlocal called
        called = True
        return httpx.Response(200, content=b"unexpected")

    checker = checker_with(handler)
    try:
        result = await checker.check("附件", url)
    finally:
        await checker.client.aclose()
    assert called is False
    assert result["result"] == "review_required"
    assert result["error_type"] == "unsupported_scheme"
    assert result["review_status"] == "manual_review"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://files.example.test:8443/attach.pdf",
        "https://user:pw@files.example.test/attach.pdf",
    ],
)
async def test_credential_or_port_restriction_is_a_per_link_result(url):
    # 政务附件挂在 8080/8443 很常见：记成这一条查不了可以，停掉整批扫描不行。
    called = False

    def handler(_request):
        nonlocal called
        called = True
        return httpx.Response(200, content=b"unexpected")

    checker = checker_with(handler)
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


def rendered_checker_with(handler, rendered_result):
    """把浏览器请求姿势的固定返回值接进 LinkChecker，独立客户端仍走 mock。"""
    checker = checker_with(handler)
    checker.rendered_hosts = {"links.test"}

    async def rendered(kind, url, related=None):
        return dict(rendered_result)

    checker.rendered_checker = rendered
    return checker


BROWSER_550 = {
    "kind": "附件", "url": "https://links.test/a.pdf", "final_url": "https://links.test/a.pdf",
    "status_code": 550, "result": "broken", "error_type": "http_error", "page_title": "",
    "redirect_chain": ["https://links.test/a.pdf"], "review_status": "confirmed",
}


@pytest.mark.asyncio
async def test_browser_side_5xx_is_reconfirmed_by_an_independent_http_client():
    """实测症状回归：伊金霍洛旗对每篇文档第一个附件回 550，独立客户端取是 200。"""
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, headers={"Content-Type": "application/pdf"}, content=b"%PDF-1.7 real")

    checker = rendered_checker_with(handler, BROWSER_550)
    try:
        result = await checker.check("附件", "https://links.test/a.pdf")
    finally:
        await checker.client.aclose()
    assert seen == ["https://links.test/a.pdf"]
    assert result["result"] == "ok"
    assert result["status_code"] == 200


@pytest.mark.asyncio
async def test_5xx_that_survives_both_request_shapes_stays_a_reported_failure():
    def handler(_request):
        return httpx.Response(550, headers={"Content-Type": "text/plain"}, content=b"refused")

    checker = rendered_checker_with(handler, BROWSER_550)
    try:
        result = await checker.check("附件", "https://links.test/a.pdf")
    finally:
        await checker.client.aclose()
    assert result["result"] == "broken"
    assert result["status_code"] == 550
    assert result["error_type"] == "http_error"


@pytest.mark.asyncio
async def test_browser_side_404_is_final_without_paying_for_a_second_request():
    """404 是真失效，不该为它多打一次请求；只有瞬态性的 5xx 才需要复验。"""
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, headers={"Content-Type": "application/pdf"}, content=b"%PDF-1.7")

    not_found = {**BROWSER_550, "status_code": 404, "error_type": "http_error"}
    checker = rendered_checker_with(handler, not_found)
    try:
        result = await checker.check("附件", "https://links.test/a.pdf")
    finally:
        await checker.client.aclose()
    assert seen == []
    assert result["result"] == "broken"
    assert result["status_code"] == 404

