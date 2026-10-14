from __future__ import annotations

import re
from datetime import date
from urllib import robotparser
from urllib.parse import urljoin, urlparse

from playwright.async_api import Browser, Page

from app.collector import BrowserCollector, parse_iso_date
from app.config import ORDOS_HOST, ScanTarget, YJHL_HOST
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


# 鄂尔多斯市与伊金霍洛旗同属宏科 was40「资源库」骨架：列表是静态 index_N.html 分页，
# 总页数与总条数只写在列表页的行内脚本里，页面上没有可点到底的完整分页控件。
COUNT_PAGE_RE = re.compile(r"var\s+countPage\s*=\s*[\"']?(\d+)")
COUNT_RECORD_RE = re.compile(r"var\s+countRecord\s*=\s*[\"']?(\d+)")
DETAIL_HREF_RE = re.compile(r"/t\d{8}_\d+\.html")
MAX_DECLARED_PAGES = 400

# 详情表头五项口径。三套模板的字段名不同（发文字号/文号、公开日期/发布日期、
# 发文机关/发布机构），归一后按同一组字段检查；两个站点都没有「公开属性」字段。
ORDOS_METADATA_LABELS = ("索引号", "文号", "成文日期", "发布机构", "发布日期")
ORDOS_LABEL_ALIASES = {
    "发文字号": "文号",
    "公开日期": "发布日期",
    "发文机关": "发布机构",
    "信息分类": "主题分类",
    "分类": "主题分类",
    "公文时效": "有效性",
    "时效性": "有效性",
}
# 详情正文与附件容器：政策文件/部门文件走 bg_sl 模板，规范性文件库走 ql_detailbro 模板，
# 伊金霍洛旗走 tx_content 模板。规章与地方性法规详情页没有这套元数据，按无表头 PASS 处理。
ORDOS_BODY_SELECTORS = "div.content_zhwy_2022, .ql_detailbro_content, div.mainContainer, #Content"
ORDOS_LINK_CONTAINERS = ".zcxz, .ba_ul, .fjxz, .ql_detailbro_content, div.content_zhwy_2022, #Content"

_INVISIBLE_RE = re.compile(r"[\s　​‌‍⁠﻿]+")
_LABEL_TAIL_RE = re.compile(r"[:：]+$")
_CN_DATE_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_TAG_RE = re.compile(r"<[^>]+>")
_ROW_RE = re.compile(r"<(?:tr|li)[^>]*>(.*?)</(?:tr|li)>", re.S)
_CELL_RE = re.compile(r"<td(?P<attrs>[^>]*)>(?P<body>.*?)</td>", re.S)
_TITLE_ANCHOR_RE = re.compile(
    r'<a[^>]*href="(?P<href>[^"]*?/t\d{8}_\d+\.html)"[^>]*>(?P<title>.*?)</a>', re.S
)


def normalize_label(value: str) -> str:
    """清理标签中的普通/全角空格、不可见字符与结尾冒号（伊金霍洛旗写作「索引号:」）。"""
    return _LABEL_TAIL_RE.sub("", _INVISIBLE_RE.sub("", value or ""))


def canonical_label(value: str) -> str:
    return ORDOS_LABEL_ALIASES.get(normalize_label(value), normalize_label(value))


def parse_ordos_date(value: str) -> date | None:
    """兼容官网两种日期写法：ISO「2026-08-24」与中文「2026年08月24日」。"""
    text = (value or "").strip()
    if not text:
        return None
    cn = _CN_DATE_RE.search(text)
    if cn:
        try:
            return date(int(cn.group(1)), int(cn.group(2)), int(cn.group(3)))
        except ValueError:
            return None
    return parse_iso_date(text[:10])


def clean_text(value: str) -> str:
    return _INVISIBLE_RE.sub(" ", _TAG_RE.sub("", value or "")).strip()


def cell_text(value: str) -> str:
    """列表单元格里混着换行与移动端重复块，压成单行再比较。"""
    return re.sub(r"\s+", " ", _TAG_RE.sub("", value or "")).strip()


def declared_pagination(html: str) -> tuple[int | None, int | None]:
    """读取列表页行内脚本声明的总页数与总条数。

    规章栏目同时存在 PC 与移动端两段声明，取第一段（PC），与浏览器实际渲染一致。
    """
    pages = COUNT_PAGE_RE.search(html)
    records = COUNT_RECORD_RE.search(html)
    return (
        int(pages.group(1)) if pages else None,
        int(records.group(1)) if records else None,
    )


def page_url(list_url: str, page_number: int) -> str:
    if page_number <= 1:
        return list_url
    return f"{list_url}index_{page_number - 1}.html"


def _item(
    target: ScanTarget, page_number: int, index: int, title: str, href: str,
    published_text: str,
) -> PolicyListItem | None:
    title = clean_text(title)
    if not title or not href:
        return None
    url = urljoin(target.list_url, href)
    if not DETAIL_HREF_RE.search(urlparse(url).path):
        return None
    return PolicyListItem(
        district=target.district,
        page_number=page_number,
        item_index=index,
        title=title,
        url=url,
        published_date=parse_ordos_date(cell_text(published_text)),
        source_site=target.label,
        source_key=target.key,
        source_channel_id=target.channel_id,
        stable_id=f"{target.key}|url:{url}",
    )


def parse_ordos_list_page(html: str, page_number: int, target: ScanTarget) -> list[PolicyListItem]:
    """按栏目声明的列表布局解析条目；未知布局直接判为采集器需要更新。"""
    layout = target.list_layout
    if layout == "table1":
        rows = _parse_table1_rows(html)
    elif layout == "rightlist":
        rows = _parse_rightlist_rows(html)
    elif layout == "ruleslist":
        rows = _parse_ruleslist_rows(html)
    elif layout == "zyk_table":
        rows = _parse_zyk_rows(html)
    else:
        raise SafetyPause(f"{target.label} 未配置列表布局，采集器需要更新")
    items: list[PolicyListItem] = []
    for title, href, published in rows:
        item = _item(target, page_number, len(items), title, href, published)
        if item is not None:
            items.append(item)
    return items


def _parse_table1_rows(html: str) -> list[tuple[str, str, str]]:
    """`table#table1`：序号 | 标题 | 发文字号 | 成文日期 | 发布日期，另有一格移动端重复内容。"""
    rows: list[tuple[str, str, str]] = []
    for row in _ROW_RE.findall(html):
        anchor = _TITLE_ANCHOR_RE.search(row)
        if not anchor:
            continue
        cells = [match for match in _CELL_RE.finditer(row) if "mobile" not in (match.group("attrs") or "")]
        published = cell_text(cells[-1].group("body")) if len(cells) >= 3 else ""
        rows.append((anchor.group("title"), anchor.group("href"), published))
    return rows


def _parse_rightlist_rows(html: str) -> list[tuple[str, str, str]]:
    """`.rightNum/.rightTitle/.rightTime` 列表（部门文件子栏目、地方性法规）。"""
    rows: list[tuple[str, str, str]] = []
    for block in _ROW_RE.findall(html):
        if "rightTitle" not in block:
            continue
        anchor = _TITLE_ANCHOR_RE.search(block)
        if not anchor:
            continue
        time = re.search(r"rightTime.*?([\d]{4}-[\d]{2}-[\d]{2})", block, re.S)
        rows.append((anchor.group("title"), anchor.group("href"), time.group(1) if time else ""))
    return rows


def _parse_ruleslist_rows(html: str) -> list[tuple[str, str, str]]:
    """规章栏目 `div.rules-list`：只有标题与下载入口，列表不提供日期。"""
    rows: list[tuple[str, str, str]] = []
    for block in re.findall(r'<div class="title l">(?P<body>.*?)</div>', html, re.S):
        anchor = _TITLE_ANCHOR_RE.search(block)
        if anchor:
            rows.append((anchor.group("title"), anchor.group("href"), ""))
    return rows


def _parse_zyk_rows(html: str) -> list[tuple[str, str, str]]:
    """伊金霍洛旗 `table.tab-xxgk`：索引号 | 标题 | 文号 | 成文日期。"""
    rows: list[tuple[str, str, str]] = []
    for row in _ROW_RE.findall(html):
        if 'class="btou"' in row:
            continue
        anchor = _TITLE_ANCHOR_RE.search(row)
        if not anchor:
            continue
        cells = [cell_text(match.group("body")) for match in _CELL_RE.finditer(row)]
        published = cells[-1] if cells else ""
        rows.append((anchor.group("title"), anchor.group("href"), published))
    return rows


def pair_values(values: list[str]) -> dict[str, str]:
    """把「标签、值」交替排列的单元格折叠成字典。"""
    pairs: dict[str, str] = {}
    for index in range(0, len(values) - 1, 2):
        label = canonical_label(values[index])
        value = values[index + 1].strip()
        if label and label not in pairs:
            pairs[label] = value
    return pairs


def ordos_related_title(text: str) -> str:
    return _INVISIBLE_RE.sub("", text or "")


def header_fields(values: dict[str, str]) -> tuple[list[str], list[str]]:
    """按五项口径返回缺失字段与格式无效字段（META-001 的两类来源）。"""
    missing = [label for label in ORDOS_METADATA_LABELS if not values.get(label)]
    invalid: list[str] = []
    for label in ("成文日期", "发布日期"):
        raw = values.get(label)
        if raw and not parse_ordos_date(raw):
            invalid.append(f"{label}（格式无效）")
    return missing, invalid


class OrdosZykCollector(BrowserCollector):
    """鄂尔多斯市/伊金霍洛旗资源库采集器：静态分页列表 + 三套详情表头模板。"""

    def __init__(self, browser: Browser, safety: SafetyController, target: ScanTarget):
        super().__init__(browser, safety)
        self.target = target
        self.list_url = target.list_url
        self.host = (target.host or ORDOS_HOST).lower()
        self.site_label = "伊金霍洛旗" if self.host == YJHL_HOST else "鄂尔多斯市"
        self._declared_pages: int | None = None
        self._declared_records: int | None = None
        self._current_page = 1
        self._current_items: list[PolicyListItem] = []
        self._pages: dict[int, list[PolicyListItem]] = {}
        self._seen_identities: set[str] = set()
        self._enumerated = 0
        self._exhausted = False
        self._discovered = False
        self._walked_from_first = False
        self._reached_declared_end = False
        self._page_link_counts: dict[int, tuple[int, int]] = {}
        self.total_discrepancy = ""

    @property
    def rendered_hosts(self) -> set[str]:
        return {self.host}

    async def check_robots(self) -> None:
        assert self.page
        robots_url = f"https://{self.host}/robots.txt"
        response = await self.safe_goto(self.page, robots_url)
        content_type = ((await response.header_value("content-type")) if response else "") or ""
        if not response or response.status != 200 or "text/plain" not in content_type.lower():
            # 两站 robots.txt 均返回 404 网页：没有可判定的规则，不因此阻断扫描。
            self._robots = None
            return
        text = await self.page.locator("body").inner_text()
        parser = robotparser.RobotFileParser(robots_url)
        parser.parse(text.splitlines())
        self._robots = parser
        self.ensure_allowed(self.list_url)

    def ensure_allowed(self, url: str) -> None:
        if self._robots and urlparse(url).netloc.lower() == self.host:
            if not self._robots.can_fetch("*", url):
                raise SafetyPause(f"robots.txt 禁止自动访问目标路径：{urlparse(url).path}")

    async def _fetch_page(self, page_number: int, *, attempt: int = 0) -> list[PolicyListItem]:
        assert self.context
        page_target = page_url(self.list_url, page_number)
        self.ensure_allowed(page_target)
        await self.safety.before_request()
        response = None
        try:
            response = await self.context.request.get(page_target, timeout=45_000, fail_on_status_code=False)
            status = response.status
            text = await response.text()
            self.safety.after_request(status, text[:3000])
            if status == 404 and page_number > 1:
                # 站点没有声明页数时，第一个 404 页就是终点。
                self._current_page = page_number
                self._current_items = []
                return []
            if status >= 400:
                if status in {502, 503, 504} and attempt == 0:
                    # 政务站网关偶发 5xx：先重取一次，别为一次抖动进 30 分钟冷却。
                    await self.safety.retry_wait(1)
                    return await self._fetch_page(page_number, attempt=1)
                raise SafetyPause(f"{self.site_label} 列表页返回 HTTP {status}：{page_target}")
            pages, records = declared_pagination(text)
            if pages is not None:
                self._declared_pages = pages
            if records is not None:
                self._declared_records = records
            if self._declared_pages is not None and page_number > self._declared_pages:
                # 声明的页数就是终点，越界只说明分页走完了，不是采集器故障，不能停整批。
                self._current_page = page_number
                self._current_items = []
                return []
            items = parse_ordos_list_page(text, page_number, self.target)
            if not items:
                if page_number == 1:
                    raise SafetyPause(
                        f"{self.site_label}「{self.target.source_name}」列表首页未解析到条目，采集器需要更新"
                    )
                if attempt == 0:
                    # 瞬态风控页或半截响应会让整页解析为空；重取一次再判，别为一张假页面停掉整批。
                    await self.safety.retry_wait(1)
                    return await self._fetch_page(page_number, attempt=1)
                raise SafetyPause(
                    f"{self.site_label}「{self.target.source_name}」第 {page_number} 页重取后仍未解析到条目，"
                    "采集器需要更新"
                )
            self._page_link_counts[page_number] = (
                len(items), len(set(DETAIL_HREF_RE.findall(text))),
            )
            self._current_page = page_number
            self._current_items = items
            self._pages[page_number] = items
            return items
        except SafetyPause:
            raise
        except Exception as exc:
            self.safety.after_request(None, error=exc)
            raise
        finally:
            if response:
                await response.dispose()

    async def _discover_all_pages(self) -> None:
        """列表页没声明总条数时，先把分页读完，用真实唯一条目数作为总量。"""
        if self._discovered:
            return
        total_pages = min(self._declared_pages or 1, MAX_DECLARED_PAGES)
        for number in range(2, total_pages + 1):
            if not await self._fetch_page(number):
                break
        self._discovered = True

    async def select_district(self, _district: str) -> None:
        await self._fetch_page(1)
        if self._declared_records is None:
            await self._discover_all_pages()

    async def estimated_total(self) -> int:
        declared = int(self._declared_records or 0)
        actual = len({item.stable_id for rows in self._pages.values() for item in rows})
        if declared and actual < declared and self._pagination_is_complete():
            self.total_discrepancy = (
                f"{self.target.label} 页面标称 {declared} 条，走完其声明的 {self._declared_pages} 页"
                f"实际只发布 {actual} 条"
            )
            return actual
        if declared:
            return declared
        if self._discovered:
            return sum(len(rows) for rows in self._pages.values())
        if self._exhausted:
            return self._enumerated
        return 0

    def _pagination_is_complete(self) -> bool:
        # 三条同时成立才允许用实测条数替代站点标称：本轮从第 1 页走到站点自己声明的末页，
        # 且每页解析出的条数都等于该页真实存在的详情链接数（没有漏解析）。
        # 缺任何一条都退回标称数，让覆盖校验继续拦着可能的漏扫。
        if not (self._walked_from_first and self._reached_declared_end and self._pages):
            return False
        return all(parsed == raw for parsed, raw in self._page_link_counts.values())

    async def iter_items(self, district: str, start_page: int = 1, start_item_index: int = -1):
        self._seen_identities = set()
        self._enumerated = 0
        self._exhausted = False
        self._walked_from_first = int(start_page) <= 1
        self._reached_declared_end = False
        page_number = max(1, int(start_page))
        while True:
            if page_number == self._current_page:
                items = list(self._current_items)
            else:
                cached = self._pages.get(page_number)
                items = list(cached) if cached is not None else list(await self._fetch_page(page_number))
            if not items:
                break
            identities = [item.stable_id for item in items]
            if len(set(identities)) != len(identities):
                raise SafetyPause(f"{self.site_label} 列表同一页出现重复记录，已暂停")
            for item in items:
                if page_number == start_page and item.item_index <= start_item_index:
                    continue
                if item.stable_id in self._seen_identities:
                    raise SafetyPause(f"{self.site_label} 列表出现跨页重复记录：{item.stable_id}")
                self._seen_identities.add(item.stable_id)
                self._enumerated += 1
                yield item
            if self._declared_pages is None:
                break
            if page_number >= self._declared_pages:
                self._reached_declared_end = True
                break
            page_number += 1
        self._exhausted = True

    async def open_item(self, item: PolicyListItem) -> DetailInspection:
        assert self.context
        detail_page = await self.context.new_page()
        try:
            response = await self.safe_goto(detail_page, item.url)
            await detail_page.wait_for_timeout(1200)
            if response and response.status >= 400:
                raise ItemReviewRequired(f"详情页返回 HTTP {response.status}", "detail_load")
            detail = await self._parse_detail(detail_page, item.title)
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
            await detail_page.wait_for_timeout(1200)
            if response and response.status >= 400:
                raise ItemReviewRequired(f"详情页返回 HTTP {response.status}", "detail_load")
            detail = await self._parse_detail(detail_page, fallback_title)
            if detail.record is not None:
                await self._precheck_detail_links(detail_page, detail.record.related_links)
            return detail
        finally:
            await detail_page.close()

    async def _detail_values(self, page: Page) -> tuple[dict[str, str], bool]:
        """按栏目模板读取详情表头，返回归一字段与「是否存在表头容器」。"""
        layout = self.target.detail_layout
        if layout == "bg_sl":
            cells = await page.locator("ul.bg_sl li").evaluate_all(
                "els => els.map(el => (el.innerText || '').trim())"
            )
            return pair_values([str(cell) for cell in cells]), bool(cells)
        if layout == "gfxwj_table":
            rows = page.locator("table.ql_detailbro_table tr")
            if not await rows.count():
                return {}, False
            grouped = await rows.evaluate_all(
                "trs => trs.map(tr => Array.from(tr.querySelectorAll('td')).map(td => (td.innerText || '').trim()))"
            )
            cells = [str(value) for row in grouped for value in row]
            values = pair_values(cells)
            published = await self._text_of(page, ".ql_detailbro_date_source")
            match = re.search(r"发布日期[:：]?\s*([\d-]{10})", published or "")
            if match:
                values.setdefault("发布日期", match.group(1))
            return values, bool(cells)
        if layout == "tx_content":
            pairs = await page.locator("td.tx-contenth").evaluate_all(
                """els => els.map(el => {
                    const value = el.nextElementSibling;
                    return {
                        label: (el.innerText || '').trim(),
                        value: value ? (value.innerText || '').trim() : '',
                    };
                })"""
            )
            values: dict[str, str] = {}
            for pair in pairs:
                label = canonical_label(str(pair.get("label") or ""))
                if label and label not in values:
                    values[label] = str(pair.get("value") or "").strip()
            return values, bool(pairs)
        return {}, False

    async def _text_of(self, page: Page, selector: str) -> str:
        locator = page.locator(selector)
        if not await locator.count():
            return ""
        return (await locator.first.inner_text()).strip()

    async def _detail_title(self, page: Page, fallback_title: str) -> str:
        for selector in ("h1.ql_detailbro_title", "h1.contit", "div.bg_gs"):
            locator = page.locator(selector)
            if not await locator.count():
                continue
            text = (await locator.first.inner_text()).strip()
            if selector == "div.bg_gs":
                # bg_sl 模板的标题行是「标题 / 正文标题」两个 span，只取后者。
                spans = await locator.first.locator("span").evaluate_all(
                    "els => els.map(el => (el.innerText || '').trim())"
                )
                text = spans[1] if len(spans) > 1 else ""
            text = re.sub(r"\s+", " ", text)
            if text and text not in ("标题",):
                return text
        return clean_text(fallback_title)

    async def _parse_detail(self, page: Page, fallback_title: str) -> DetailInspection:
        text = await page.locator("body").inner_text()
        if any(marker in text for marker in ("页面不存在", "内容不存在", "访问出错", "系统错误", "您访问的页面")):
            raise ItemReviewRequired("详情页不存在或已失效", "detail_load")
        values, container_present = await self._detail_values(page)
        hits = sum(1 for label in ORDOS_METADATA_LABELS if values.get(label))
        # 只含个别字样的普通新闻页不能算政策表头；规章与地方性法规详情页本就无这套表头，按 PASS 处理。
        header_detected = container_present and hits >= 2
        if not header_detected:
            return DetailInspection(record=None, header_detected=False)

        missing_fields, invalid_fields = header_fields(values)
        authored = parse_ordos_date(values.get("成文日期", ""))
        published = parse_ordos_date(values.get("发布日期", ""))

        title = await self._detail_title(page, fallback_title)
        body_text = await self._text_of(page, ORDOS_BODY_SELECTORS) or text
        links = await self._extract_ordos_detail_links(page, page.url)
        record = PolicyRecord(
            district=self.target.district, title=title, url=page.url,
            source_id=values.get("索引号", ""),
            source_site=self.target.label, issuing_agency=values.get("发布机构", ""),
            page_document_number=values.get("文号", ""),
            published_date=published, authored_date=authored,
            body_text=body_text,
            body_document_numbers=extract_declared_document_numbers(body_text),
            related_links=links,
            topic_category=values.get("主题分类", ""),
            header_detected=True,
            missing_metadata_fields=[*missing_fields, *invalid_fields],
        )
        return DetailInspection(
            record=record, header_detected=True,
            missing_fields=missing_fields, invalid_fields=invalid_fields,
        )

    async def _extract_ordos_detail_links(self, page: Page, url: str) -> list[RelatedLink]:
        """附件由详情页 JS 渲染成 .zcxz / .ba_ul 里的可见链接，隐藏的 .ba 源锚点不计入。"""
        links: list[RelatedLink] = []
        container = page.locator(ORDOS_LINK_CONTAINERS)
        if not await container.count():
            return links
        for related in await self._extract_visible_links_from_locator(container, url, "详情页附件"):
            if related.kind and related.url not in {link.url for link in links}:
                links.append(related)
        return links
