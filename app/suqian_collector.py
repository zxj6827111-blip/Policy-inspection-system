from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from urllib import robotparser
from urllib.parse import urljoin, urlparse

from playwright.async_api import Browser, Page

from app.collector import BrowserCollector, extract_labeled_value, parse_iso_date
from app.config import ScanTarget
from app.domain import (
    DetailInspection,
    ItemReviewRequired,
    PolicyListItem,
    PolicyRecord,
    RelatedLink,
    SafetyPause,
)
from app.rules import extract_declared_document_numbers
from app.safety import SafetyController


SUQIAN_LIST_URL = "https://www.suqian.gov.cn/zcwjk/zcwj/zqt_zcsd_list_data.shtml"
SUQIAN_HOST = "www.suqian.gov.cn"
SUQIAN_MAX_PAYLOAD_BYTES = 20 * 1024 * 1024
# 宿迁 JSONP 列表中各 CHANNEL_ID 对应的站内栏目路径前缀（实测映射）。
SUQIAN_CHANNEL_PREFIXES = {
    "4d79b50ea5684d4b8b63643a46ff6071": "xqbmzc",  # 县区部门政策
    "ea5a76d7d10b413e877769583af8ba3f": "zcwj",    # 市级政策文件
    "c7b7132d5d1648d2b053fd3a8cab52bf": "suyu",    # 宿豫
}
SUQIAN_BASE_URL = "https://www.suqian.gov.cn/zcwjk/"
# 宿迁详情页自有表头口径：无“公开属性”字段；详情页表格无索引号行（索引号取列表接口 C_SYH）。
SUQIAN_METADATA_LABELS = ("发布机构", "文号", "发文日期", "发布日期")
SUQIAN_DETAIL_LABELS = ("发布机构", "文号", "发文日期", "有效期", "名称", "主题分类", "附件下载")


def normalize_label(value: str) -> str:
    """清理标签中的普通/全角空格及零宽字符。"""
    return re.sub(r"[\s\u3000\u200b\u200c\u200d\u2060\ufeff]+", "", value or "")


def suqian_detail_url(record: dict) -> str:
    """按 CHANNEL_ID 映射站内栏目路径；未知频道视为站外记录（返回空字符串）。"""
    channel_id = str(record.get("CHANNEL_ID") or "").strip()
    prefix = SUQIAN_CHANNEL_PREFIXES.get(channel_id)
    if not prefix:
        return ""
    raw_url = str(record.get("URL") or "").strip()
    if not raw_url:
        return ""
    return SUQIAN_BASE_URL + prefix + "/" + raw_url


def parse_suqian_timestamp(value: object) -> date | None:
    """宿迁列表接口使用毫秒时间戳，按北京时区（UTC+8）解释以与详情页日期一致。"""
    if value in (None, "", "0"):
        return None
    try:
        seconds = int(value) / 1000
    except (TypeError, ValueError):
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone(timedelta(hours=8))).date()
    except (OverflowError, OSError, ValueError):
        return None


def suqian_identity(target_key: str, record: dict) -> str:
    metadata_id = str(record.get("MANUSCRIPT_METADATA_ID") or "").strip()
    if metadata_id:
        return f"{target_key}|id:{metadata_id}"
    raw_url = str(record.get("URL") or "").strip()
    if not raw_url:
        raise SafetyPause("宿迁市官网列表记录缺少稳定身份（MANUSCRIPT_METADATA_ID/URL），采集器需要更新")
    return f"{target_key}|url:{SUQIAN_BASE_URL}{raw_url}"


def suqian_list_item(record: dict, page_number: int, item_index: int, target: ScanTarget) -> PolicyListItem:
    title = str(record.get("TITLE") or "").strip()
    if not title:
        raise SafetyPause("宿迁市官网列表记录缺少标题，采集器需要更新")
    url = suqian_detail_url(record)
    if not url:
        # 站外记录（县区/上级机构）：保留列表计数，标记跳过详情。
        url = f"{SUQIAN_BASE_URL}unresolved/{record.get('MANUSCRIPT_METADATA_ID') or item_index}.shtml"
    published = parse_suqian_timestamp(record.get("PUBLISHED_TIME"))
    authored = parse_suqian_timestamp(record.get("C_FWRQ"))
    return PolicyListItem(
        district="宿迁市",
        page_number=page_number,
        item_index=item_index,
        title=title,
        url=url,
        published_date=published,
        source_site=target.label,
        source_key=target.key,
        source_channel_id=target.channel_id,
        stable_id=suqian_identity(target.key, record),
        api_record_id=str(record.get("MANUSCRIPT_METADATA_ID") or ""),
        doc_flag="" if urlparse(url).netloc.lower() == SUQIAN_HOST and "/unresolved/" not in url else "external",
    )


class SuqianCollector(BrowserCollector):
    """宿迁市政策文件库采集器：单发 JSONP 全量列表，详情页表格按标签提取。"""

    def __init__(self, browser: Browser, safety: SafetyController, target: ScanTarget):
        super().__init__(browser, safety)
        self.target = target
        self.list_url = target.list_url or "https://www.suqian.gov.cn/zcwjk/index.shtml"
        self._records: list[dict] = []
        self._total_count: int | None = None
        # 列表接口提供索引号（C_SYH），详情页表格无该行，按 URL 传递。
        self._source_ids: dict[str, str] = {}

    @property
    def rendered_hosts(self) -> set[str]:
        return {SUQIAN_HOST}

    async def check_robots(self) -> None:
        assert self.page
        response = await self.safe_goto(self.page, f"https://{SUQIAN_HOST}/robots.txt")
        content_type = ((await response.header_value("content-type")) if response else "") or ""
        if not response or response.status != 200 or "text/plain" not in content_type.lower():
            # robots.txt 未发布或被替换为普通页面时，不应阻断主扫描。
            self._robots = None
            return
        text = await self.page.locator("body").inner_text()
        parser = robotparser.RobotFileParser(f"https://{SUQIAN_HOST}/robots.txt")
        parser.parse(text.splitlines())
        self._robots = parser
        self.ensure_allowed(self.list_url)

    def ensure_allowed(self, url: str) -> None:
        if self._robots and urlparse(url).netloc.lower() == SUQIAN_HOST:
            if not self._robots.can_fetch("*", url):
                raise SafetyPause(f"robots.txt 禁止自动访问目标路径：{urlparse(url).path}")

    async def _fetch_list(self) -> list[dict]:
        assert self.context
        self.ensure_allowed(SUQIAN_LIST_URL)
        await self.safety.before_request()
        response = None
        try:
            response = await self.context.request.get(
                SUQIAN_LIST_URL,
                timeout=120_000,
                fail_on_status_code=False,
            )
            status = response.status
            payload_bytes = await response.body()
            if len(payload_bytes) > SUQIAN_MAX_PAYLOAD_BYTES:
                raise SafetyPause(f"宿迁市官网列表响应过大：{len(payload_bytes)} 字节")
            visible = payload_bytes[:3000].decode("utf-8", errors="ignore")
            self.safety.after_request(status, visible)
            if status >= 400:
                raise SafetyPause(f"宿迁市官网列表接口返回 HTTP {status}")
            text = payload_bytes.decode("utf-8", errors="ignore")
            match = re.search(r"handleZcsdList\(\s*(\[.*\])\s*\)", text, re.DOTALL)
            if not match:
                raise SafetyPause("宿迁市官网列表接口数据结构发生变化（JSONP 包裹异常），采集器需要更新")
            try:
                records = json.loads(match.group(1))
            except json.JSONDecodeError as exc:
                raise SafetyPause("宿迁市官网列表接口数据结构发生变化（JSON 解析失败），采集器需要更新") from exc
            if not isinstance(records, list):
                raise SafetyPause("宿迁市官网列表接口数据结构发生变化（非数组），采集器需要更新")
            for record in records:
                if not isinstance(record, dict):
                    raise SafetyPause("宿迁市官网列表接口返回了非对象记录，采集器需要更新")
                url = suqian_detail_url(record)
                if url:
                    self._source_ids[url] = str(record.get("C_SYH") or "").strip()
            self._records = records
            self._total_count = len(records)
            return records
        except SafetyPause:
            raise
        except Exception as exc:
            self.safety.after_request(None, error=exc)
            raise
        finally:
            if response:
                await response.dispose()

    async def select_district(self, _district: str) -> None:
        await self._fetch_list()

    async def estimated_total(self) -> int:
        return self._total_count or len(self._records)

    async def iter_items(self, district: str, start_page: int = 1, start_item_index: int = -1):
        if not self._records:
            await self._fetch_list()
        seen: set[str] = set()
        for index, record in enumerate(self._records):
            identity = suqian_identity(self.target.key, record)
            if identity in seen:
                raise SafetyPause(f"宿迁市官网列表出现重复记录，已暂停：{identity}")
            seen.add(identity)
            if index <= start_item_index:
                continue
            yield suqian_list_item(record, 1, index, self.target)

    async def open_item(self, item: PolicyListItem) -> DetailInspection:
        if item.doc_flag == "external":
            # 站外记录（县区/上级机构）：保留列表计数，不打开详情，不产生问题。
            return DetailInspection(record=None, header_detected=False)
        assert self.context
        detail_page = await self.context.new_page()
        try:
            response = await self.safe_goto(detail_page, item.url)
            await detail_page.wait_for_timeout(1000)
            if response and response.status >= 400:
                raise ItemReviewRequired(
                    f"详情页返回 HTTP {response.status}", "detail_load"
                )
            detail = await self._parse_suqian_detail(detail_page, item.title)
            if detail.record is not None:
                await self._precheck_detail_links(detail_page, detail.record.related_links)
            return detail
        finally:
            await detail_page.close()

    async def open_detail_url(self, _district: str, url: str, fallback_title: str) -> DetailInspection:
        assert self.context
        detail_page = await self.context.new_page()
        try:
            response = await self.safe_goto(detail_page, url)
            await detail_page.wait_for_timeout(1000)
            if response and response.status >= 400:
                raise ItemReviewRequired(
                    f"详情页返回 HTTP {response.status}", "detail_load"
                )
            detail = await self._parse_suqian_detail(detail_page, fallback_title)
            if detail.record is not None:
                await self._precheck_detail_links(detail_page, detail.record.related_links)
            return detail
        finally:
            await detail_page.close()

    async def _parse_suqian_detail(self, page: Page, fallback_title: str) -> DetailInspection:
        text = await page.locator("body").inner_text()
        # 404/失效详情页检测：列表存在但详情打不开时走采集异常，而不是无表头 PASS。
        if any(marker in text for marker in ("页面不存在", "内容不存在", "访问出错", "系统错误", "您访问的页面")):
            raise ItemReviewRequired("详情页不存在或已失效", "detail_load")
        title_locator = page.locator("h1")
        title = (await title_locator.first.inner_text()).strip() if await title_locator.count() else ""
        if not title:
            mc_locator = page.locator("#ysj_mc")
            if await mc_locator.count():
                title = (await mc_locator.first.inner_text()).strip()
        if not title:
            title = fallback_title
        values = await self._extract_suqian_table_values(page)
        # 发布日期在正文头部区域，非表格行，单独提取。
        raw_published = extract_labeled_value(text, "发布日期")
        published = parse_iso_date(raw_published.strip()[:10]) if raw_published else None
        # 表头检测：元数据表格中的 发布机构/文号/发文日期 至少命中两项，
        # 避免仅含“发布日期”字样的普通新闻页被误判为政策详情。
        table_labels = ("发布机构", "文号", "发文日期")
        table_hits = sum(1 for label in table_labels if values.get(label))
        header_detected = table_hits >= 2
        if not header_detected:
            return DetailInspection(record=None, header_detected=False)

        raw_authored = values.get("发文日期", "")
        authored = parse_iso_date(raw_authored.strip()[:10]) if raw_authored else None
        missing_fields = [label for label in SUQIAN_METADATA_LABELS if not values.get(label) and label != "发布日期"]
        if not raw_published:
            missing_fields.append("发布日期")
        invalid_fields = []
        if raw_published and not published:
            invalid_fields.append("发布日期（格式无效）")
        if raw_authored and not authored:
            invalid_fields.append("发文日期（格式无效）")

        body_locator = page.locator("#zoomcon, .article-content-body")
        body_text = (
            await body_locator.first.inner_text()
            if await body_locator.count()
            else text
        )
        links = await self._extract_suqian_detail_links(page, page.url)
        record = PolicyRecord(
            district="宿迁市", title=title, url=page.url,
            source_id=self._source_ids.get(page.url, "") or extract_labeled_value(text, "索引号") or "",
            source_site=self.target.label, issuing_agency=values.get("发布机构", ""),
            page_document_number=values.get("文号", ""),
            published_date=published, authored_date=authored,
            body_text=body_text,
            body_document_numbers=extract_declared_document_numbers(body_text),
            related_links=links,
            topic_category=values.get("主题分类", ""),
            header_detected=True, missing_metadata_fields=[*missing_fields, *invalid_fields],
        )
        return DetailInspection(
            record=record, header_detected=True, missing_fields=missing_fields, invalid_fields=invalid_fields,
        )

    async def _extract_suqian_table_values(self, page: Page) -> dict[str, str]:
        """宿迁详情页元数据表按 th/td 配对读取，避免同一行标签互相吞并。"""
        rows = page.locator(".table_ysj table tr")
        if not await rows.count():
            return {}
        pairs = await rows.evaluate_all(
            """trs => trs.map(tr => {
                const out = [];
                const ths = Array.from(tr.querySelectorAll('th'));
                const tds = Array.from(tr.querySelectorAll('td'));
                for (let i = 0; i < ths.length && i < tds.length; i++) {
                    const label = (ths[i].innerText || '').trim().replace(/[:：]+$/, '').trim();
                    const value = (tds[i].innerText || '').trim();
                    if (label) out.push({label, value});
                }
                return out;
            })"""
        )
        values: dict[str, str] = {}
        for row in pairs:
            for pair in row:
                label = str(pair.get("label") or "").strip()
                if not label:
                    continue
                value = str(pair.get("value") or "").strip()
                if label not in values:
                    values[label] = value
        return values

    async def _extract_suqian_detail_links(self, page: Page, url: str) -> list[RelatedLink]:
        links: list[RelatedLink] = []
        fujian = page.locator("#fujian a, span.fujian a")
        if await fujian.count():
            for related in await self._extract_visible_links_from_locator(fujian, url, "详情页附件"):
                if related.kind:
                    links.append(related)
        return links


# ==================== 宿迁市政府文件栏目（信息公开体系） ====================

SUQIAN_SZFWJ_BASE = "https://www.suqian.gov.cn/cnsq/szfwj/"


def parse_szfwj_list_page(html: str, page_number: int, target: ScanTarget) -> list[PolicyListItem]:
    """解析 szfwj 列表页 <li><a>标题</a> <span>日期</span></li> 结构。"""
    items: list[PolicyListItem] = []
    pattern = re.compile(
        r'<a\s+href="(/cnsq/szfwj/\d{6}/[^"]+\.shtml)"[^>]*title=[\'"]([^\'"]*)[\'"][^>]*>.*?</a>\s*<span>([\d-]+)</span>',
        re.DOTALL,
    )
    for index, match in enumerate(pattern.finditer(html)):
        path, title, raw_date = match.group(1), match.group(2).strip(), match.group(3).strip()
        if not title:
            continue
        url = "https://www.suqian.gov.cn" + path
        published = parse_iso_date(raw_date) if raw_date else None
        items.append(
            PolicyListItem(
                district="宿迁市",
                page_number=page_number,
                item_index=index,
                title=title,
                url=url,
                published_date=published,
                source_site=target.label,
                source_key=target.key,
                source_channel_id=target.channel_id,
                stable_id=f"{target.key}|url:{url}",
            )
        )
    return items


class SuqianSzfwjCollector(BrowserCollector):
    """宿迁市政府文件栏目采集器：静态分页列表 + c1 表格详情解析。"""

    # szfwj 详情页 c1 标签实际值：名称/索引号/分类/发布机构/发文日期/文号/关键词/文件下载/时效。
    # 其中“发文日期”即公开日期（页面无独立“公开日期”行）。
    SZFWJ_REQUIRED_LABELS = ("索引号", "发布机构", "文号", "发文日期")

    def __init__(self, browser: Browser, safety: SafetyController, target: ScanTarget):
        super().__init__(browser, safety)
        self.target = target
        self.list_url = target.list_url or "https://www.suqian.gov.cn/cnsq/szfwj/xxgk_list.shtml"
        self._total_count: int | None = None
        self._declared_pages: int | None = None
        self._current_page = 1
        self._current_items: list[PolicyListItem] = []
        self._seen_identities: set[str] = set()

    @property
    def rendered_hosts(self) -> set[str]:
        return {SUQIAN_HOST}

    async def check_robots(self) -> None:
        assert self.page
        response = await self.safe_goto(self.page, f"https://{SUQIAN_HOST}/robots.txt")
        content_type = ((await response.header_value("content-type")) if response else "") or ""
        if not response or response.status != 200 or "text/plain" not in content_type.lower():
            self._robots = None
            return
        text = await self.page.locator("body").inner_text()
        parser = robotparser.RobotFileParser(f"https://{SUQIAN_HOST}/robots.txt")
        parser.parse(text.splitlines())
        self._robots = parser
        self.ensure_allowed(self.list_url)

    def ensure_allowed(self, url: str) -> None:
        if self._robots and urlparse(url).netloc.lower() == SUQIAN_HOST:
            if not self._robots.can_fetch("*", url):
                raise SafetyPause(f"robots.txt 禁止自动访问目标路径：{urlparse(url).path}")

    async def _fetch_page(self, page_number: int) -> list[PolicyListItem]:
        assert self.context
        page_url = (
            self.list_url
            if page_number <= 1
            else self.list_url.replace("xxgk_list.shtml", f"xxgk_list_{page_number}.shtml")
        )
        self.ensure_allowed(page_url)
        await self.safety.before_request()
        response = None
        try:
            response = await self.context.request.get(page_url, timeout=45_000, fail_on_status_code=False)
            status = response.status
            text = await response.text()
            self.safety.after_request(status, text[:3000])
            if status >= 400:
                raise SafetyPause(f"宿迁市政府文件列表接口返回 HTTP {status}")
            # 页数声明：createPageHTML('page_div', total_page, curr, 'xxgk_list', 'shtml', total_count)
            total_match = re.search(
                r"createPageHTML\([^,]*,\s*(\d+)\s*,\s*(\d+)\s*,[^)]*,\s*(\d+)\s*\)", text
            )
            if total_match:
                total_pages = int(total_match.group(1))
                self._total_count = int(total_match.group(3))
                self._declared_pages = total_pages
                if page_number > total_pages:
                    raise SafetyPause(
                        f"宿迁市政府文件列表请求页码超出范围：{page_number}/{total_pages}"
                    )
            elif page_number > 1:
                raise SafetyPause("宿迁市政府文件列表页数声明缺失，采集器需要更新")
            items = parse_szfwj_list_page(text, page_number, self.target)
            self._current_page = page_number
            self._current_items = items
            return items
        except SafetyPause:
            raise
        except Exception as exc:
            self.safety.after_request(None, error=exc)
            raise
        finally:
            if response:
                await response.dispose()

    async def select_district(self, _district: str) -> None:
        await self._fetch_page(1)
        if not self._current_items and self._total_count in (None, 0):
            self._total_count = 0

    async def estimated_total(self) -> int:
        return self._total_count or 0

    async def iter_items(self, district: str, start_page: int = 1, start_item_index: int = -1):
        self._seen_identities = set()
        page_number = max(1, int(start_page))
        while True:
            if page_number != self._current_page:
                await self._fetch_page(page_number)
            items = list(self._current_items or [])
            if not items:
                if self._total_count and page_number == 1:
                    raise SafetyPause("宿迁市政府文件列表首页为空，已暂停")
                break
            identities = [item.stable_id for item in items]
            if len(set(identities)) != len(identities):
                raise SafetyPause("宿迁市政府文件列表同一页出现重复记录，已暂停")
            for item in items:
                if page_number == start_page and item.item_index <= start_item_index:
                    self._seen_identities.add(item.stable_id)
                    continue
                if item.stable_id in self._seen_identities:
                    raise SafetyPause(f"宿迁市政府文件列表出现跨页重复记录：{item.stable_id}")
                self._seen_identities.add(item.stable_id)
                yield item
            declared_pages = self._declared_pages or page_number
            if page_number >= declared_pages:
                break
            page_number += 1

    async def open_item(self, item: PolicyListItem) -> DetailInspection:
        assert self.context
        detail_page = await self.context.new_page()
        try:
            response = await self.safe_goto(detail_page, item.url)
            await detail_page.wait_for_timeout(1000)
            if response and response.status >= 400:
                raise ItemReviewRequired(f"详情页返回 HTTP {response.status}", "detail_load")
            detail = await self._parse_szfwj_detail(detail_page, item.title)
            if detail.record is not None:
                await self._precheck_detail_links(detail_page, detail.record.related_links)
            return detail
        finally:
            await detail_page.close()

    async def open_detail_url(self, _district: str, url: str, fallback_title: str) -> DetailInspection:
        assert self.context
        detail_page = await self.context.new_page()
        try:
            response = await self.safe_goto(detail_page, url)
            await detail_page.wait_for_timeout(1000)
            if response and response.status >= 400:
                raise ItemReviewRequired(f"详情页返回 HTTP {response.status}", "detail_load")
            detail = await self._parse_szfwj_detail(detail_page, fallback_title)
            if detail.record is not None:
                await self._precheck_detail_links(detail_page, detail.record.related_links)
            return detail
        finally:
            await detail_page.close()

    async def _parse_szfwj_detail(self, page: Page, fallback_title: str) -> DetailInspection:
        text = await page.locator("body").inner_text()
        if any(marker in text for marker in ("页面不存在", "内容不存在", "访问出错", "系统错误", "您访问的页面")):
            raise ItemReviewRequired("详情页不存在或已失效", "detail_load")
        title_locator = page.locator("#zoomtitle")
        title = (await title_locator.first.inner_text()).strip() if await title_locator.count() else ""
        if not title:
            title = fallback_title
        values = await self._extract_szfwj_table_values(page)
        header_detected = any(label in values for label in self.SZFWJ_REQUIRED_LABELS)
        if not header_detected:
            return DetailInspection(record=None, header_detected=False)

        missing_fields = [label for label in self.SZFWJ_REQUIRED_LABELS if not values.get(label)]
        invalid_fields = []
        published = parse_iso_date(values.get("发文日期", "")[:10])
        if values.get("发文日期") and not published:
            invalid_fields.append("发文日期（格式无效）")
        body_locator = page.locator("#zoomcon, .article-content .article")
        body_text = await body_locator.first.inner_text() if await body_locator.count() else text
        links = await self._extract_szfwj_detail_links(page, page.url)
        record = PolicyRecord(
            district="宿迁市", title=title, url=page.url,
            source_id=values.get("索引号", ""),
            source_site=self.target.label, issuing_agency=values.get("发布机构", ""),
            page_document_number=values.get("文号", ""),
            published_date=published, authored_date=None,
            body_text=body_text,
            body_document_numbers=extract_declared_document_numbers(body_text),
            related_links=links,
            header_detected=True, missing_metadata_fields=[*missing_fields, *invalid_fields],
        )
        return DetailInspection(
            record=record, header_detected=True, missing_fields=missing_fields, invalid_fields=invalid_fields,
        )

    async def _extract_szfwj_table_values(self, page: Page) -> dict[str, str]:
        """szfwj 详情页 c1 标签 + 后续 td 值。"""
        label_cells = page.locator("table.table_content td.c1")
        if not await label_cells.count():
            return {}
        pairs = await label_cells.evaluate_all(
            """els => els.map(el => {
                const cells = Array.from(el.parentElement.querySelectorAll('td'));
                const idx = cells.indexOf(el);
                const next = cells[idx + 1];
                return {
                    label: (el.innerText || '').trim(),
                    value: next ? (next.innerText || '').trim() : '',
                };
            })"""
        )
        values: dict[str, str] = {}
        for pair in pairs:
            label = normalize_label(str(pair.get("label") or ""))
            if not label or label in values:
                continue
            values[label] = str(pair.get("value") or "").strip()
        return values

    async def _extract_szfwj_detail_links(self, page: Page, url: str) -> list[RelatedLink]:
        links: list[RelatedLink] = []
        # 文件下载行的 doc/pdf 附件
        for selector in ("table a[href]", "#zoomcon a[href]"):
            locator = page.locator(selector)
            if not await locator.count():
                continue
            for related in await self._extract_visible_links_from_locator(locator, url, "详情页附件"):
                if related.kind and related.url not in {link.url for link in links}:
                    links.append(related)
        return links
