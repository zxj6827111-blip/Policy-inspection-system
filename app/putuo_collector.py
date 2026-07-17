from __future__ import annotations

import re
from datetime import date
from math import ceil
from urllib import robotparser
from urllib.parse import urljoin, urlparse

from playwright.async_api import Browser, Page

from app.collector import BrowserCollector, parse_iso_date
from app.config import PUTUO_DISTRICT_URL, ScanTarget
from app.domain import DetailInspection, PolicyListItem, PolicyRecord, RelatedLink, SafetyPause
from app.rules import extract_document_numbers
from app.safety import SafetyController


PUTUO_API_URL = "https://www.shpt.gov.cn/front/api/data/affair"
PUTUO_PAGE_SIZE = 15
# 官网列表接口在部分大栏目上会把 totalCount/totalPage 截断到该上限。
PUTUO_API_TOTAL_CAP = 10_000
# 截断后最多再向后探测的额外页数，防止无限循环。
PUTUO_CAPPED_MAX_EXTRA_PAGES = 2_000
# 连续空页达到该次数后视为真实终点。
PUTUO_CAPPED_MAX_EMPTY_PAGES = 1
PUTUO_QUERY_CONTRACT = "putuo-docflag-capped-v2"
METADATA_LABELS = (
    "索引号", "主题分类", "公开属性", "成文日期", "发文字号", "发布日期", "公开主体",
)
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


def extract_putuo_header_value(text: str, label: str) -> str:
    """普陀详情页的字段按行读取，避免空值误吞下一行表头。"""
    pattern = re.compile(rf"(?m)^[ \t]*{re.escape(label)}[ \t]*[:：][ \t]*(.*)$")
    match = pattern.search(text)
    return match.group(1).strip() if match else ""


def putuo_record_identity(record: dict, *, list_url: str = PUTUO_DISTRICT_URL) -> str:
    """稳定记录身份：优先 id，其次规范化 URL。"""
    record_id = record.get("id")
    if record_id is not None and str(record_id).strip() != "":
        return f"id:{record_id}"
    raw_url = str(
        record.get("url") or record.get("link") or record.get("linkUrl") or record.get("website") or ""
    ).strip()
    if not raw_url:
        raise SafetyPause("普陀区官网列表记录缺少稳定身份（id/url），采集器需要更新")
    return f"url:{urljoin(list_url, raw_url)}"


def putuo_list_item_from_api(
    record: dict, page_number: int, item_index: int, *, source_site: str = "区级网站·普陀区",
    source_key: str = "putuo_government", source_channel_id: str = "3", list_url: str = PUTUO_DISTRICT_URL,
) -> PolicyListItem:
    title = str(record.get("title") or record.get("name") or "").strip()
    raw_url = str(
        record.get("url") or record.get("link") or record.get("linkUrl") or record.get("website") or ""
    ).strip()
    if not title or not raw_url:
        raise SafetyPause("普陀区官网列表接口缺少标题或详情地址，采集器需要更新")
    url = urljoin(list_url, raw_url)
    if urlparse(url).netloc.lower() != "www.shpt.gov.cn":
        raise SafetyPause(f"普陀区官网列表返回了异常详情域名：{url}")
    return PolicyListItem(
        district="普陀区",
        page_number=page_number,
        item_index=item_index,
        title=title,
        url=url,
        published_date=parse_putuo_date(str(record.get("display_date") or record.get("displayDate") or "")),
        source_site=source_site,
        source_key=source_key,
        source_channel_id=source_channel_id,
    )


def _record_doc_flag(record: dict) -> str:
    raw_flag = record.get("doc_flag")
    if raw_flag is None:
        raw_flag = record.get("docFlag")
    return "" if raw_flag is None else str(raw_flag)


class PutuoDistrictCollector(BrowserCollector):
    """普陀区官网五个政府文件栏目共用的安全采集器。"""

    def __init__(
        self,
        browser: Browser,
        safety: SafetyController,
        target: ScanTarget,
        *,
        api_total_cap: int = PUTUO_API_TOTAL_CAP,
        page_size: int = PUTUO_PAGE_SIZE,
        capped_max_extra_pages: int = PUTUO_CAPPED_MAX_EXTRA_PAGES,
    ):
        super().__init__(browser, safety)
        self.target = target
        self.list_url = target.list_url or PUTUO_DISTRICT_URL
        self.channel_id = target.channel_id or "3"
        self.api_total_cap = int(api_total_cap)
        self.page_size = int(page_size)
        self.capped_max_extra_pages = int(capped_max_extra_pages)
        self._total_pages = 0
        self._total_count: int | None = None
        self._declared_total_pages = 0
        self._declared_total_count: int | None = None
        self._current_page = 1
        self._capped_mode = False
        self._capped_resolved = False
        self._resume_examined = 0
        self._yielded_unique = 0
        self._seen_identities: set[str] = set()

    @property
    def rendered_hosts(self) -> set[str]:
        return {"www.shpt.gov.cn"}

    @property
    def capped_pagination_active(self) -> bool:
        return self._capped_mode

    @property
    def capped_pagination_resolved(self) -> bool:
        return self._capped_resolved

    def note_resume_examined(self, examined_count: int) -> None:
        """任务恢复时，把本源已覆盖条数记入最终真实总量。"""
        self._resume_examined = max(0, int(examined_count))

    async def check_robots(self) -> None:
        assert self.page
        await self.safe_goto(self.page, "https://www.shpt.gov.cn/robots.txt")
        text = await self.page.locator("body").inner_text()
        parser = robotparser.RobotFileParser("https://www.shpt.gov.cn/robots.txt")
        parser.parse(text.splitlines())
        self._robots = parser
        self.ensure_allowed(self.list_url)

    def ensure_allowed(self, url: str) -> None:
        if self._robots and urlparse(url).netloc.lower() == "www.shpt.gov.cn":
            if not self._robots.can_fetch("*", url):
                raise SafetyPause(f"robots.txt 禁止自动访问目标路径：{urlparse(url).path}")

    def _validate_doc_flags(self, records: list, page_number: int) -> None:
        for record in records:
            if not isinstance(record, dict):
                raise SafetyPause(
                    f"普陀区官网政策文件过滤失效，接口返回了非文件记录："
                    f"page_number={page_number}, id=?, channel_id={self.channel_id}, doc_flag=?"
                )
            record_doc_flag = _record_doc_flag(record)
            if record_doc_flag != "1":
                record_id = record.get("id", "?")
                channel = record.get("channel_id") or record.get("channelId") or self.channel_id
                raise SafetyPause(
                    f"普陀区官网政策文件过滤失效，接口返回了非文件记录："
                    f"page_number={page_number}, id={record_id}, channel_id={channel}, "
                    f"doc_flag={record_doc_flag!r}"
                )

    def _expected_rows(self, page_number: int, total_pages: int, total_count: int) -> int:
        if not total_count:
            return 0
        if page_number < total_pages:
            return self.page_size
        return total_count - self.page_size * (total_pages - 1)

    def _maybe_enter_capped_mode(
        self,
        *,
        page_number: int,
        total_count: int,
        total_pages: int,
        records: list,
    ) -> None:
        if self._capped_mode:
            return
        if total_count != self.api_total_cap:
            return
        expected_rows = self._expected_rows(page_number, total_pages, total_count)
        last_page_overflow = (
            page_number == total_pages
            and expected_rows < self.page_size
            and len(records) > expected_rows
            and len(records) <= self.page_size
        )
        beyond_declared = page_number > total_pages and len(records) > 0
        # 仅在强截断证据下进入：声明末页条数溢出，或超声明页仍返回数据。
        if last_page_overflow or beyond_declared:
            self._capped_mode = True

    async def _fetch_page(self, page_number: int) -> list[dict]:
        assert self.context
        self.ensure_allowed(PUTUO_API_URL)
        await self.safety.before_request()
        response = None
        try:
            response = await self.context.request.post(
                PUTUO_API_URL,
                data={
                    "channelList": [self.channel_id],
                    "pageSize": self.page_size,
                    "orderFields": ["display_date"],
                    "orderTypes": ["desc"],
                    "pageNo": page_number,
                    "docFlag": "1",
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
            if not isinstance(data, dict):
                raise SafetyPause("普陀区官网列表接口数据结构发生变化，采集器需要更新")
            records = data.get("list")
            try:
                response_page = int(data["pageNo"])
                total_pages = int(data["totalPage"])
                total_count = int(data["totalCount"])
            except (KeyError, TypeError, ValueError) as exc:
                raise SafetyPause("普陀区官网列表接口缺少有效的 pageNo/totalPage/totalCount") from exc
            if not isinstance(records, list) or total_count < 0 or total_pages < 0:
                raise SafetyPause("普陀区官网列表接口数据结构发生变化，采集器需要更新")
            self._validate_doc_flags(records, page_number)
            if self._declared_total_count is None and not (total_count == 0 and total_pages == 0):
                self._declared_total_count = total_count
                self._declared_total_pages = total_pages

            # 超远页：官网返回空元数据，视为没有更多数据。
            if not records and total_count == 0 and total_pages == 0 and response_page == 0:
                if self._capped_mode and page_number > (self._declared_total_pages or 0):
                    self._current_page = page_number
                    self._current_records = []
                    return []
                if page_number == 1:
                    self._total_count = 0
                    self._total_pages = 0
                    self._declared_total_count = 0
                    self._declared_total_pages = 0
                    self._estimated_total = 0
                    self._current_page = 1
                    self._current_records = []
                    return []
                raise SafetyPause(
                    f"普陀区官网列表接口返回了空元数据：请求第 {page_number} 页"
                )

            expected_pages = ceil(total_count / self.page_size) if total_count else 0
            if total_pages != expected_pages:
                raise SafetyPause(
                    f"普陀区官网列表接口总量校验失败：totalCount={total_count}，totalPage={total_pages}"
                )

            # 先根据本页证据决定是否进入截断模式，再做宽松/严格校验。
            self._maybe_enter_capped_mode(
                page_number=page_number,
                total_count=total_count,
                total_pages=total_pages,
                records=records,
            )

            if self._capped_mode:
                records = self._validate_capped_page(
                    page_number=page_number,
                    response_page=response_page,
                    total_pages=total_pages,
                    total_count=total_count,
                    records=records,
                )
            else:
                records = self._validate_strict_page(
                    page_number=page_number,
                    response_page=response_page,
                    total_pages=total_pages,
                    total_count=total_count,
                    expected_pages=expected_pages,
                    records=records,
                )

            if not self._capped_mode and (
                total_count != self._declared_total_count or total_pages != self._declared_total_pages
            ):
                raise SafetyPause("普陀区官网列表总量在扫描期间发生变化，已暂停以避免生成不完整结果")
            if self._capped_mode and total_count not in {self.api_total_cap, 0}:
                if total_count != self._declared_total_count:
                    raise SafetyPause(
                        f"普陀区官网列表截断元数据在扫描期间发生变化：{self._declared_total_count} -> {total_count}"
                    )

            if not self._capped_mode:
                self._total_count = total_count
                self._total_pages = total_pages
                self._estimated_total = total_count
            else:
                # 截断期间不把 10000 当作真实终点；真实总量在 iter_items 结束后写回。
                self._total_count = self._declared_total_count
                self._total_pages = self._declared_total_pages
                discovered = self._resume_examined + self._yielded_unique
                self._estimated_total = max(self.api_total_cap, discovered)
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

    def _validate_strict_page(
        self,
        *,
        page_number: int,
        response_page: int,
        total_pages: int,
        total_count: int,
        expected_pages: int,
        records: list,
    ) -> list[dict]:
        if response_page != page_number:
            raise SafetyPause(
                f"普陀区官网列表接口页码校验失败：请求第 {page_number} 页，返回第 {response_page} 页"
            )
        if total_pages != expected_pages:
            raise SafetyPause(
                f"普陀区官网列表接口总量校验失败：totalCount={total_count}，totalPage={total_pages}"
            )
        if total_count and not 1 <= page_number <= total_pages:
            raise SafetyPause(f"普陀区官网列表接口返回了无效页码：{page_number}/{total_pages}")
        expected_rows = self._expected_rows(page_number, total_pages, total_count)
        if len(records) != expected_rows:
            # 命中 CAP 且末页条数大于推导值：转入截断模式而不是立即失败。
            if (
                total_count == self.api_total_cap
                and page_number == total_pages
                and len(records) > expected_rows
                and len(records) <= self.page_size
            ):
                self._capped_mode = True
                return records
            raise SafetyPause(
                f"普陀区官网列表第 {page_number} 页条数异常：应为 {expected_rows} 条，实际 {len(records)} 条"
            )
        return records

    def _validate_capped_page(
        self,
        *,
        page_number: int,
        response_page: int,
        total_pages: int,
        total_count: int,
        records: list,
    ) -> list[dict]:
        declared_pages = self._declared_total_pages or total_pages
        if page_number <= declared_pages:
            if response_page != page_number:
                raise SafetyPause(
                    f"普陀区官网列表接口页码校验失败：请求第 {page_number} 页，返回第 {response_page} 页"
                )
            if page_number < declared_pages and len(records) != self.page_size:
                raise SafetyPause(
                    f"普陀区官网列表第 {page_number} 页条数异常：应为 {self.page_size} 条，实际 {len(records)} 条"
                )
            if page_number == declared_pages:
                expected_rows = self._expected_rows(page_number, declared_pages, total_count)
                # 截断末页：允许满页（15）而不是 residual（如 10）。
                if len(records) == 0:
                    raise SafetyPause(
                        f"普陀区官网列表第 {page_number} 页在截断模式下为空，无法确认数据连续性"
                    )
                if len(records) > self.page_size:
                    raise SafetyPause(
                        f"普陀区官网列表第 {page_number} 页条数异常：超过 pageSize={self.page_size}"
                    )
                if len(records) < expected_rows:
                    raise SafetyPause(
                        f"普陀区官网列表第 {page_number} 页条数异常：应为至少 {expected_rows} 条，实际 {len(records)} 条"
                    )
            return records

        # 请求页码已超过官网声明 totalPage。
        if not records:
            return []
        if response_page not in {declared_pages, page_number}:
            # 探测显示超页时常固定返回声明末页页码；其它值视为契约变化。
            raise SafetyPause(
                f"普陀区官网列表截断分页页码异常：请求第 {page_number} 页，返回第 {response_page} 页，"
                f"声明末页 {declared_pages}"
            )
        if len(records) > self.page_size:
            raise SafetyPause(
                f"普陀区官网列表第 {page_number} 页条数异常：超过 pageSize={self.page_size}"
            )
        return records

    async def select_district(self, _district: str) -> None:
        assert self.page
        await self.safe_goto(self.page, self.list_url)
        records = await self._fetch_page(1)
        if not records:
            raise SafetyPause("普陀区官网区政府文件列表为空，已停止扫描")

    def _page_identities(self, records: list[dict]) -> list[str]:
        identities = []
        page_seen: set[str] = set()
        for record in records:
            identity = putuo_record_identity(record, list_url=self.list_url)
            if identity in page_seen:
                raise SafetyPause(
                    f"普陀区官网列表同一页出现重复记录：{identity}"
                )
            page_seen.add(identity)
            identities.append(identity)
        return identities

    async def iter_items(self, district: str, start_page: int = 1, start_item_index: int = -1):
        await self.select_district(district)
        self._seen_identities = set()
        self._yielded_unique = 0
        self._capped_resolved = False

        declared_pages = self._declared_total_pages or self._total_pages
        declared_count = self._declared_total_count if self._declared_total_count is not None else self._total_count
        if not declared_pages and not declared_count:
            raise SafetyPause("普陀区官网区政府文件列表为空，已停止扫描")

        page_number = start_page
        empty_streak = 0
        reached_end = False

        while True:
            if (
                self._capped_mode
                and page_number > declared_pages
                and (page_number - declared_pages) > self.capped_max_extra_pages
            ):
                raise SafetyPause(
                    f"普陀区官网列表截断分页超过安全上限：已超过声明末页 "
                    f"{declared_pages} 共 {page_number - declared_pages} 页，channel_id={self.channel_id}"
                )

            if page_number != self._current_page:
                await self._fetch_page(page_number)

            records = list(self._current_records or [])
            if not records:
                if page_number <= declared_pages and not self._capped_mode:
                    raise SafetyPause(
                        f"普陀区官网列表第 {page_number} 页在声明范围内为空，已暂停"
                    )
                if self._capped_mode or page_number > declared_pages:
                    empty_streak += 1
                    if empty_streak >= PUTUO_CAPPED_MAX_EMPTY_PAGES:
                        reached_end = True
                        break
                    page_number += 1
                    continue
                raise SafetyPause(f"普陀区官网列表第 {page_number} 页为空，已暂停")

            empty_streak = 0
            identities = self._page_identities(records)
            new_identities = [identity for identity in identities if identity not in self._seen_identities]

            if page_number > declared_pages:
                # 超页仍有新数据：确认进入截断模式
                if not self._capped_mode:
                    if declared_count == self.api_total_cap and new_identities:
                        self._capped_mode = True
                    else:
                        raise SafetyPause(
                            f"普陀区官网列表返回了超出声明范围的数据：page={page_number}, "
                            f"declared={declared_pages}"
                        )
                if not new_identities:
                    raise SafetyPause(
                        f"普陀区官网列表截断分页在第 {page_number} 页重复返回已扫描记录，"
                        f"无法确认真实终点，channel_id={self.channel_id}"
                    )
                if len(new_identities) < len(identities):
                    raise SafetyPause(
                        f"普陀区官网列表截断分页在第 {page_number} 页出现部分重复记录，已暂停"
                    )

            for index, api_record in enumerate(records):
                if page_number == start_page and index <= start_item_index:
                    self._seen_identities.add(identities[index])
                    continue
                identity = identities[index]
                if identity in self._seen_identities:
                    raise SafetyPause(
                        f"普陀区官网列表出现跨页重复记录，已暂停：{identity} @ page={page_number}"
                    )
                self._seen_identities.add(identity)
                self._yielded_unique += 1
                if self._capped_mode:
                    self._estimated_total = max(
                        self._estimated_total,
                        self._resume_examined + self._yielded_unique,
                        self.api_total_cap,
                    )
                yield putuo_list_item_from_api(
                    api_record, page_number, index, source_site=self.target.label, source_key=self.target.key,
                    source_channel_id=self.channel_id, list_url=self.list_url,
                )

            # 声明末页处理：若已确认截断则继续向后；否则尝试探测下一页。
            if page_number == declared_pages and not self._capped_mode:
                if declared_count == self.api_total_cap:
                    next_page = page_number + 1
                    await self._fetch_page(next_page)
                    if self._current_records:
                        self._capped_mode = True
                        page_number = next_page
                        continue
                    # 下一页为空：以声明总量为真实终点（末页条数已在严格校验中对齐）
                    reached_end = True
                    break
                reached_end = True
                break

            if self._capped_mode and page_number >= declared_pages and 0 < len(records) < self.page_size:
                next_page = page_number + 1
                await self._fetch_page(next_page)
                if not self._current_records:
                    reached_end = True
                    break
                page_number = next_page
                continue

            if not self._capped_mode and page_number >= declared_pages:
                reached_end = True
                break

            page_number += 1

        if self._capped_mode:
            if not reached_end:
                raise SafetyPause(
                    f"普陀区官网列表截断分页未能确认真实终点，channel_id={self.channel_id}"
                )
            real_total = self._resume_examined + self._yielded_unique
            if real_total <= 0:
                raise SafetyPause("普陀区官网列表截断分页未发现任何可扫描记录")
            self._total_count = real_total
            self._estimated_total = real_total
            self._capped_resolved = True
        else:
            if self._resume_examined:
                self._estimated_total = self._resume_examined + self._yielded_unique
            else:
                self._estimated_total = self._total_count or self._yielded_unique

    async def open_item(self, item: PolicyListItem) -> DetailInspection:

        assert self.context
        detail_page = await self.context.new_page()
        try:
            await self.safe_goto(detail_page, item.url)
            await detail_page.wait_for_timeout(1000)
            detail = await self._parse_putuo_detail(detail_page, item.title)
            if detail.record is not None:
                await self._precheck_detail_links(detail_page, detail.record.related_links)
            return detail
        finally:
            await detail_page.close()

    async def open_detail_url(self, _district: str, url: str, fallback_title: str) -> DetailInspection:
        assert self.context
        detail_page = await self.context.new_page()
        try:
            await self.safe_goto(detail_page, url)
            await detail_page.wait_for_timeout(1000)
            detail = await self._parse_putuo_detail(detail_page, fallback_title)
            if detail.record is not None:
                await self._precheck_detail_links(detail_page, detail.record.related_links)
            return detail
        finally:
            await detail_page.close()

    async def _parse_putuo_detail(self, page: Page, fallback_title: str) -> DetailInspection:
        text = await page.locator("body").inner_text()
        title_locator = page.locator("h1")
        title = (await title_locator.first.inner_text()).strip() if await title_locator.count() else fallback_title
        values = {label: extract_putuo_header_value(text, label) for label in METADATA_LABELS}
        metadata_pairs = await page.locator(
            ".article-info .col-md-7, .article-info .col-md-5"
        ).evaluate_all(
            """els => els.map(el => {
                const values = Array.from(el.querySelectorAll('span'))
                    .map(span => (span.innerText || '').trim());
                return {label: values[0] || '', value: values.slice(1).join(' ').trim()};
            })"""
        )
        dom_header_detected = False
        for pair in metadata_pairs:
            label = re.sub(r"[\s:：]+$", "", str(pair.get("label") or "").strip())
            label = re.sub(r"\s+", "", label)
            if label not in values:
                continue
            dom_header_detected = True
            dom_value = str(pair.get("value") or "").strip()
            if dom_value:
                values[label] = dom_value
        header_detected = dom_header_detected or any(
            re.search(rf"{re.escape(label)}[ \t]*[:：]", text) for label in METADATA_LABELS
        )
        if not header_detected:
            return DetailInspection(record=None, header_detected=False)

        authored = parse_putuo_date(values["成文日期"])
        published = parse_putuo_date(values["发布日期"])
        missing_fields = [label for label in METADATA_LABELS if not values[label]]
        invalid_fields = []
        if values["成文日期"] and not authored:
            invalid_fields.append("成文日期（格式无效）")
        if values["发布日期"] and not published:
            invalid_fields.append("发布日期（格式无效）")
        body_locator = page.locator("article, .article-content, .TRS_Editor, .content")
        body_text = await body_locator.first.inner_text() if await body_locator.count() else text
        links = await self._extract_detail_links(page, page.url)
        record = PolicyRecord(
            district="普陀区", title=title, url=page.url,
            source_id=values["索引号"],
            source_site=self.target.label, issuing_agency=values["公开主体"],
            page_document_number=values["发文字号"], published_date=published, authored_date=authored,
            body_text=body_text, body_document_numbers=extract_document_numbers(body_text), related_links=links,
            topic_category=values["主题分类"], disclosure_attribute=values["公开属性"],
            header_detected=True, missing_metadata_fields=[*missing_fields, *invalid_fields],
        )
        return DetailInspection(
            record=record, header_detected=True, missing_fields=missing_fields, invalid_fields=invalid_fields,
        )
