from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import unicodedata
from datetime import date
from urllib import robotparser
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from playwright.async_api import Browser, BrowserContext, Page, TimeoutError as PlaywrightTimeout

from app.config import DISTRICT_SITE_IDS, TARGET_URL
from app.domain import CooldownPause, ItemReviewRequired, PolicyListItem, PolicyRecord, RelatedLink, SafetyPause
from app.rules import extract_authored_date, extract_document_numbers
from app.safety import RISK_TEXT, SafetyController


DATE_VALUE_RE = re.compile(r"(?:19|20)\d{2}-\d{1,2}-\d{1,2}")
SOURCE_ID_RE = re.compile(r"(?:/|id=)(\d{5,})(?:\.html|&|$)")

DETAIL_METADATA_READY = """() => {
    const text = document.body?.innerText || '';
    return text.includes('发布日期') && (text.includes('发文单位') || text.includes('发布机构'));
}"""


def comparable_title(value: str) -> str:
    """忽略网页渲染带来的换行、空白和全半角差异，仍保留文字顺序校验。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value or ""))


class UnsafeLinkDestination(RuntimeError):
    """关联外链不满足公网访问约束，必须记录但绝不能实际请求。"""


def parse_iso_date(value: str) -> date | None:
    match = DATE_VALUE_RE.search(value or "")
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(0))
    except ValueError:
        return None


def extract_labeled_value(text: str, label: str) -> str:
    pattern = re.compile(re.escape(label) + r"\s*[:：]?\s*([^\n]+)")
    match = pattern.search(text)
    return match.group(1).strip() if match else ""


def policy_list_item_from_api(record: dict, district: str, page_number: int, item_index: int) -> PolicyListItem:
    business_id = str(record.get("businessId") or "").strip()
    site_id = str(record.get("siteId") or DISTRICT_SITE_IDS.get(district, "")).strip()
    if not business_id or not site_id:
        raise SafetyPause("列表公开接口缺少 businessId 或 siteId，采集器需要更新")
    related_links: list[RelatedLink] = []
    for related in record.get("relates") or []:
        related_id = str(related.get("id") or "").strip()
        if related_id:
            related_links.append(RelatedLink(
                "政策解读", f"https://www.shanghai.gov.cn/zhengce/detail?id={related_id}&key=relates"
            ))
    for affair in record.get("affairs") or []:
        affair_url = str(affair.get("link") or affair.get("originUrl") or "").strip()
        if affair_url and all(existing.url != affair_url for existing in related_links):
            related_links.append(RelatedLink("阅办联动", affair_url))
    return PolicyListItem(
        district=district,
        page_number=page_number,
        item_index=item_index,
        title=str(record.get("title") or "").strip(),
        url=f"https://www.shanghai.gov.cn/zhengce/detail?businessId={business_id}&siteId={site_id}",
        published_date=parse_iso_date(str(record.get("publishDate") or record.get("displayDate") or "")),
        related_links=related_links,
    )


def is_attachment_url(url: str) -> bool:
    parsed = urlparse(url)
    candidates = [parsed.path]
    query = parse_qs(parsed.query)
    candidates.extend(query.get("filename", []))
    candidates.extend(query.get("pathname", []))
    return any(value.lower().endswith((".pdf", ".doc", ".docx", ".xls", ".xlsx")) for value in candidates)


def is_district_list_response(response, site_id: str) -> bool:
    if not response.url.endswith("/gwk/policy/page"):
        return False
    try:
        payload = json.loads(response.request.post_data or "{}")
    except (TypeError, json.JSONDecodeError):
        return False
    return payload.get("siteIdList") == [site_id]


class BrowserCollector:
    def __init__(self, browser: Browser, safety: SafetyController):
        self.browser = browser
        self.safety = safety
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._robots: robotparser.RobotFileParser | None = None
        self._current_records: list[dict] = []
        self._estimated_total = 0

    async def __aenter__(self) -> "BrowserCollector":
        self.context = await self.browser.new_context(locale="zh-CN")
        self.page = await self.context.new_page()
        return self

    async def __aexit__(self, *_args) -> None:
        if self.context:
            await self.context.close()

    @property
    def rendered_hosts(self) -> set[str]:
        return {urlparse(TARGET_URL).netloc.lower()}

    async def safe_goto(self, page: Page, url: str):
        self.ensure_allowed(url)
        last_error: Exception | None = None
        for attempt in range(self.safety.config.max_retries + 1):
            await self.safety.before_request()
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                await page.wait_for_timeout(1200)
                visible = (await page.locator("body").inner_text())[:3000]
                status = response.status if response else None
                self.safety.after_request(status, visible)
                if response and urlparse(url).netloc.lower() == urlparse(TARGET_URL).netloc.lower():
                    if urlparse(response.url).netloc.lower() != urlparse(url).netloc.lower():
                        raise SafetyPause(f"目标站发生异常跨域重定向：{response.url}")
                return response
            except SafetyPause:
                raise
            except Exception as exc:
                last_error = exc
                self.safety.after_request(None, error=exc)
                if attempt < self.safety.config.max_retries:
                    await self.safety.retry_wait(attempt + 1)
        raise last_error or RuntimeError("页面访问失败")

    async def check_rendered_link(self, kind: str, url: str) -> dict:
        assert self.context
        if kind == "附件":
            return await self._check_browser_attachment(kind, url)
        page = await self.context.new_page()
        try:
            response = await self.safe_goto(page, url)
            body_text = (await page.locator("body").inner_text()).strip()
            title = await page.title()
            business_error = any(
                marker in body_text for marker in ("页面不存在", "内容不存在", "访问出错", "系统错误")
            )
            empty = not body_text
            status_code = response.status if response else None
            content_type = (await response.header_value("content-type") if response else "") or ""
            is_attachment = kind == "附件" or any(
                marker in content_type.lower()
                for marker in ("application/pdf", "application/msword", "officedocument", "application/vnd.ms-")
            )
            redirect_chain: list[str] = []
            if response:
                request = response.request
                while request:
                    redirect_chain.append(request.url)
                    request = request.redirected_from
                redirect_chain.reverse()
            return {
                "kind": kind, "url": url, "final_url": page.url,
                "status_code": status_code,
                "result": "broken" if (empty and not is_attachment) or business_error or (status_code is not None and status_code >= 400) else "ok",
                "error_type": ("empty_page" if empty and not is_attachment else "business_error" if business_error
                               else "http_error" if status_code is not None and status_code >= 400 else ""),
                "page_title": title, "redirect_chain": redirect_chain or [page.url],
            }
        finally:
            await page.close()

    async def _check_browser_attachment(self, kind: str, url: str) -> dict:
        assert self.context
        self.ensure_allowed(url)
        await self.safety.before_request()
        try:
            response = await self.context.request.get(
                url,
                headers={"Range": "bytes=0-65535"},
                timeout=45_000,
                fail_on_status_code=False,
                max_redirects=0,
            )
            status = response.status
            self.safety.after_request(status, "")
            content_length = response.headers.get("content-length", "")
            location = response.headers.get("location", "")
            if 300 <= status < 400:
                return {
                    "kind": kind,
                    "url": url,
                    "final_url": urljoin(url, location) if location else url,
                    "status_code": status,
                    "result": "broken",
                    "error_type": "redirect_requires_review" if location else "redirect_without_location",
                    "page_title": "",
                    "redirect_chain": [url, urljoin(url, location)] if location else [url],
                }
            empty = content_length == "0"
            ok = status in {200, 206} and not empty
            return {
                "kind": kind,
                "url": url,
                "final_url": response.url,
                "status_code": status,
                "result": "ok" if ok else "broken",
                "error_type": "" if ok else "empty_file" if empty else "http_error" if status >= 400 else "unexpected_status",
                "page_title": "",
                "redirect_chain": [url],
            }
        except SafetyPause:
            raise
        except Exception as exc:
            self.safety.after_request(None, error=exc)
            raise
        finally:
            if "response" in locals():
                await response.dispose()

    async def check_robots(self) -> None:
        assert self.page
        response = await self.safe_goto(self.page, "https://www.shanghai.gov.cn/robots.txt")
        content_type = ((await response.header_value("content-type")) if response else "") or ""
        if not response or response.status != 200 or "text/plain" not in content_type.lower():
            # robots.txt 未发布或被替换为普通页面时，不应阻断主扫描；403/429 等风控状态已由 safe_goto 暂停。
            self._robots = None
            return
        text = await self.page.locator("body").inner_text()
        parser = robotparser.RobotFileParser("https://www.shanghai.gov.cn/robots.txt")
        parser.parse(text.splitlines())
        self._robots = parser
        self.ensure_allowed(TARGET_URL)

    def ensure_allowed(self, url: str) -> None:
        if not self._robots:
            return
        target_host = urlparse(TARGET_URL).netloc.lower()
        if urlparse(url).netloc.lower() == target_host and not self._robots.can_fetch("*", url):
            raise SafetyPause(f"robots.txt 禁止自动访问目标路径：{urlparse(url).path}")

    async def select_district(self, district: str) -> None:
        assert self.page
        await self.safe_goto(self.page, TARGET_URL)
        trigger = self.page.get_by_role("button", name=re.compile("全部区域"))
        if await trigger.count() != 1:
            raise SafetyPause("页面结构已变化：无法定位区县选择器")
        await trigger.click()
        button = self.page.get_by_role("button", name=district, exact=True)
        if await button.count() != 1:
            raise SafetyPause(f"页面结构已变化：无法定位 {district}")
        site_id = DISTRICT_SITE_IDS.get(district)
        if not site_id:
            raise SafetyPause(f"未配置区县站点标识：{district}")
        async with self.page.expect_response(
            lambda response: is_district_list_response(response, site_id),
            timeout=20_000,
        ) as response_info:
            await self._click_and_observe(self.page, button.click, 500)
        await self._load_list_response(await response_info.value, district)
        await self.page.locator(".district-toggle-btn").filter(has_text=district).wait_for(timeout=10_000)

    async def _load_list_response(self, response, district: str) -> None:
        if response.status in {403, 429}:
            raise CooldownPause(f"列表接口触发风控状态：HTTP {response.status}")
        try:
            payload = await response.json()
            data = payload["data"]
            records = data["records"]
            total = int(data["totalCount"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SafetyPause("列表公开接口数据结构已变化，采集器需要更新") from exc
        if not isinstance(records, list):
            raise SafetyPause("列表公开接口 records 字段异常，采集器需要更新")
        if any(str(record.get("siteId") or "") != DISTRICT_SITE_IDS[district] for record in records):
            raise SafetyPause(f"区县筛选结果与目标不一致：{district}")
        self._current_records = records
        self._estimated_total = total

    async def _click_and_observe(self, page: Page, click, wait_ms: int) -> None:
        statuses: list[int] = []

        def capture(response) -> None:
            if response.request.resource_type in {"document", "xhr", "fetch"}:
                statuses.append(response.status)

        page.on("response", capture)
        await self.safety.before_request()
        finished = False
        try:
            await click()
            await page.wait_for_timeout(wait_ms)
            visible = (await page.locator("body").inner_text())[:3000]
            status = next((value for value in statuses if value in {403, 429}), statuses[-1] if statuses else 200)
            self.safety.after_request(status, visible)
            finished = True
        except SafetyPause:
            finished = True
            raise
        except Exception as exc:
            self.safety.after_request(None, error=exc)
            finished = True
            raise
        finally:
            page.remove_listener("response", capture)
            if not finished:
                self.safety.after_request(None, error=RuntimeError("点击访问未正常结束"))

    async def estimated_total(self) -> int:
        return self._estimated_total

    async def iter_items(self, district: str, start_page: int = 1, start_item_index: int = -1):
        assert self.page
        await self.select_district(district)
        current_page = 1
        while current_page < start_page:
            if not await self._next_page():
                return
            current_page += 1
        while True:
            items = self.page.locator(".policy-list-item")
            count = await items.count()
            if count == 0 or not self._current_records:
                raise SafetyPause("页面结构已变化或列表为空：无法定位政策条目")
            if count != len(self._current_records):
                raise SafetyPause("列表页面与公开接口记录数不一致，采集器需要更新")
            for index in range(count):
                if current_page == start_page and index <= start_item_index:
                    continue
                api_item = policy_list_item_from_api(self._current_records[index], district, current_page, index)
                rendered_title = (await items.nth(index).locator("h3").inner_text()).strip()
                if comparable_title(rendered_title) != comparable_title(api_item.title):
                    raise SafetyPause(
                        f"列表数据校验失败：第 {current_page} 页第 {index + 1} 条标题不一致"
                        f"（接口：{api_item.title[:80]}；页面：{rendered_title[:80]}）"
                    )
                yield api_item
            if not await self._next_page():
                return
            current_page += 1

    async def iter_records(self, district: str, start_page: int = 1, start_item_index: int = -1, max_documents: int = 0):
        yielded = 0
        async for item in self.iter_items(district, start_page, start_item_index):
            if max_documents and yielded >= max_documents:
                return
            record = await self.open_item(item)
            yielded += 1
            yield item.page_number, record

    async def open_item(self, item: PolicyListItem) -> PolicyRecord:
        record = await self._open_item(item.district, item.item_index, item.title)
        for related in item.related_links:
            if all(existing.url != related.url for existing in record.related_links):
                record.related_links.append(related)
        return record

    async def _open_item(self, district: str, index: int, title: str) -> PolicyRecord:
        assert self.page and self.context
        list_url = self.page.url
        items = self.page.locator(".policy-list-item")
        item = items.nth(index)
        detail_page: Page | None = None
        responses = []

        def capture(response) -> None:
            if response.request.resource_type == "document":
                responses.append(response)

        self.context.on("response", capture)
        await self.safety.before_request()
        finished = False
        try:
            title_link = item.locator("h3")
            if await title_link.count() != 1:
                raise SafetyPause("政策条目缺少唯一标题点击区域，采集器需要更新")
            async with self.context.expect_page(timeout=10_000) as popup:
                await title_link.click()
            detail_page = await popup.value
            await detail_page.wait_for_load_state("domcontentloaded")
        except PlaywrightTimeout:
            if self.page.url != list_url:
                detail_page = self.page
            else:
                self.safety.after_request(None, error=RuntimeError("详情页未打开"))
                finished = True
                self.context.remove_listener("response", capture)
                raise ItemReviewRequired("政策条目点击后未打开详情页", "detail_open")
        except Exception as exc:
            self.safety.after_request(None, error=exc)
            finished = True
            self.context.remove_listener("response", capture)
            raise
        try:
            await detail_page.wait_for_timeout(1000)
            visible = (await detail_page.locator("body").inner_text())[:3000]
            matching = [response for response in responses if response.url == detail_page.url]
            status = matching[-1].status if matching else (responses[-1].status if responses else None)
            if status is None:
                raise ItemReviewRequired("未捕获到详情页导航响应", "detail_open")
            self.safety.after_request(status, visible)
            finished = True
            await self._wait_for_detail_metadata(detail_page)
            record = await self._parse_detail(detail_page, district, title)
        except SafetyPause:
            finished = True
            raise
        except Exception as exc:
            if not finished:
                self.safety.after_request(None, error=exc)
                finished = True
            raise
        finally:
            self.context.remove_listener("response", capture)
            if not finished:
                self.safety.after_request(None, error=RuntimeError("详情访问未正常结束"))
            if detail_page is not self.page:
                await detail_page.close()
            elif self.page.url != list_url:
                await self.safe_goto(self.page, list_url)
        return record

    async def open_detail_url(self, district: str, url: str, fallback_title: str) -> PolicyRecord:
        """收尾复测时直接打开已记录的详情链接，不依赖列表页当前排序。"""
        assert self.context
        page = await self.context.new_page()
        try:
            await self.safe_goto(page, url)
            await self._wait_for_detail_metadata(page)
            return await self._parse_detail(page, district, fallback_title)
        finally:
            await page.close()

    async def _wait_for_detail_metadata(self, page: Page) -> None:
        """给动态详情页一次受限重载机会，避免瞬时慢加载中断整个任务。"""
        try:
            await page.wait_for_function(DETAIL_METADATA_READY, timeout=20_000)
            return
        except PlaywrightTimeout:
            retry_url = page.url

        await self.safe_goto(page, retry_url)
        try:
            await page.wait_for_function(DETAIL_METADATA_READY, timeout=20_000)
        except PlaywrightTimeout as exc:
            raise ItemReviewRequired("详情页动态内容两次加载后仍不完整", "detail_metadata") from exc

    async def _parse_detail(self, page: Page, district: str, fallback_title: str) -> PolicyRecord:
        url = page.url
        main = page.locator("main")
        text = await main.inner_text() if await main.count() else await page.locator("body").inner_text()
        headings = page.locator("h1")
        title = (await headings.first.inner_text()).strip() if await headings.count() else fallback_title
        agency = extract_labeled_value(text, "发文单位") or extract_labeled_value(text, "发布机构")
        document_number = extract_labeled_value(text, "文号")
        published = parse_iso_date(extract_labeled_value(text, "发布日期"))
        body_locator = page.locator("article, .article-content, .policy-content, .TRS_Editor")
        body_text = text
        if await body_locator.count():
            body_text = await body_locator.first.inner_text()
        links: list[RelatedLink] = []
        anchors = await page.locator("a").evaluate_all(
            "els => els.map(a => ({text:(a.innerText||'').trim(), href:a.href})).filter(x => x.href)"
        )
        for link in anchors:
            label = link["text"]
            href = urljoin(url, link["href"])
            kind = ""
            if "政策解读" in label:
                kind = "政策解读"
            elif "阅办联动" in label:
                kind = "阅办联动"
            elif is_attachment_url(href):
                kind = "附件"
            if kind and href != url and all(existing.url != href for existing in links):
                links.append(RelatedLink(kind, href))
        if not agency or not published:
            raise ItemReviewRequired("详情页缺少发文机构或发布日期", "detail_metadata")
        source_match = SOURCE_ID_RE.search(url)
        query_source_id = (parse_qs(urlparse(url).query).get("businessId") or [""])[0]
        return PolicyRecord(
            district=district,
            title=title,
            url=url,
            source_id=query_source_id or (source_match.group(1) if source_match else ""),
            issuing_agency=agency,
            page_document_number=document_number,
            published_date=published,
            authored_date=extract_authored_date(body_text, agency),
            body_text=body_text,
            body_document_numbers=extract_document_numbers(body_text),
            related_links=links,
            source_site="市级平台",
        )

    async def _next_page(self) -> bool:
        assert self.page
        next_button = self.page.locator('li[title="下一页"] button')
        if await next_button.count() != 1 or await next_button.is_disabled():
            return False
        site_id = next(iter({str(record.get("siteId") or "") for record in self._current_records}), "")
        if not site_id:
            raise SafetyPause("无法确定当前区县站点标识，采集器需要更新")
        await self.safety.before_request()
        try:
            async with self.page.expect_response(
                lambda response: is_district_list_response(response, site_id),
                timeout=20_000,
            ) as response_info:
                await next_button.click()
            response = await response_info.value
            district = next(name for name, configured_id in DISTRICT_SITE_IDS.items() if configured_id == site_id)
            await self._load_list_response(response, district)
            await self.page.wait_for_timeout(500)
            visible = (await self.page.locator("main").inner_text())[:2000]
            self.safety.after_request(response.status, visible)
            return True
        except SafetyPause:
            raise
        except Exception as exc:
            self.safety.after_request(None, error=exc)
            raise


class LinkChecker:
    def __init__(
        self, safety: SafetyController, target_allowed_check=None, resolver=None, rendered_checker=None,
        rendered_hosts: set[str] | None = None,
    ):
        self.safety = safety
        self.target_allowed_check = target_allowed_check
        self.resolver = resolver
        self.rendered_checker = rendered_checker
        self.rendered_hosts = {host.lower() for host in (rendered_hosts or {urlparse(TARGET_URL).netloc})}
        self.client: httpx.AsyncClient | None = None

    async def __aenter__(self):
        self.client = httpx.AsyncClient(follow_redirects=False, timeout=30)
        return self

    async def __aexit__(self, *_args):
        if self.client:
            await self.client.aclose()

    async def _ensure_allowed(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise SafetyPause(f"关联链接不是可检查的 HTTP(S) 地址：{url}")
        if parsed.username or parsed.password or (parsed.port and parsed.port not in {80, 443}):
            raise SafetyPause(f"关联链接包含不安全的认证信息或端口：{url}")
        if parsed.netloc.lower() == urlparse(TARGET_URL).netloc.lower() and self.target_allowed_check:
            self.target_allowed_check(url)
            return
        await self._validate_public_destination(parsed.hostname or "")

    async def _validate_public_destination(self, hostname: str) -> None:
        try:
            literal = ipaddress.ip_address(hostname.strip("[]"))
            addresses = [literal]
        except ValueError:
            try:
                if self.resolver:
                    resolved = self.resolver(hostname)
                    if asyncio.iscoroutine(resolved):
                        resolved = await resolved
                    addresses = [ipaddress.ip_address(value) for value in resolved]
                else:
                    info = await asyncio.to_thread(socket.getaddrinfo, hostname, None, type=socket.SOCK_STREAM)
                    addresses = list({ipaddress.ip_address(item[4][0]) for item in info})
            except (OSError, ValueError) as exc:
                raise UnsafeLinkDestination(f"关联链接域名无法安全解析：{hostname}") from exc
        if not addresses or any(not address.is_global for address in addresses):
            raise UnsafeLinkDestination(f"关联链接解析到本机、内网或非公网地址，已拒绝访问：{hostname}")

    async def check(self, kind: str, url: str) -> dict:
        result = {"kind": kind, "url": url, "result": "error", "error_type": ""}
        if self.rendered_checker and urlparse(url).netloc.lower() in self.rendered_hosts:
            try:
                return await self.rendered_checker(kind, url)
            except SafetyPause:
                raise
            except (PlaywrightTimeout, asyncio.TimeoutError, httpx.TimeoutException) as exc:
                # 页面内附件检查也可能超时；这是单条外链的不确定结果，不能中断整个政策扫描任务。
                self.safety.after_request(None, error=exc)
                result.update(error_type="timeout", final_url="", redirect_chain=[])
                return result
            except Exception as exc:
                self.safety.after_request(None, error=exc)
                result.update(error_type="rendered_check_error", final_url="", redirect_chain=[])
                return result
        if self.client is None:
            async with self:
                return await self.check(kind, url)
        for attempt in range(self.safety.config.max_retries + 1):
            try:
                current_url = url
                redirect_chain: list[str] = []
                for redirect_index in range(6):
                    await self._ensure_allowed(current_url)
                    await self.safety.before_request()
                    async with self.client.stream("GET", current_url, headers={"Range": "bytes=0-65535"}) as response:
                        chunk = await response.aread()
                        visible = chunk[:65536].decode(response.encoding or "utf-8", errors="ignore")
                        # 外链的访问限制属于链接检查结果，不应被误判为主站风控而中断整批扫描。
                        # 仍通过安全控制器完成全局节流、访问计数和事件记录。
                        self.safety.after_request(response.status_code, visible[:3000], enforce_risk=False)
                        redirect_chain.append(str(response.url))
                        if 300 <= response.status_code < 400:
                            location = response.headers.get("location")
                            if not location:
                                result.update(
                                    final_url=str(response.url), status_code=response.status_code,
                                    result="broken", error_type="redirect_without_location",
                                    redirect_chain=redirect_chain,
                                )
                                return result
                            current_url = urljoin(str(response.url), location)
                            continue
                        content_type = response.headers.get("content-type", "").lower()
                        plain_text = re.sub(r"<[^>]+>", "", visible).strip() if "html" in content_type else visible.strip()
                        business_error = any(marker in plain_text for marker in ("页面不存在", "内容不存在", "访问出错", "系统错误"))
                        risk_page = any(marker in plain_text for marker in RISK_TEXT)
                        empty = not chunk or ("html" in content_type and not plain_text)
                        ok = 200 <= response.status_code < 400 and not empty and not business_error and not risk_page
                        result.update(
                            final_url=str(response.url), status_code=response.status_code,
                            result="ok" if ok else "broken",
                            error_type=("empty_page" if empty else "access_restricted" if risk_page else "business_error" if business_error
                                        else "http_error" if response.status_code >= 400 else ""),
                        )
                        result["redirect_chain"] = redirect_chain
                        title = re.search(r"<title[^>]*>(.*?)</title>", visible, re.I | re.S)
                        result["page_title"] = re.sub(r"\s+", " ", title.group(1)).strip() if title else ""
                        if response.status_code >= 500 and attempt < self.safety.config.max_retries:
                            break
                        return result
                else:
                    result.update(
                        final_url=current_url, result="broken", error_type="too_many_redirects",
                        redirect_chain=redirect_chain,
                    )
                    return result
            except UnsafeLinkDestination:
                result.update(result="error", error_type="unsafe_destination", final_url="", redirect_chain=[])
                return result
            except SafetyPause:
                raise
            except httpx.TimeoutException as exc:
                self.safety.after_request(None, error=exc)
                result["error_type"] = "timeout"
            except httpx.ConnectError as exc:
                self.safety.after_request(None, error=exc)
                message = str(exc).lower()
                if "certificate" in message or "ssl" in message:
                    result["error_type"] = "certificate"
                elif "name or service" in message or "getaddrinfo" in message or "nodename" in message:
                    result["error_type"] = "dns"
                else:
                    result["error_type"] = "connection"
            if attempt < self.safety.config.max_retries:
                await self.safety.retry_wait(attempt + 1)
        return result
