from __future__ import annotations

import re
from datetime import date
from urllib import robotparser
from urllib.parse import urljoin, urlparse

from playwright.async_api import Browser, Page

from app.collector import BrowserCollector, extract_labeled_value, is_attachment_url, parse_iso_date
from app.config import PUTUO_DISTRICT_URL
from app.domain import PolicyListItem, PolicyRecord, RelatedLink, SafetyPause
from app.rules import extract_authored_date, extract_document_numbers
from app.safety import SafetyController


PUTUO_API_URL = "https://www.shpt.gov.cn/front/api/data/affair"
PUTUO_CHANNEL_ID = "3"
PUTUO_PAGE_SIZE = 15
CHINESE_DATE_RE = re.compile(r"((?:19|20)\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")


def parse_putuo_date(value: str) -> date | None:
    parsed = parse_iso_date(value)
    if parsed:
        return parsed
    match = CHINESE_DATE_RE.search(value or "")
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def putuo_list_item_from_api(record: dict, page_number: int, item_index: int) -> PolicyListItem:
    title = str(record.get("title") or record.get("name") or "").strip()
    raw_url = str(
        record.get("url") or record.get("link") or record.get("linkUrl") or record.get("website") or ""
    ).strip()
    if not title or not raw_url:
        raise SafetyPause("普陀区官网列表接口缺少标题或详情地址，采集器需要更新")
    url = urljoin(PUTUO_DISTRICT_URL, raw_url)
    if urlparse(url).netloc.lower() != "www.shpt.gov.cn":
        raise SafetyPause(f"普陀区官网列表返回了异常详情域名：{url}")
    return PolicyListItem(
        district="普陀区",
        page_number=page_number,
        item_index=item_index,
        title=title,
        url=url,
        published_date=parse_putuo_date(str(record.get("display_date") or record.get("displayDate") or "")),
        source_site="区级网站·普陀区",
    )


class PutuoDistrictCollector(BrowserCollector):
    """普陀区官网区政府文件采集器：列表走公开接口，详情仍使用真实浏览器。"""

    def __init__(self, browser: Browser, safety: SafetyController):
        super().__init__(browser, safety)
        self._total_pages = 0
        self._current_page = 1

    @property
    def rendered_hosts(self) -> set[str]:
        return {"www.shpt.gov.cn"}

    async def check_robots(self) -> None:
        assert self.page
        await self.safe_goto(self.page, "https://www.shpt.gov.cn/robots.txt")
        text = await self.page.locator("body").inner_text()
        parser = robotparser.RobotFileParser("https://www.shpt.gov.cn/robots.txt")
        parser.parse(text.splitlines())
        self._robots = parser
        self.ensure_allowed(PUTUO_DISTRICT_URL)

    def ensure_allowed(self, url: str) -> None:
        if self._robots and urlparse(url).netloc.lower() == "www.shpt.gov.cn":
            if not self._robots.can_fetch("*", url):
                raise SafetyPause(f"robots.txt 禁止自动访问目标路径：{urlparse(url).path}")

    async def _fetch_page(self, page_number: int) -> list[dict]:
        assert self.context
        self.ensure_allowed(PUTUO_API_URL)
        await self.safety.before_request()
        response = None
        try:
            response = await self.context.request.post(
                PUTUO_API_URL,
                data={
                    "channelList": [PUTUO_CHANNEL_ID],
                    "pageSize": PUTUO_PAGE_SIZE,
                    "orderFields": ["display_date"],
                    "orderTypes": ["desc"],
                    "pageNum": page_number,
                },
                timeout=45_000,
                fail_on_status_code=False,
            )
            status = response.status
            text = await response.text()
            self.safety.after_request(status, text[:3000])
            if status >= 400:
                raise SafetyPause(f"普陀区官网列表接口返回 HTTP {status}")
            payload = await response.json()
            data = payload.get("data", payload)
            records = data.get("list") if isinstance(data, dict) else None
            total_pages = data.get("totalPage") if isinstance(data, dict) else None
            if not isinstance(records, list) or not total_pages:
                raise SafetyPause("普陀区官网列表接口数据结构发生变化，采集器需要更新")
            self._total_pages = int(total_pages)
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
        assert self.page
        await self.safe_goto(self.page, PUTUO_DISTRICT_URL)
        records = await self._fetch_page(1)
        if not records:
            raise SafetyPause("普陀区官网区政府文件列表为空，已停止扫描")
        # 接口仅公开页数。读取最后一页可得精确总数，随后恢复第一页供扫描使用。
        if self._total_pages > 1:
            last_records = await self._fetch_page(self._total_pages)
            self._estimated_total = (self._total_pages - 1) * PUTUO_PAGE_SIZE + len(last_records)
            await self._fetch_page(1)
        else:
            self._estimated_total = len(records)

    async def iter_items(self, district: str, start_page: int = 1, start_item_index: int = -1):
        await self.select_district(district)
        for page_number in range(start_page, self._total_pages + 1):
            if page_number != self._current_page:
                await self._fetch_page(page_number)
            for index, api_record in enumerate(self._current_records):
                if page_number == start_page and index <= start_item_index:
                    continue
                yield putuo_list_item_from_api(api_record, page_number, index)

    async def open_item(self, item: PolicyListItem) -> PolicyRecord:
        assert self.context
        detail_page = await self.context.new_page()
        try:
            await self.safe_goto(detail_page, item.url)
            await detail_page.wait_for_timeout(1000)
            return await self._parse_putuo_detail(detail_page, item.title)
        finally:
            await detail_page.close()

    async def _parse_putuo_detail(self, page: Page, fallback_title: str) -> PolicyRecord:
        text = await page.locator("body").inner_text()
        title_locator = page.locator("h1")
        title = (await title_locator.first.inner_text()).strip() if await title_locator.count() else fallback_title
        agency = (
            extract_labeled_value(text, "公开主体")
            or extract_labeled_value(text, "发文机构")
            or extract_labeled_value(text, "发文单位")
        )
        published = parse_putuo_date(extract_labeled_value(text, "发布日期"))
        authored = parse_putuo_date(extract_labeled_value(text, "成文日期"))
        body_locator = page.locator("article, .article-content, .TRS_Editor, .content")
        body_text = await body_locator.first.inner_text() if await body_locator.count() else text
        if not authored:
            authored = extract_authored_date(body_text, agency)
        if not agency or not published:
            raise SafetyPause("普陀区官网详情页缺少公开主体或发布日期，已保守暂停")
        links: list[RelatedLink] = []
        anchors = await page.locator("a").evaluate_all(
            "els => els.map(a => ({text:(a.innerText||'').trim(), href:a.href})).filter(x => x.href)"
        )
        for link in anchors:
            label = link["text"]
            href = urljoin(page.url, link["href"])
            kind = ""
            if "政策解读" in label:
                kind = "政策解读"
            elif "阅办联动" in label:
                kind = "阅办联动"
            elif is_attachment_url(href):
                kind = "附件"
            if kind and href != page.url and all(existing.url != href for existing in links):
                links.append(RelatedLink(kind, href))
        source_match = re.search(r"/(\d+)\.html$", urlparse(page.url).path)
        return PolicyRecord(
            district="普陀区",
            title=title,
            url=page.url,
            source_id=extract_labeled_value(text, "索引号") or (source_match.group(1) if source_match else ""),
            source_site="区级网站·普陀区",
            issuing_agency=agency,
            page_document_number=extract_labeled_value(text, "发文字号"),
            published_date=published,
            authored_date=authored,
            body_text=body_text,
            body_document_numbers=extract_document_numbers(body_text),
            related_links=links,
        )
