from __future__ import annotations

import re
from datetime import date
from math import ceil
from urllib import robotparser
from urllib.parse import urljoin, urlparse

from playwright.async_api import Browser, Page

from app.collector import BrowserCollector, parse_iso_date
from app.config import ScanTarget
from app.domain import DetailInspection, ItemReviewRequired, PolicyListItem, PolicyRecord, RelatedLink, SafetyPause
from app.rules import extract_authored_date, extract_declared_document_numbers
from app.safety import SafetyController


HUAIAN_API_URL = "https://www.huaian.gov.cn/search/zcwj.do"
HUAIAN_ROOT_DEPT_ID = "0000000064a8f16d0164ad1d9d730006"
HUAIAN_HOST = "www.huaian.gov.cn"
HUAIAN_PAGE_SIZE = 100
HUAIAN_MAX_EMPTY_PAGES = 1
# 县区文件栏目使用各县区独立政府域名（全量列表实测固定集合）。
HUAIAN_COUNTY_HOSTS = frozenset({
    "www.zghy.gov.cn",     # 洪泽区
    "www.jinhu.gov.cn",    # 金湖县
    "www.zghaq.gov.cn",    # 淮安区
    "www.xuyi.gov.cn",     # 盱眙县
    "www.hongze.gov.cn",   # 洪泽区（政府主站）
    "www.lianshui.gov.cn", # 涟水县
    "www.haqjp.gov.cn",    # 清江浦区
})
# 淮安详情页自有表头口径：无“公开属性/成文日期”字段，成文日期从正文落款提取。
HUAIAN_METADATA_LABELS = ("索引号", "发布机构", "文号", "公开日期")
HUAIAN_DETAIL_LABELS = (
    "索引号", "发布机构", "文号", "公开日期", "失效日期", "主题分类", "体裁分类", "名称", "关键词", "内容概述", "时效说明",
)
_REQUIRED_URL_FIELDS = ("id", "title", "path", "domain", "release_time")


def is_huaian_trusted_host(host: str) -> bool:
    """本域、*.huaian.gov.cn 子域以及县区独立政府域名视为可信；其余视为外链。"""
    host = (host or "").lower()
    if host == HUAIAN_HOST or host.endswith(".huaian.gov.cn"):
        return True
    return host in HUAIAN_COUNTY_HOSTS


def parse_huaian_date(value: str) -> date | None:
    if not value:
        return None
    return parse_iso_date(str(value).strip()[:10])


def normalize_label(value: str) -> str:
    """清理标签中的普通/全角空格及零宽字符。"""
    return re.sub(r"[\s\u3000\u200b\u200c\u200d\u2060\ufeff]+", "", value or "")


# 县区站点字段常带零宽空格等不可见字符，统一清理。
_INVISIBLE_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")


def clean_field(value: str | None) -> str:
    return _INVISIBLE_RE.sub("", value or "").strip()


async def extract_huaian_table_values(page: Page) -> dict[str, str]:
    """淮安详情页元数据表按行读取标签/值，跳过隐藏行。

    兼容两套表格：
    - 主站/子域: table.wk1（标签/值单元格相邻配对）
    - 县区站: table.xxgk_table（tdlab 标签 + 后续 td 值，标签含全角空格）
    """
    rows = page.locator("table.wk1 tr, table.xxgk_table tr")
    if not await rows.count():
        return {}
    pairs = await rows.evaluate_all(
        """trs => trs.map(tr => {
            const style = window.getComputedStyle(tr);
            if (style.display === 'none') return null;
            const cells = Array.from(tr.querySelectorAll('td'));
            const out = [];
            for (let i = 0; i < cells.length; i++) {
                const label = (cells[i].innerText || '').trim();
                // 标签单元格：主站 .wz1 或县区站 .tdlab
                if (!label || !cells[i].classList.contains('wz1') && !cells[i].classList.contains('tdlab')) continue;
                const next = cells[i + 1];
                const value = next ? (next.innerText || '').trim() : '';
                out.push({label, value});
                i += 1;
            }
            return out;
        })"""
    )
    values: dict[str, str] = {}
    for row in pairs:
        if not row:
            continue
        for pair in row:
            label = normalize_label(str(pair.get("label") or ""))
            if not label:
                continue
            value = str(pair.get("value") or "").strip()
            if label not in values:
                values[label] = value
    return values


class HuaianCollector(BrowserCollector):
    """淮安市政策文件库三类栏目共用的安全采集器（zfwjKey 区分来源）。"""

    def __init__(
        self,
        browser: Browser,
        safety: SafetyController,
        target: ScanTarget,
        *,
        page_size: int = HUAIAN_PAGE_SIZE,
    ):
        super().__init__(browser, safety)
        self.target = target
        self.list_url = target.list_url
        self.zfwj_key = target.channel_id or "市政府文件"
        self.page_size = int(page_size)
        self._declared_total_count: int | None = None
        self._current_page = 1
        self._current_records: list[dict] = []
        self._seen_identities: set[str] = set()

    @property
    def rendered_hosts(self) -> set[str]:
        return {HUAIAN_HOST}

    async def check_robots(self) -> None:
        assert self.page
        response = await self.safe_goto(self.page, f"https://{HUAIAN_HOST}/robots.txt")
        content_type = ((await response.header_value("content-type")) if response else "") or ""
        if not response or response.status != 200 or "text/plain" not in content_type.lower():
            # robots.txt 未发布或被替换为普通页面时，不应阻断主扫描。
            self._robots = None
            return
        text = await self.page.locator("body").inner_text()
        parser = robotparser.RobotFileParser(f"https://{HUAIAN_HOST}/robots.txt")
        parser.parse(text.splitlines())
        self._robots = parser
        self.ensure_allowed(self.list_url)

    def ensure_allowed(self, url: str) -> None:
        if self._robots and urlparse(url).netloc.lower() == HUAIAN_HOST:
            if not self._robots.can_fetch("*", url):
                raise SafetyPause(f"robots.txt 禁止自动访问目标路径：{urlparse(url).path}")

    def _record_identity(self, record: dict) -> str:
        record_id = record.get("id")
        if record_id is not None and str(record_id).strip() != "":
            return f"{self.target.key}|id:{record_id}"
        raw_url = str(record.get("path") or "").strip()
        if not raw_url:
            raise SafetyPause("淮安市官网列表记录缺少稳定身份（id/path），采集器需要更新")
        return f"{self.target.key}|url:{urljoin(record.get('domain') or self.list_url, raw_url)}"

    def _item_from_record(self, record: dict, page_number: int, item_index: int) -> PolicyListItem:
        title = str(record.get("title") or "").strip()
        path = str(record.get("path") or "").strip()
        domain = str(record.get("domain") or "").strip()
        if not title or not path:
            raise SafetyPause("淮安市官网列表记录缺少标题或详情地址，采集器需要更新")
        link = str(record.get("link") or "").strip()
        if link:
            url = urljoin(domain, link)
        else:
            url = urljoin(domain, path)
        # 外链记录（非本域/子域/县区可信域）保留在列表计数中，但不打开详情。
        trusted = is_huaian_trusted_host(urlparse(url).netloc)
        published = parse_huaian_date(str(record.get("release_time") or ""))
        identity = self._record_identity(record)
        return PolicyListItem(
            district="淮安市",
            page_number=page_number,
            item_index=item_index,
            title=title,
            url=url,
            published_date=published,
            source_site=self.target.label,
            source_key=self.target.key,
            source_channel_id=self.target.channel_id,
            stable_id=identity,
            api_record_id=str(record.get("id") or ""),
            doc_flag="" if trusted else "external",
        )

    async def _fetch_page(self, page_number: int) -> list[dict]:
        assert self.context
        self.ensure_allowed(HUAIAN_API_URL)
        await self.safety.before_request()
        response = None
        try:
            response = await self.context.request.post(
                HUAIAN_API_URL,
                form={
                    "q": "",
                    "sortField": "release_time",
                    "pageSize": str(self.page_size),
                    "pageNo": str(page_number),
                    "rootDeptId": HUAIAN_ROOT_DEPT_ID,
                    "searchModel": "precise",
                    "searchField": "title",
                    "timeRange": "",
                    "zctagid": "",
                    "orgKey": "",
                    "yearKey": "",
                    "zfwjKey": self.zfwj_key,
                    "xqKey": "",
                    "highlightSize": "60",
                },
                timeout=45_000,
                fail_on_status_code=False,
            )
            status = response.status
            text = await response.text()
            self.safety.after_request(status, text[:3000])
            if status >= 400:
                raise SafetyPause(f"淮安市官网列表接口返回 HTTP {status}")
            try:
                payload = await response.json()
            except Exception as exc:
                raise SafetyPause(
                    "淮安市官网列表接口数据结构发生变化（JSON 解析失败），采集器需要更新"
                ) from exc
            value = payload.get("value")
            if not isinstance(value, dict):
                raise SafetyPause("淮安市官网列表接口数据结构发生变化，采集器需要更新")
            total = int(value.get("total") or 0)
            records = value.get("list")
            if not isinstance(records, list) or total < 0:
                raise SafetyPause("淮安市官网列表接口数据结构发生变化，采集器需要更新")
            if self._declared_total_count is None and total > 0:
                self._declared_total_count = total
            if self._declared_total_count is not None and total != self._declared_total_count and total > 0:
                raise SafetyPause(
                    f"淮安市官网列表总量在扫描期间发生变化：{self._declared_total_count} -> {total}"
                )
            if self._declared_total_count is not None and self._declared_total_count > 0:
                declared_pages = ceil(self._declared_total_count / self.page_size)
                if page_number <= declared_pages and records:
                    expected_rows = self.page_size if page_number < declared_pages else (
                        self._declared_total_count - self.page_size * (declared_pages - 1)
                    )
                    if len(records) != expected_rows:
                        raise SafetyPause(
                            f"淮安市官网列表第 {page_number} 页条数异常："
                            f"应为 {expected_rows} 条，实际 {len(records)} 条"
                        )
            self._current_page = page_number
            self._current_records = records
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
        await self._fetch_page(1)

    async def estimated_total(self) -> int:
        return self._declared_total_count or 0

    async def iter_items(self, district: str, start_page: int = 1, start_item_index: int = -1):
        self._seen_identities = set()
        page_number = max(1, int(start_page))
        empty_streak = 0
        declared_pages = (
            ceil(self._declared_total_count / self.page_size)
            if self._declared_total_count and self._declared_total_count > 0
            else 0
        )
        while True:
            if page_number != self._current_page:
                await self._fetch_page(page_number)
            records = list(self._current_records or [])
            if not records:
                if self._declared_total_count and page_number <= declared_pages:
                    raise SafetyPause(f"淮安市官网列表第 {page_number} 页在声明范围内为空，已暂停")
                empty_streak += 1
                if empty_streak >= HUAIAN_MAX_EMPTY_PAGES:
                    break
                page_number += 1
                continue
            empty_streak = 0
            identities = [self._record_identity(record) for record in records]
            if len(set(identities)) != len(identities):
                raise SafetyPause(f"淮安市官网列表同一页出现重复记录：{self.target.label}")
            for index, record in enumerate(records):
                if page_number == start_page and index <= start_item_index:
                    self._seen_identities.add(identities[index])
                    continue
                identity = identities[index]
                if identity in self._seen_identities:
                    raise SafetyPause(
                        f"淮安市官网列表出现跨页重复记录，已暂停：{identity} @ page={page_number}"
                    )
                self._seen_identities.add(identity)
                yield self._item_from_record(record, page_number, index)
            if declared_pages and page_number >= declared_pages:
                break
            page_number += 1

    async def open_item(self, item: PolicyListItem) -> DetailInspection:
        if item.doc_flag == "external":
            # 外链记录：保留列表计数，不打开详情，不产生问题。
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
            detail = await self._parse_huaian_detail(detail_page, item.title)
            if detail.record is not None:
                await self._precheck_detail_links(detail_page, detail.record.related_links)
            return detail
        finally:
            await detail_page.close()

    async def open_detail_url(self, _district: str, url: str, fallback_title: str) -> DetailInspection:
        if not is_huaian_trusted_host(urlparse(url).netloc):
            return DetailInspection(record=None, header_detected=False)
        assert self.context
        detail_page = await self.context.new_page()
        try:
            response = await self.safe_goto(detail_page, url)
            await detail_page.wait_for_timeout(1000)
            if response and response.status >= 400:
                raise ItemReviewRequired(
                    f"详情页返回 HTTP {response.status}", "detail_load"
                )
            detail = await self._parse_huaian_detail(detail_page, fallback_title)
            if detail.record is not None:
                await self._precheck_detail_links(detail_page, detail.record.related_links)
            return detail
        finally:
            await detail_page.close()

    async def _parse_huaian_detail(self, page: Page, fallback_title: str) -> DetailInspection:
        text = await page.locator("body").inner_text()
        title_locator = page.locator("h1")
        title = (await title_locator.first.inner_text()).strip() if await title_locator.count() else fallback_title
        values = await extract_huaian_table_values(page)
        header_detected = any(label in values for label in HUAIAN_METADATA_LABELS)
        if not header_detected:
            return DetailInspection(record=None, header_detected=False)

        missing_fields = [label for label in HUAIAN_METADATA_LABELS if not values.get(label)]
        invalid_fields = []
        published = parse_huaian_date(values.get("公开日期"))
        if values.get("公开日期") and not published:
            invalid_fields.append("公开日期（格式无效）")
        body_locator = page.locator(".wz3")
        body_text = (
            await body_locator.first.inner_text()
            if await body_locator.count()
            else text
        )
        authored = extract_authored_date(body_text, values.get("发布机构") or "")
        links = await self._extract_huaian_detail_links(page, page.url)
        record = PolicyRecord(
            district="淮安市", title=title, url=page.url,
            source_id=clean_field(values.get("索引号")),
            source_site=self.target.label, issuing_agency=clean_field(values.get("发布机构")),
            page_document_number=clean_field(values.get("文号")),
            published_date=published, authored_date=authored,
            body_text=body_text,
            body_document_numbers=extract_declared_document_numbers(body_text),
            related_links=links,
            topic_category=clean_field(values.get("主题分类")),
            header_detected=True, missing_metadata_fields=[*missing_fields, *invalid_fields],
        )
        return DetailInspection(
            record=record, header_detected=True, missing_fields=missing_fields, invalid_fields=invalid_fields,
        )

    async def _extract_huaian_detail_links(self, page: Page, url: str) -> list[RelatedLink]:
        links: list[RelatedLink] = []
        body = page.locator(".wz3")
        if await body.count():
            for related in await self._extract_visible_links_from_locator(body.first, url, "详情页正文"):
                if related.kind:
                    links.append(related)
        interpret = page.locator("#podecipDiv")
        if await interpret.count():
            for related in await self._extract_visible_links_from_locator(interpret.first, url, "详情页侧栏"):
                if related.kind:
                    links.append(related)
        return links
