from datetime import date, datetime, timedelta, timezone

import pytest

from app.config import SCAN_TARGETS
from app.domain import SafetyPause
from app.suqian_collector import (
    SuqianCollector,
    SuqianSzfwjCollector,
    parse_suqian_timestamp,
    parse_szfwj_list_page,
    suqian_identity,
    suqian_list_item,
)


def sample_record(**overrides):
    record = {
        "TITLE": "市政府办公室印发关于推动宿迁市高端纺织产业高质量发展实施意见的通知",
        "URL": "202607/a44a7268a12f4737bc5bf90f5c567b7f.shtml",
        "C_FBJG": "宿迁市人民政府办公室",
        "C_WJBH": "宿政办发〔2026〕34号",
        "C_SYH": "142334102/2026-00008",
        "C_FWRQ": 1784851020000,
        "PUBLISHED_TIME": 1785457032000,
        "MANUSCRIPT_METADATA_ID": "a44a7268a12f4737bc5bf90f5c567b7f",
        "CHANNEL_ID": "ea5a76d7d10b413e877769583af8ba3f",
    }
    record.update(overrides)
    return record


def test_suqian_timestamp_parser():
    cst = timezone(timedelta(hours=8))
    assert parse_suqian_timestamp(1785457032000) == datetime.fromtimestamp(1785457032, tz=cst).date()
    assert parse_suqian_timestamp("1785457032000") == datetime.fromtimestamp(1785457032, tz=cst).date()
    assert parse_suqian_timestamp(None) is None
    assert parse_suqian_timestamp("") is None
    assert parse_suqian_timestamp("0") is None
    assert parse_suqian_timestamp("abc") is None


def test_suqian_list_item_maps_fields():
    target = SCAN_TARGETS["suqian_zcwj"]
    item = suqian_list_item(sample_record(), 1, 5, target)
    assert item.district == "宿迁市"
    assert item.page_number == 1
    assert item.item_index == 5
    assert item.title == sample_record()["TITLE"]
    assert item.url == "https://www.suqian.gov.cn/zcwjk/zcwj/202607/a44a7268a12f4737bc5bf90f5c567b7f.shtml"
    assert item.source_key == "suqian_zcwj"
    assert item.stable_id == "suqian_zcwj|id:a44a7268a12f4737bc5bf90f5c567b7f"
    assert item.published_date is not None
    assert item.published_date.year == 2026


def test_suqian_list_item_maps_county_and_suyu_channels():
    target = SCAN_TARGETS["suqian_zcwj"]
    item = suqian_list_item(sample_record(CHANNEL_ID="4d79b50ea5684d4b8b63643a46ff6071"), 1, 0, target)
    assert item.url == "https://www.suqian.gov.cn/zcwjk/xqbmzc/202607/a44a7268a12f4737bc5bf90f5c567b7f.shtml"
    assert item.doc_flag == ""
    item = suqian_list_item(sample_record(CHANNEL_ID="c7b7132d5d1648d2b053fd3a8cab52bf"), 1, 0, target)
    assert item.url == "https://www.suqian.gov.cn/zcwjk/suyu/202607/a44a7268a12f4737bc5bf90f5c567b7f.shtml"
    assert item.doc_flag == ""


def test_suqian_list_item_marks_unknown_channel_as_external():
    target = SCAN_TARGETS["suqian_zcwj"]
    item = suqian_list_item(sample_record(CHANNEL_ID="unknown-channel-id"), 1, 0, target)
    assert item.doc_flag == "external"
    assert "/unresolved/" in item.url


def test_suqian_list_item_requires_title():
    target = SCAN_TARGETS["suqian_zcwj"]
    with pytest.raises(SafetyPause):
        suqian_list_item(sample_record(TITLE=""), 1, 0, target)


def test_suqian_identity_falls_back_to_url():
    target = SCAN_TARGETS["suqian_zcwj"]
    identity = suqian_identity(target.key, sample_record(MANUSCRIPT_METADATA_ID=""))
    assert identity == "suqian_zcwj|url:https://www.suqian.gov.cn/zcwjk/202607/a44a7268a12f4737bc5bf90f5c567b7f.shtml"


def test_suqian_target_config():
    target = SCAN_TARGETS["suqian_zcwj"]
    assert target.collector_type == "suqian"
    assert target.host == "www.suqian.gov.cn"
    assert target.district == "宿迁市"


class SuqianFakeLocator:
    def __init__(self, text="", count=0, items=None):
        self.text = text
        self._count = count
        self.items = items or []

    async def count(self):
        return self._count

    async def inner_text(self):
        return self.text

    async def evaluate_all(self, _script):
        return self.items

    @property
    def first(self):
        return self

    def locator(self, _selector):
        return self

    def filter(self, **_kwargs):
        return self

    def nth(self, _index):
        return self


class SuqianDetailPage:
    url = "https://www.suqian.gov.cn/zcwjk/zcwj/202607/a44a7268a12f4737bc5bf90f5c567b7f.shtml"

    def __init__(self, text="", zoomcon="", fujian_items=None, table_rows=None):
        self.text = text
        self.zoomcon = zoomcon
        self.fujian_items = fujian_items or []
        self.table_rows = table_rows

    def locator(self, selector):
        if selector == "body":
            return SuqianFakeLocator(self.text, count=1)
        if selector == "h1":
            return SuqianFakeLocator("宿迁市促进新型就业困难人员就业若干措施")
        if selector == "#ysj_mc":
            return SuqianFakeLocator("", count=0)
        if selector == ".table_ysj table tr":
            return SuqianFakeLocator(items=self.table_rows or [], count=1 if self.table_rows else 0)
        if selector == "#zoomcon, .article-content-body":
            return SuqianFakeLocator(self.zoomcon, count=1 if self.zoomcon else 0)
        if selector == "#fujian a, span.fujian a":
            return SuqianFakeLocator(items=self.fujian_items, count=1 if self.fujian_items else 0)
        if selector == "a":
            return SuqianFakeLocator(items=[])
        return SuqianFakeLocator("", count=0)


def table_row(*cells):
    out = []
    for index in range(0, len(cells) - 1, 2):
        out.append({"label": cells[index], "value": cells[index + 1]})
    return out


def sample_table_rows():
    return [
        table_row("发布机构", "宿迁市人民政府办公室", "文号", "宿政办发〔2026〕33号"),
        table_row("发文日期", "2026-07-17", "有效性", ""),
        table_row("名称", "市政府办公室关于印发宿迁市促进新型就业困难人员就业若干措施的通知"),
        table_row("主题分类", "政策文件 劳动就业 通知"),
        table_row("附件下载", "宿政办发〔2026〕33号.docx"),
    ]


def sample_detail_text(**overrides):
    text = (
        "发布机构：宿迁市人民政府办公室\n"
        "文号：宿政办发〔2026〕33号\n"
        "发文日期：2026-07-17\n"
        "有效性：\n"
        "名称：市政府办公室关于印发宿迁市促进新型就业困难人员就业若干措施的通知\n"
        "主题分类：政策文件 劳动就业 通知\n"
        "附件下载：宿政办发〔2026〕33号.docx\n"
        "发布日期：2026-07-17 10:44\n"
    )
    for key, value in overrides.items():
        for line_key in ("发布机构", "文号", "发文日期", "名称", "主题分类"):
            if key == line_key:
                text = text.replace(f"{line_key}：", f"{line_key}：{value}")
    return text


@pytest.mark.asyncio
async def test_suqian_detail_extracts_own_header_fields():
    collector = SuqianCollector(None, None, SCAN_TARGETS["suqian_zcwj"])
    collector._source_ids[SuqianDetailPage.url] = "142334102/2026-00008"
    page = SuqianDetailPage(
        text=sample_detail_text(),
        zoomcon="各县、区人民政府：\n宿迁市人民政府办公室\n2026年7月17日\n（此件公开发布）",
        table_rows=sample_table_rows(),
    )
    detail = await collector._parse_suqian_detail(page, "后备标题")
    assert detail.header_detected is True
    assert detail.missing_fields == []
    assert detail.invalid_fields == []
    assert detail.record is not None
    assert detail.record.source_id == "142334102/2026-00008"
    assert detail.record.issuing_agency == "宿迁市人民政府办公室"
    assert detail.record.page_document_number == "宿政办发〔2026〕33号"
    assert detail.record.published_date == date(2026, 7, 17)
    assert detail.record.authored_date == date(2026, 7, 17)
    assert detail.record.topic_category == "政策文件 劳动就业 通知"
    assert "各县、区人民政府" in detail.record.body_text


@pytest.mark.asyncio
async def test_suqian_detail_extracts_fields_without_label_swallowing():
    """表格同行多个 th/td 配对时，机构字段不应吞掉后续文号标签。"""
    collector = SuqianCollector(None, None, SCAN_TARGETS["suqian_zcwj"])
    page = SuqianDetailPage(
        text="发布机构：宿迁市人民政府办公室 文号：宿政办发〔2026〕33号\n发布日期：2026-07-17 10:44",
        zoomcon="正文",
        table_rows=[
            table_row("发布机构", "宿迁市人民政府办公室", "文号", "宿政办发〔2026〕33号"),
            table_row("发文日期", "2026-07-17", "有效性", ""),
        ],
    )
    detail = await collector._parse_suqian_detail(page, "后备标题")
    assert detail.record is not None
    assert detail.record.issuing_agency == "宿迁市人民政府办公室"
    assert detail.record.page_document_number == "宿政办发〔2026〕33号"
    assert "文号" not in detail.record.issuing_agency


@pytest.mark.asyncio
async def test_suqian_detail_no_header_passes():
    collector = SuqianCollector(None, None, SCAN_TARGETS["suqian_zcwj"])
    page = SuqianDetailPage(text="无表头的页面", zoomcon="正文")
    detail = await collector._parse_suqian_detail(page, "后备标题")
    assert detail.header_detected is False
    assert detail.record is None


@pytest.mark.asyncio
async def test_suqian_detail_404_raises_item_review():
    from app.domain import ItemReviewRequired

    collector = SuqianCollector(None, None, SCAN_TARGETS["suqian_zcwj"])
    page = SuqianDetailPage(text="您访问的页面不存在或已被删除\n404")
    with pytest.raises(ItemReviewRequired):
        await collector._parse_suqian_detail(page, "后备标题")


@pytest.mark.asyncio
async def test_suqian_detail_missing_field_reports_meta():
    collector = SuqianCollector(None, None, SCAN_TARGETS["suqian_zcwj"])
    rows = [
        table_row("发布机构", "宿迁市人民政府办公室"),
        table_row("发文日期", "2026-07-17", "有效性", ""),
    ]
    page = SuqianDetailPage(
        text=sample_detail_text().replace("文号：宿政办发〔2026〕33号\n", ""),
        zoomcon="正文",
        table_rows=rows,
    )
    detail = await collector._parse_suqian_detail(page, "后备标题")
    assert detail.header_detected is True
    assert "文号" in detail.missing_fields


@pytest.mark.asyncio
async def test_suqian_detail_extracts_attachment_link():
    collector = SuqianCollector(None, None, SCAN_TARGETS["suqian_zcwj"])
    fujian_items = [
        {
            "href": "b9db7123959242e7916e653aca71c5d0/files/99ee95c0ce814b0ab8cc172f80f37151.docx",
            "text": "宿政办发〔2026〕33号.docx",
            "context": "",
            "index": 0,
        }
    ]
    page = SuqianDetailPage(
        text=sample_detail_text(), zoomcon="正文", fujian_items=fujian_items,
        table_rows=sample_table_rows(),
    )
    links = await collector._extract_suqian_detail_links(page, page.url)
    assert len(links) == 1
    assert links[0].kind == "附件"
    assert links[0].url == (
        "https://www.suqian.gov.cn/zcwjk/zcwj/202607/b9db7123959242e7916e653aca71c5d0/files/"
        "99ee95c0ce814b0ab8cc172f80f37151.docx"
    )


@pytest.mark.asyncio
async def test_suqian_detail_news_page_with_publish_date_only_passes():
    """仅有“发布日期”字样的普通新闻页不应被当作政策详情表头。"""
    collector = SuqianCollector(None, None, SCAN_TARGETS["suqian_zcwj"])
    text = "宿迁市某部门发布通知\n发布日期：2026-07-17 10:44\n正文内容"
    page = SuqianDetailPage(text=text, zoomcon="正文内容")
    detail = await collector._parse_suqian_detail(page, "后备标题")
    assert detail.header_detected is False
    assert detail.record is None


@pytest.mark.asyncio
async def test_suqian_detail_single_table_label_only_passes():
    """仅命中一个表格标签（如文号引用出现在正文）不判定为表头。"""
    collector = SuqianCollector(None, None, SCAN_TARGETS["suqian_zcwj"])
    text = "正文提到文号：宿政办发〔2026〕33号\n发布日期：2026-07-17 10:44"
    page = SuqianDetailPage(
        text=text, zoomcon="正文",
        table_rows=[table_row("文号", "宿政办发〔2026〕33号")],
    )
    detail = await collector._parse_suqian_detail(page, "后备标题")
    assert detail.header_detected is False
    assert detail.record is None


# ==================== 宿迁市政府文件（szfwj） ====================

def test_szfwj_target_registered():
    target = SCAN_TARGETS["suqian_szfwj"]
    assert target.collector_type == "suqian"
    assert target.key == "suqian_szfwj"
    assert target.host == "www.suqian.gov.cn"
    site = SCAN_TARGETS  # site check via config
    from app.config import resolve_site

    suqian_site = resolve_site("jiangsu_suqian")
    assert "suqian_szfwj" in suqian_site.target_keys


def test_parse_szfwj_list_page():
    target = SCAN_TARGETS["suqian_szfwj"]
    html = """
    <ul class="listContent">
      <li>
      <a href="/cnsq/szfwj/202607/4c2b0f7d0a7c4e9fab831bea414a1b46.shtml" target="_blank"
             title='市政府关于同意宿迁市2026年度第七期国有建设用地使用权供应方案的批复'  >市政府关于同意宿迁市2026年度第七期国有建设用地使用权供应方案的批复</a> <span>2026-07-06</span>
      </li>
      <li>
      <a href="/cnsq/szfwj/202607/82445631fb98491cbc7f66d807244565.shtml" target="_blank"
             title='市政府关于同意宿迁市中心城市教育设施布局规划（2024-2035年）的批复'  >市政府关于同意宿迁市中心城市教育设施布局规划（2024-2035年）的批复</a> <span>2026-07-01</span>
      </li>
    </ul>
    """
    items = parse_szfwj_list_page(html, 1, target)
    assert len(items) == 2
    assert items[0].title == "市政府关于同意宿迁市2026年度第七期国有建设用地使用权供应方案的批复"
    assert items[0].url == "https://www.suqian.gov.cn/cnsq/szfwj/202607/4c2b0f7d0a7c4e9fab831bea414a1b46.shtml"
    assert items[0].published_date == date(2026, 7, 6)
    assert items[0].stable_id == (
        "suqian_szfwj|url:https://www.suqian.gov.cn/cnsq/szfwj/202607/"
        "4c2b0f7d0a7c4e9fab831bea414a1b46.shtml"
    )


class SzfwjFakeLocator:
    def __init__(self, text="", count=0, items=None):
        self.text = text
        self._count = count
        self.items = items or []

    async def count(self):
        return self._count

    async def inner_text(self):
        return self.text

    async def evaluate_all(self, _script):
        return self.items

    @property
    def first(self):
        return self

    def locator(self, _selector):
        return self

    def filter(self, **_kwargs):
        return self

    def nth(self, _index):
        return self


class SzfwjDetailPage:
    url = "https://www.suqian.gov.cn/cnsq/szfwj/202607/4c2b0f7d0a7c4e9fab831bea414a1b46.shtml"

    def __init__(self, text="", zoomcon="", table_rows=None, link_items=None):
        self.text = text
        self.zoomcon = zoomcon
        self.table_rows = table_rows or []
        self.link_items = link_items or []

    def locator(self, selector):
        if selector == "body":
            return SzfwjFakeLocator(self.text, count=1)
        if selector == "#zoomtitle":
            return SzfwjFakeLocator("市政府关于同意宿迁市2026年度第七期国有建设用地使用权供应方案的批复")
        if selector == "table.table_content td.c1":
            items = []
            for row in self.table_rows:
                items.extend(row)
            return SzfwjFakeLocator(items=items, count=1 if items else 0)
        if selector == "#zoomcon, .article-content .article":
            return SzfwjFakeLocator(self.zoomcon, count=1 if self.zoomcon else 0)
        if selector in ("table a[href]", "#zoomcon a[href]"):
            return SzfwjFakeLocator(items=self.link_items, count=1 if self.link_items else 0)
        if selector == "a":
            return SzfwjFakeLocator(items=[])
        return SzfwjFakeLocator("", count=0)


def szfwj_table_row(label, value):
    return [{"label": label, "value": value}]


@pytest.mark.asyncio
async def test_szfwj_detail_extracts_own_header_fields():
    collector = SuqianSzfwjCollector(None, None, SCAN_TARGETS["suqian_szfwj"])
    page = SzfwjDetailPage(
        text="正文",
        zoomcon="市自然资源和规划局：\n关于同意……的批复\n（此件公开发布）",
        table_rows=[
            szfwj_table_row("名称", "市政府关于同意宿迁市2026年度第七期国有建设用地使用权供应方案的批复"),
            szfwj_table_row("索引号", "014320291/2026-00255"),
            szfwj_table_row("分类", "政府文件"),
            szfwj_table_row("发布机构", "宿迁市人民政府"),
            szfwj_table_row("发文日期", "2026-07-06"),
            szfwj_table_row("文号", "宿政复〔2026〕26号"),
            szfwj_table_row("文件下载", "宿政复〔2026〕26号.doc"),
        ],
    )
    detail = await collector._parse_szfwj_detail(page, "后备标题")
    assert detail.header_detected is True
    assert detail.missing_fields == []
    assert detail.record is not None
    assert detail.record.source_id == "014320291/2026-00255"
    assert detail.record.issuing_agency == "宿迁市人民政府"
    assert detail.record.page_document_number == "宿政复〔2026〕26号"
    assert detail.record.published_date == date(2026, 7, 6)
    assert "市自然资源和规划局" in detail.record.body_text


@pytest.mark.asyncio
async def test_szfwj_detail_no_header_passes():
    collector = SuqianSzfwjCollector(None, None, SCAN_TARGETS["suqian_szfwj"])
    page = SzfwjDetailPage(text="无表头的页面", zoomcon="正文")
    detail = await collector._parse_szfwj_detail(page, "后备标题")
    assert detail.header_detected is False
    assert detail.record is None


@pytest.mark.asyncio
async def test_szfwj_detail_missing_field_reports_meta():
    collector = SuqianSzfwjCollector(None, None, SCAN_TARGETS["suqian_szfwj"])
    page = SzfwjDetailPage(
        text="正文",
        zoomcon="正文",
        table_rows=[
            szfwj_table_row("索引号", "014320291/2026-00255"),
            szfwj_table_row("发布机构", "宿迁市人民政府"),
            szfwj_table_row("发文日期", "2026-07-06"),
        ],
    )
    detail = await collector._parse_szfwj_detail(page, "后备标题")
    assert detail.header_detected is True
    assert "文号" in detail.missing_fields
