from __future__ import annotations

import asyncio
import inspect
import ipaddress
import json
import re
import socket
import unicodedata
from contextlib import suppress
from datetime import date
from pathlib import Path
from typing import Any
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
TUN_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
LOCAL_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home", ".corp")

DETAIL_METADATA_READY = """() => {
    const text = document.body?.innerText || '';
    return text.includes('发布日期') && (text.includes('发文单位') || text.includes('发布机构'));
}"""

RELATED_CLICK_SELECTOR = "a[href], button, [role=button], [onclick], [data-url], [data-href]"
LIST_RELATED_TRIGGER_SELECTOR = '.tag-image[alt="政策解读"], .tag-image[alt="阅办联动"]'
LIST_POPOVER_ITEM_SELECTOR = ".ant-popover .tag-popover-item.clickable:visible"
VISIBLE_LINKS_JS = """els => els.map((el, index) => {
    const style = window.getComputedStyle(el);
    const rects = el.getClientRects();
    const ownText = (el.innerText || el.textContent || '').trim();
    const itemText = (el.closest('li')?.innerText || '').trim();
    const nestedAlt = (el.querySelector('img[alt]')?.getAttribute('alt') || '').trim();
    const text = ownText
        || (el.getAttribute('aria-label') || '').trim()
        || (el.getAttribute('title') || '').trim()
        || (el.getAttribute('alt') || '').trim()
        || itemText
        || nestedAlt;
    const section = el.closest('section, aside, article, .aside-card, .related, .relates, .interpret, .policy-interpret');
    const context = (section?.querySelector('h1, h2, h3, h4, .title, .aside-title')?.innerText || '').trim();
    const rawHref = el.href || el.getAttribute('href') || el.dataset?.url || el.dataset?.href || '';
    const isScriptHref = rawHref.trim().toLowerCase().startsWith('javascript:');
    let href = '';
    if (rawHref && !isScriptHref) {
        try { href = new URL(rawHref, document.baseURI).href; } catch { href = rawHref; }
    }
    return {
        index,
        text,
        context,
        href,
        visible: style.display !== 'none' && style.visibility !== 'hidden' && rects.length > 0,
    };
}).filter(x => x.visible && (x.href || x.text || x.context))"""


def comparable_title(value: str) -> str:
    """忽略网页渲染带来的换行、空白和全半角差异，仍保留文字顺序校验。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value or ""))


class UnsafeLinkDestination(RuntimeError):
    """关联外链不满足公网访问约束，必须记录但绝不能实际请求。"""


def destination_addresses_are_public(
    hostname: str, addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address], *, literal: bool
) -> bool:
    normalized = hostname.rstrip(".").lower()
    if not normalized or normalized == "localhost" or normalized.endswith(LOCAL_HOST_SUFFIXES):
        return False
    if not literal and "." not in normalized:
        return False
    if not addresses:
        return False
    return all(
        address.is_global or (not literal and address in TUN_FAKE_IP_NETWORK)
        for address in addresses
    )


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
    return PolicyListItem(
        district=district,
        page_number=page_number,
        item_index=item_index,
        title=str(record.get("title") or "").strip(),
        url=f"https://www.shanghai.gov.cn/zhengce/detail?businessId={business_id}&siteId={site_id}",
        published_date=parse_iso_date(str(record.get("publishDate") or record.get("displayDate") or "")),
    )


def is_attachment_url(url: str) -> bool:
    parsed = urlparse(url)
    candidates = [parsed.path]
    query = parse_qs(parsed.query)
    candidates.extend(query.get("filename", []))
    candidates.extend(query.get("pathname", []))
    return any(value.lower().endswith((".pdf", ".doc", ".docx", ".xls", ".xlsx")) for value in candidates)


def attachment_payload_error(url: str, content_type: str, payload: bytes) -> tuple[str, str]:
    if not payload:
        return "empty_file", "附件响应为空"
    prefix = payload[:2048].lstrip().lower()
    if "html" in (content_type or "").lower() or prefix.startswith((b"<!doctype", b"<html")):
        return "html_error", "返回 HTML 页面，不是附件文件"
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    candidates = [parsed.path, *query.get("filename", []), *query.get("pathname", [])]
    suffix = next((Path(value).suffix.lower() for value in candidates if Path(value).suffix), "")
    if suffix == ".pdf" and not payload.startswith(b"%PDF"):
        return "invalid_pdf", "PDF 文件头无效"
    if suffix in {".docx", ".xlsx"} and not payload.startswith(b"PK"):
        return "invalid_office_file", "Office OpenXML 文件头无效"
    if suffix in {".doc", ".xls"} and not payload.startswith(b"\xd0\xcf\x11\xe0"):
        return "invalid_office_file", "Office 二进制文件头无效"
    return "", f"download_size={len(payload)}"


def classify_related_link(label: str, url: str) -> str:
    normalized = re.sub(r"\s+", "", label or "")
    if "政策解读" in normalized:
        return "政策解读"
    if "阅办联动" in normalized:
        return "阅办联动"
    if is_attachment_url(url) or "附件" in normalized:
        return "附件"
    return ""


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
        # 导航拦截必须覆盖页面发出的请求；禁用 Service Worker，避免其绕过 route 安全校验。
        self.context = await self.browser.new_context(locale="zh-CN", service_workers="block")
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

    async def check_rendered_link(self, kind: str, url: str, related: RelatedLink | None = None) -> dict:
        assert self.context
        if related and related.source_page_url:
            source_page = await self.context.new_page()
            try:
                await self.safe_goto(source_page, related.source_page_url)
                candidates = source_page.locator(RELATED_CLICK_SELECTOR)
                locator = (
                    candidates.nth(related.element_index)
                    if related.element_index >= 0
                    else candidates.filter(has_text=related.link_text or related.kind).first
                )
                return await self._check_related_click(source_page, locator, related)
            finally:
                await source_page.close()
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
            content_type = response.headers.get("content-type", "")
            payload = await response.body()
            visible = payload[:3000].decode("utf-8", errors="ignore")
            self.safety.after_request(status, visible, enforce_risk=False)
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
            payload_error, evidence = attachment_payload_error(url, content_type, payload)
            empty = content_length == "0" or payload_error == "empty_file"
            review_required = status in {403, 429}
            ok = status in {200, 206} and not payload_error
            return {
                "kind": kind,
                "url": url,
                "final_url": response.url,
                "status_code": status,
                "result": "ok" if ok else "review_required" if review_required else "broken",
                "error_type": (
                    "" if ok else "access_restricted" if review_required else "empty_file" if empty
                    else "http_error" if status >= 400 else payload_error or "unexpected_status"
                ),
                "page_title": "",
                "redirect_chain": [url],
                "review_status": "manual_review" if review_required else "confirmed",
                "evidence": evidence,
            }
        except SafetyPause:
            raise
        except Exception as exc:
            self.safety.after_request(None, error=exc)
            raise
        finally:
            if "response" in locals():
                await response.dispose()

    def _with_link_metadata(self, result: dict[str, Any], related: RelatedLink) -> dict[str, Any]:
        result.setdefault("kind", related.kind)
        result.setdefault("url", related.url)
        result.update(
            source_area=related.source_area,
            link_text=related.link_text,
            source_page_url=related.source_page_url,
            interaction_type=related.interaction_type,
            visible=related.visible,
            review_status=result.get("review_status", "manual_review" if result.get("result") == "review_required" else "confirmed"),
            evidence=result.get("evidence", ""),
        )
        return result

    def _link_error_result(
        self, related: RelatedLink, error_type: str, evidence: str = "", *, result: str = "error"
    ) -> dict[str, Any]:
        return self._with_link_metadata(
            {
                "kind": related.kind,
                "url": related.url,
                "final_url": "",
                "status_code": None,
                "result": result,
                "error_type": error_type,
                "page_title": "",
                "redirect_chain": [],
                "evidence": evidence,
            },
            related,
        )

    async def _extract_visible_links_from_locator(
        self,
        container,
        source_page_url: str,
        source_area: str,
        *,
        allowed_kinds: set[str] | None = None,
    ) -> list[RelatedLink]:
        try:
            anchors = await container.locator(RELATED_CLICK_SELECTOR).evaluate_all(VISIBLE_LINKS_JS)
        except Exception:
            return []
        links: list[RelatedLink] = []
        for anchor in anchors:
            raw_href = str(anchor.get("href") or "").strip()
            href = urljoin(source_page_url, raw_href) if raw_href else ""
            label = str(anchor.get("text") or "").strip()
            context = str(anchor.get("context") or "").strip()
            kind = classify_related_link(f"{context} {label}".strip(), href)
            if not kind or (allowed_kinds and kind not in allowed_kinds) or (href and href == source_page_url):
                continue
            links.append(
                RelatedLink(
                    kind,
                    href,
                    source_area=source_area,
                    link_text=label or context or kind,
                    source_page_url=source_page_url,
                    interaction_type="click",
                    visible=True,
                    element_index=int(anchor.get("index", -1)),
                )
            )
        return links

    async def _visible_list_popover_items(self, page: Page, trigger, *, force_open: bool = False):
        items = page.locator(LIST_POPOVER_ITEM_SELECTOR)
        if not force_open and await items.count() and await items.first.is_visible():
            return items
        if force_open:
            existing_items = items
            with suppress(Exception):
                await page.keyboard.press("Escape")
            if await existing_items.count():
                with suppress(Exception):
                    await existing_items.first.wait_for(state="hidden", timeout=1_500)
            await page.wait_for_timeout(200)
        await trigger.scroll_into_view_if_needed(timeout=5_000)
        await trigger.click(timeout=5_000)
        # Ant Popover 会复用浮层节点；等待内容完成切换，避免读取到上一个图标的条目。
        await page.wait_for_timeout(400)
        items = page.locator(LIST_POPOVER_ITEM_SELECTOR)
        await items.first.wait_for(state="visible", timeout=5_000)
        return items

    async def _extract_and_check_list_popovers(
        self, page: Page, container, source_page_url: str
    ) -> list[RelatedLink]:
        """市级列表的图标先展开弹层，弹层条目才是真正需要检查的可见链接。"""
        triggers = container.locator(LIST_RELATED_TRIGGER_SELECTOR)
        links: list[RelatedLink] = []
        for trigger_index in range(await triggers.count()):
            trigger = triggers.nth(trigger_index)
            if not await trigger.is_visible():
                continue
            kind = classify_related_link((await trigger.get_attribute("alt")) or "", "")
            if not kind:
                continue
            try:
                popover_items = await self._visible_list_popover_items(page, trigger, force_open=True)
                item_texts = [
                    (await popover_items.nth(index).inner_text()).strip()
                    for index in range(await popover_items.count())
                ]
            except (PlaywrightTimeout, asyncio.TimeoutError) as exc:
                related = RelatedLink(
                    kind,
                    "",
                    source_area="列表页",
                    link_text=kind,
                    source_page_url=source_page_url,
                    interaction_type="popover_click",
                    visible=True,
                    element_index=trigger_index,
                )
                related.check_result = self._link_error_result(
                    related,
                    "popover_not_opened",
                    f"点击{kind}图标后未显示可点击条目：{exc}",
                    result="review_required",
                )
                self._add_related_link(links, related)
                continue
            except Exception as exc:
                related = RelatedLink(
                    kind,
                    "",
                    source_area="列表页",
                    link_text=kind,
                    source_page_url=source_page_url,
                    interaction_type="popover_click",
                    visible=True,
                    element_index=trigger_index,
                )
                related.check_result = self._link_error_result(
                    related,
                    "popover_error",
                    f"{type(exc).__name__}: {exc}",
                    result="review_required",
                )
                self._add_related_link(links, related)
                continue

            for item_index, item_text in enumerate(item_texts):
                if not item_text:
                    continue
                related = RelatedLink(
                    kind,
                    "",
                    source_area="列表页",
                    link_text=item_text,
                    source_page_url=source_page_url,
                    interaction_type="popover_click",
                    visible=True,
                    element_index=item_index,
                )
                try:
                    popover_items = await self._visible_list_popover_items(
                        page, trigger, force_open=item_index > 0
                    )
                    locator = popover_items.nth(item_index)
                    related.check_result = await self._check_related_click(page, locator, related)
                    if related.check_result.get("error_type") == "click_no_response":
                        popover_items = await self._visible_list_popover_items(
                            page, trigger, force_open=True
                        )
                        related.check_result = await self._check_related_click(
                            page, popover_items.nth(item_index), related
                        )
                    resolved_url = str(
                        related.check_result.get("url") or related.check_result.get("final_url") or ""
                    ).strip()
                    if resolved_url:
                        related.url = resolved_url
                        related.check_result["url"] = resolved_url
                except SafetyPause:
                    raise
                except Exception as exc:
                    related.check_result = self._link_error_result(
                        related,
                        "popover_click_error",
                        f"{type(exc).__name__}: {exc}",
                        result="review_required",
                    )
                self._add_related_link(links, related)

            with suppress(Exception):
                keyboard = getattr(page, "keyboard", None)
                if keyboard:
                    await keyboard.press("Escape")
        return links

    def _add_related_link(self, links: list[RelatedLink], related: RelatedLink) -> None:
        identity = (
            related.kind,
            related.url,
            related.source_area,
            related.link_text,
            related.source_page_url,
        )
        existing = {
            (link.kind, link.url, link.source_area, link.link_text, link.source_page_url)
            for link in links
        }
        if identity not in existing:
            links.append(related)

    async def _ensure_click_target_allowed(self, url: str) -> None:
        if not url:
            return
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise UnsafeLinkDestination(f"关联链接不是可检查的 HTTP(S) 地址：{url}")
        if parsed.username or parsed.password or (parsed.port and parsed.port not in {80, 443}):
            raise UnsafeLinkDestination(f"关联链接包含不安全的认证信息或端口：{url}")
        hostname = parsed.hostname or ""
        if parsed.netloc.lower() == urlparse(TARGET_URL).netloc.lower():
            self.ensure_allowed(url)
            return
        is_literal = False
        try:
            literal = ipaddress.ip_address(hostname.strip("[]"))
            addresses = [literal]
            is_literal = True
        except ValueError:
            try:
                info = await asyncio.to_thread(socket.getaddrinfo, hostname, None, type=socket.SOCK_STREAM)
                addresses = list({ipaddress.ip_address(item[4][0]) for item in info})
            except (OSError, ValueError) as exc:
                raise UnsafeLinkDestination(f"关联链接域名无法安全解析：{hostname}") from exc
        if not destination_addresses_are_public(hostname, addresses, literal=is_literal):
            raise UnsafeLinkDestination(f"关联链接解析到本机、内网或非公网地址，已拒绝点击：{hostname}")

    def _status_from_responses(self, responses: list, target_url: str, final_url: str = "") -> int | None:
        candidates = [final_url, target_url]
        for candidate in candidates:
            if not candidate:
                continue
            for response in reversed(responses):
                if response.url == candidate:
                    return response.status
        return responses[-1].status if responses else None

    async def _classify_rendered_page(
        self, page: Page, related: RelatedLink, responses: list
    ) -> tuple[dict[str, Any], str]:
        body_text = (await page.locator("body").inner_text()).strip()
        title = await page.title()
        status_code = self._status_from_responses(responses, related.url, page.url)
        business_error = any(marker in body_text for marker in ("页面不存在", "内容不存在", "访问出错", "系统错误"))
        risk_page = status_code in {403, 429} or any(marker in body_text for marker in RISK_TEXT)
        empty = not body_text and related.kind != "附件"
        if risk_page:
            result = "review_required"
            error_type = "access_restricted"
        elif empty:
            result = "broken"
            error_type = "empty_page"
        elif business_error:
            result = "broken"
            error_type = "business_error"
        elif status_code is not None and status_code >= 400:
            result = "broken"
            error_type = "http_error"
        else:
            result = "ok"
            error_type = ""
        return (
            self._with_link_metadata(
                {
                    "kind": related.kind,
                    "url": related.url or page.url,
                    "final_url": page.url,
                    "status_code": status_code,
                    "result": result,
                    "error_type": error_type,
                    "page_title": title,
                    "redirect_chain": [response.url for response in responses] or [page.url],
                },
                related,
            ),
            body_text,
        )

    async def _classify_download(self, download, related: RelatedLink) -> dict[str, Any]:
        failure = await download.failure()
        path = await download.path()
        file_path = Path(path) if path else None
        size = file_path.stat().st_size if file_path else 0
        payload = file_path.read_bytes()[:65536] if file_path and size else b""
        payload_error, evidence = attachment_payload_error(download.url or related.url, "", payload)
        ok = not failure and not payload_error
        return self._with_link_metadata(
            {
                "kind": related.kind,
                "url": related.url or download.url,
                "final_url": download.url,
                "status_code": None,
                "result": "ok" if ok else "broken",
                "error_type": "" if ok else "download_failed" if failure else payload_error,
                "page_title": "",
                "redirect_chain": [download.url],
                "evidence": failure or evidence,
            },
            related,
        )

    async def _check_related_click(self, page: Page, locator, related: RelatedLink) -> dict[str, Any]:
        assert self.context
        try:
            await self._ensure_click_target_allowed(related.url)
        except UnsafeLinkDestination as exc:
            return self._link_error_result(related, "unsafe_destination", str(exc))

        source_url = page.url
        responses = []
        navigation_failure: Exception | None = None
        popup_task = None
        download_task = None
        route_registered = False

        def capture(response) -> None:
            if response.request.resource_type in {"document", "xhr", "fetch"}:
                responses.append(response)

        async def guard_document_navigation(route) -> None:
            nonlocal navigation_failure
            if urlparse(route.request.url).scheme not in {"http", "https"}:
                await route.continue_()
                return
            if navigation_failure is not None:
                await route.abort("blockedbyclient")
                return
            try:
                await self._ensure_click_target_allowed(route.request.url)
            except (UnsafeLinkDestination, SafetyPause) as exc:
                navigation_failure = exc
                await route.abort("blockedbyclient")
                return
            await route.continue_()

        def finish_navigation_failure() -> dict[str, Any] | None:
            if navigation_failure is None:
                return None
            self.safety.after_request(
                None, error=navigation_failure, enforce_risk=False, count_failure=False
            )
            if isinstance(navigation_failure, SafetyPause):
                raise navigation_failure
            return self._link_error_result(
                related, "unsafe_destination", str(navigation_failure), result="error"
            )

        self.context.on("response", capture)
        await self.safety.before_request()
        try:
            await self.context.route("**/*", guard_document_navigation)
            route_registered = True
            popup_task = asyncio.create_task(self.context.wait_for_event("page", timeout=5_000))
            download_task = asyncio.create_task(page.wait_for_event("download", timeout=5_000))
            await locator.scroll_into_view_if_needed(timeout=5_000)
            await locator.click(timeout=10_000)
            await page.wait_for_timeout(800)
            blocked_result = finish_navigation_failure()
            if blocked_result:
                return blocked_result
            if page.url == source_url:
                await asyncio.wait(
                    {popup_task, download_task}, timeout=3, return_when=asyncio.FIRST_COMPLETED
                )
                blocked_result = finish_navigation_failure()
                if blocked_result:
                    return blocked_result

            download = None
            if download_task.done() and not download_task.cancelled():
                with suppress(Exception):
                    download = download_task.result()
            if download:
                result = await self._classify_download(download, related)
                self.safety.after_request(None, "", enforce_risk=False)
                return result

            popup = None
            if popup_task.done() and not popup_task.cancelled():
                with suppress(Exception):
                    popup = popup_task.result()
            if popup:
                try:
                    await popup.wait_for_load_state("domcontentloaded", timeout=15_000)
                    await popup.wait_for_timeout(800)
                    blocked_result = finish_navigation_failure()
                    if blocked_result:
                        return blocked_result
                    result, visible = await self._classify_rendered_page(popup, related, responses)
                    self.safety.after_request(result.get("status_code"), visible[:3000], enforce_risk=False)
                    return result
                finally:
                    await popup.close()

            if page.url != source_url:
                blocked_result = finish_navigation_failure()
                if blocked_result:
                    return blocked_result
                result, visible = await self._classify_rendered_page(page, related, responses)
                self.safety.after_request(result.get("status_code"), visible[:3000], enforce_risk=False)
                with suppress(Exception):
                    await page.go_back(wait_until="domcontentloaded", timeout=10_000)
                    await page.wait_for_timeout(500)
                return result

            status_code = self._status_from_responses(responses, related.url)
            if status_code is not None:
                result = self._with_link_metadata(
                    {
                        "kind": related.kind,
                        "url": related.url or (responses[-1].url if responses else related.source_page_url),
                        "final_url": related.url or (responses[-1].url if responses else related.source_page_url),
                        "status_code": status_code,
                        "result": "review_required" if status_code in {403, 429} else "ok" if status_code < 400 else "broken",
                        "error_type": "access_restricted" if status_code in {403, 429} else "" if status_code < 400 else "http_error",
                        "page_title": "",
                        "redirect_chain": [response.url for response in responses],
                        "review_status": "manual_review" if status_code in {403, 429} else "confirmed",
                    },
                    related,
                )
                self.safety.after_request(status_code, "", enforce_risk=False)
                return result

            self.safety.after_request(None, "", enforce_risk=False)
            return self._link_error_result(related, "click_no_response", "点击后未出现跳转、下载或网络响应", result="broken")
        except (PlaywrightTimeout, asyncio.TimeoutError, httpx.TimeoutException) as exc:
            blocked_result = finish_navigation_failure()
            if blocked_result:
                return blocked_result
            self.safety.after_request(None, error=exc, enforce_risk=False, count_failure=False)
            return self._link_error_result(related, "timeout", str(exc), result="review_required")
        except SafetyPause:
            raise
        except Exception as exc:
            blocked_result = finish_navigation_failure()
            if blocked_result:
                return blocked_result
            self.safety.after_request(None, error=exc, enforce_risk=False, count_failure=False)
            return self._link_error_result(related, "click_error", f"{type(exc).__name__}: {exc}", result="review_required")
        finally:
            if route_registered:
                with suppress(Exception):
                    await self.context.unroute("**/*", guard_document_navigation)
            self.context.remove_listener("response", capture)
            for task in (popup_task, download_task):
                if task is None:
                    continue
                if not task.done():
                    task.cancel()
                with suppress(Exception, asyncio.CancelledError):
                    await task
                if task is popup_task and task.done() and not task.cancelled():
                    with suppress(Exception):
                        pending_popup = task.result()
                        if pending_popup and not pending_popup.is_closed():
                            await pending_popup.close()

    async def _precheck_links_in_container(self, page: Page, container, links: list[RelatedLink]) -> None:
        candidates = container.locator(RELATED_CLICK_SELECTOR)
        for related in links:
            if related.check_result is not None:
                continue
            try:
                locator = candidates.nth(related.element_index) if related.element_index >= 0 else candidates.filter(has_text=related.link_text).first
                related.check_result = await self._check_related_click(page, locator, related)
                if not related.url:
                    related.url = str(
                        related.check_result.get("url") or related.check_result.get("final_url") or ""
                    ).strip()
                    if related.url:
                        related.check_result["url"] = related.url
            except SafetyPause:
                raise
            except Exception as exc:
                related.check_result = self._link_error_result(
                    related, "click_error", f"{type(exc).__name__}: {exc}", result="review_required"
                )

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
                api_item.related_links = await self._extract_visible_links_from_locator(
                    items.nth(index),
                    self.page.url,
                    "列表页",
                    allowed_kinds={"政策解读", "阅办联动"},
                )
                for related in await self._extract_and_check_list_popovers(
                    self.page, items.nth(index), self.page.url
                ):
                    self._add_related_link(api_item.related_links, related)
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
        record = await self._open_item(item)
        for related in item.related_links:
            self._add_related_link(record.related_links, related)
        return record

    async def _open_item(self, item_data: PolicyListItem) -> PolicyRecord:
        assert self.page and self.context
        list_url = self.page.url
        items = self.page.locator(".policy-list-item")
        item = items.nth(item_data.item_index)
        if item_data.related_links:
            await self._precheck_links_in_container(self.page, item, item_data.related_links)
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
            record = await self._parse_detail(detail_page, item_data.district, item_data.title)
            await self._precheck_detail_links(detail_page, record.related_links)
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
            record = await self._parse_detail(page, district, fallback_title)
            await self._precheck_detail_links(page, record.related_links)
            return record
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

    async def _extract_detail_links(self, page: Page, url: str) -> list[RelatedLink]:
        links: list[RelatedLink] = []
        scoped_selectors = [
            ("详情页正文", "article, .article-content, .policy-content, .TRS_Editor, .content, .article, .detail-content, .pages_content, .zw"),
            ("详情页侧栏", "aside, .right, .side, .related, .relates, .interpret, .policy-interpret"),
            ("详情页标题区", ".policy-detail-header, .detail-header, .detail-title, .article-title, h1"),
        ]
        for source_area, selector in scoped_selectors:
            locator = page.locator(selector)
            if not await locator.count():
                continue
            for related in await self._extract_visible_links_from_locator(locator.first, url, source_area):
                self._add_related_link(links, related)
        if links:
            return links
        return links

    async def _precheck_detail_links(self, page: Page, links: list[RelatedLink]) -> None:
        area_selectors = {
            "详情页正文": "article, .article-content, .policy-content, .TRS_Editor, .content, .article, .detail-content, .pages_content, .zw",
            "详情页侧栏": "aside, .right, .side, .related, .relates, .interpret, .policy-interpret",
            "详情页标题区": ".policy-detail-header, .detail-header, .detail-title, .article-title, h1",
        }
        for source_area, selector in area_selectors.items():
            area_links = [link for link in links if link.source_area == source_area]
            if not area_links:
                continue
            locator = page.locator(selector)
            if not await locator.count():
                continue
            await self._precheck_links_in_container(page, locator.first, area_links)

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
        links = await self._extract_detail_links(page, url)
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
        is_literal = False
        try:
            literal = ipaddress.ip_address(hostname.strip("[]"))
            addresses = [literal]
            is_literal = True
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
        if not destination_addresses_are_public(hostname, addresses, literal=is_literal):
            raise UnsafeLinkDestination(f"关联链接解析到本机、内网或非公网地址，已拒绝访问：{hostname}")

    async def _call_rendered_checker(self, kind: str, url: str, related: RelatedLink | None) -> dict:
        assert self.rendered_checker
        try:
            signature = inspect.signature(self.rendered_checker)
            positional = [
                parameter for parameter in signature.parameters.values()
                if parameter.kind in {parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD}
            ]
            has_varargs = any(parameter.kind == parameter.VAR_POSITIONAL for parameter in signature.parameters.values())
            if has_varargs or len(positional) >= 3:
                return await self.rendered_checker(kind, url, related)
        except (TypeError, ValueError):
            pass
        return await self.rendered_checker(kind, url)

    @staticmethod
    def _with_related_metadata(result: dict, related: RelatedLink | None) -> dict:
        if not related:
            return result
        enriched = dict(result)
        enriched.setdefault("source_area", related.source_area)
        enriched.setdefault("link_text", related.link_text)
        enriched.setdefault("source_page_url", related.source_page_url)
        enriched.setdefault("interaction_type", related.interaction_type)
        enriched.setdefault("visible", related.visible)
        enriched.setdefault("review_status", "confirmed")
        enriched.setdefault("evidence", "")
        return enriched

    async def check(self, kind: str, url: str, related: RelatedLink | None = None) -> dict:
        if related and related.check_result:
            return self._with_related_metadata(related.check_result, related)
        result = {"kind": kind, "url": url, "result": "error", "error_type": ""}
        if self.rendered_checker and urlparse(url).netloc.lower() in self.rendered_hosts:
            try:
                return self._with_related_metadata(
                    await self._call_rendered_checker(kind, url, related), related
                )
            except SafetyPause:
                raise
            except (PlaywrightTimeout, asyncio.TimeoutError, httpx.TimeoutException) as exc:
                # 页面内附件检查也可能超时；这是单条外链的不确定结果，不能中断整个政策扫描任务。
                self.safety.after_request(None, error=exc, count_failure=False)
                result.update(
                    result="review_required", error_type="timeout", final_url="", redirect_chain=[],
                    review_status="manual_review",
                )
                return self._with_related_metadata(result, related)
            except Exception as exc:
                self.safety.after_request(None, error=exc, count_failure=False)
                result.update(
                    result="review_required", error_type="rendered_check_error", final_url="", redirect_chain=[],
                    review_status="manual_review",
                )
                return self._with_related_metadata(result, related)
        if self.client is None:
            async with self:
                return await self.check(kind, url, related)
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
                                return self._with_related_metadata(result, related)
                            current_url = urljoin(str(response.url), location)
                            continue
                        content_type = response.headers.get("content-type", "").lower()
                        plain_text = re.sub(r"<[^>]+>", "", visible).strip() if "html" in content_type else visible.strip()
                        business_error = any(marker in plain_text for marker in ("页面不存在", "内容不存在", "访问出错", "系统错误"))
                        risk_page = response.status_code in {403, 429} or any(marker in plain_text for marker in RISK_TEXT)
                        empty = not chunk or ("html" in content_type and not plain_text)
                        ok = 200 <= response.status_code < 400 and not empty and not business_error and not risk_page
                        result.update(
                            final_url=str(response.url), status_code=response.status_code,
                            result="ok" if ok else "review_required" if risk_page else "broken",
                            error_type=("empty_page" if empty else "access_restricted" if risk_page else "business_error" if business_error
                                        else "http_error" if response.status_code >= 400 else ""),
                        )
                        if risk_page:
                            result["review_status"] = "manual_review"
                        result["redirect_chain"] = redirect_chain
                        title = re.search(r"<title[^>]*>(.*?)</title>", visible, re.I | re.S)
                        result["page_title"] = re.sub(r"\s+", " ", title.group(1)).strip() if title else ""
                        if response.status_code >= 500 and attempt < self.safety.config.max_retries:
                            break
                        return self._with_related_metadata(result, related)
                else:
                    result.update(
                        final_url=current_url, result="broken", error_type="too_many_redirects",
                        redirect_chain=redirect_chain,
                    )
                    return self._with_related_metadata(result, related)
            except UnsafeLinkDestination:
                result.update(result="error", error_type="unsafe_destination", final_url="", redirect_chain=[])
                return self._with_related_metadata(result, related)
            except SafetyPause:
                raise
            except httpx.TimeoutException as exc:
                self.safety.after_request(None, error=exc, count_failure=False)
                result["result"] = "review_required"
                result["error_type"] = "timeout"
                result["review_status"] = "manual_review"
            except httpx.ConnectError as exc:
                self.safety.after_request(None, error=exc, count_failure=False)
                message = str(exc).lower()
                if "certificate" in message or "ssl" in message:
                    result["error_type"] = "certificate"
                elif "name or service" in message or "getaddrinfo" in message or "nodename" in message:
                    result["error_type"] = "dns"
                else:
                    result["error_type"] = "connection"
            except httpx.RequestError as exc:
                self.safety.after_request(None, error=exc, count_failure=False)
                result["result"] = "review_required"
                result["error_type"] = "transport"
                result["review_status"] = "manual_review"
            if attempt < self.safety.config.max_retries:
                await self.safety.retry_wait(attempt + 1)
        return self._with_related_metadata(result, related)
