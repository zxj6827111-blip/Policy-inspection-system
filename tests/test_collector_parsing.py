from datetime import date

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeout

from app.collector import (
    BrowserCollector,
    comparable_title,
    extract_labeled_value,
    is_attachment_url,
    is_district_list_response,
    parse_iso_date,
    policy_list_item_from_api,
)
from app.config import SCAN_TARGETS
from app.domain import SafetyPause
from app.putuo_collector import PutuoDistrictCollector, parse_putuo_date, putuo_list_item_from_api


def test_parse_list_metadata_values():
    text = "发文单位：上海市普陀区人民政府\n发布日期：2026-07-03\n文号：普府〔2026〕54号"
    assert extract_labeled_value(text, "发文单位") == "上海市普陀区人民政府"
    assert parse_iso_date(extract_labeled_value(text, "发布日期")) == date(2026, 7, 3)
    assert extract_labeled_value(text, "文号") == "普府〔2026〕54号"


def test_public_list_record_provides_stable_detail_url_and_date():
    item = policy_list_item_from_api(
        {
            "title": "测试文件",
            "businessId": "0075204477",
            "siteId": "0075",
            "publishDate": "2026-07-03 09:49:12",
        },
        "普陀区",
        1,
        0,
    )
    assert item.url == "https://www.shanghai.gov.cn/zhengce/detail?businessId=0075204477&siteId=0075"
    assert item.published_date == date(2026, 7, 3)


def test_comparable_title_ignores_rendering_whitespace_and_full_width_forms():
    assert comparable_title("上海　政策\n文件") == comparable_title("上海 政策文件")


@pytest.mark.asyncio
async def test_detail_metadata_reloads_once_after_transient_timeout(monkeypatch):
    class DelayedDetailPage:
        url = "https://example.test/detail"

        def __init__(self):
            self.wait_calls = 0

        async def wait_for_function(self, _script, timeout):
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise PlaywrightTimeout("first render was incomplete")

    collector = BrowserCollector(None, None)
    reloaded = []

    async def safe_goto(page, url):
        reloaded.append((page, url))

    monkeypatch.setattr(collector, "safe_goto", safe_goto)
    page = DelayedDetailPage()
    await collector._wait_for_detail_metadata(page)

    assert page.wait_calls == 2
    assert reloaded == [(page, page.url)]


class FakeRobotsResponse:
    def __init__(self, status, content_type):
        self.status = status
        self.content_type = content_type

    async def header_value(self, name):
        assert name == "content-type"
        return self.content_type


class FakeRobotsPage:
    def __init__(self, text):
        self.text = text

    def locator(self, name):
        assert name == "body"
        return self

    async def inner_text(self):
        return self.text


@pytest.mark.asyncio
async def test_missing_main_site_robots_does_not_block_scan(monkeypatch):
    collector = BrowserCollector(None, None)
    collector.page = FakeRobotsPage("not found")

    async def safe_goto(_page, _url):
        return FakeRobotsResponse(404, "text/html")

    monkeypatch.setattr(collector, "safe_goto", safe_goto)
    await collector.check_robots()

    assert collector._robots is None


@pytest.mark.asyncio
async def test_main_site_robots_are_enforced_only_for_plain_text_response(monkeypatch):
    collector = BrowserCollector(None, None)
    collector.page = FakeRobotsPage("User-agent: *\nDisallow: /private")
    allowed = []

    async def safe_goto(_page, _url):
        return FakeRobotsResponse(200, "text/plain; charset=utf-8")

    monkeypatch.setattr(collector, "safe_goto", safe_goto)
    monkeypatch.setattr(collector, "ensure_allowed", allowed.append)
    await collector.check_robots()

    assert collector._robots is not None
    assert collector._robots.can_fetch("*", "https://www.shanghai.gov.cn/private") is False
    assert allowed == ["https://www.shanghai.gov.cn/zhengce/more?level=district&siteId=all"]


def test_public_list_record_missing_identity_pauses():
    with pytest.raises(SafetyPause, match="缺少 businessId"):
        policy_list_item_from_api({"title": "测试文件"}, "普陀区", 1, 0)


def test_public_list_record_carries_policy_explanation_and_service_links():
    item = policy_list_item_from_api(
        {
            "title": "测试文件",
            "businessId": "0075204036",
            "siteId": "0075",
            "relates": [{"id": "abc123", "title": "政策解读"}],
            "affairs": [{"link": "https://example.test/service"}],
        },
        "普陀区",
        1,
        0,
    )
    assert [(link.kind, link.url) for link in item.related_links] == [
        ("政策解读", "https://www.shanghai.gov.cn/zhengce/detail?id=abc123&key=relates"),
        ("阅办联动", "https://example.test/service"),
    ]


def test_attachment_extension_in_download_query_is_detected():
    assert is_attachment_url(
        "https://www.shanghai.gov.cn/gwk/resource/file?pathname=2/0075/a/report.pdf&filename=report.pdf"
    )


def test_district_response_match_requires_exact_single_site():
    class Request:
        post_data = '{"siteIdList":["0075"]}'

    class Response:
        url = "https://www.shanghai.gov.cn/gwk/policy/page"
        request = Request()

    assert is_district_list_response(Response(), "0075")
    Response.request.post_data = '{"siteIdList":["0070","0075"]}'
    assert not is_district_list_response(Response(), "0075")


def test_putuo_public_api_record_keeps_source_and_relative_detail_url():
    item = putuo_list_item_from_api(
        {"title": "普陀区政府文件", "url": "/zhengwu/zdgkml-qzfwj/2026/188/204477.html", "display_date": "2026-07-03"},
        1,
        0,
    )
    assert item.district == "普陀区"
    assert item.source_site == "区级网站·普陀区"
    assert item.url == "https://www.shpt.gov.cn/zhengwu/zdgkml-qzfwj/2026/188/204477.html"
    assert item.published_date == date(2026, 7, 3)


def test_putuo_date_parser_accepts_metadata_format():
    assert parse_putuo_date("2026年07月03日") == date(2026, 7, 3)


class PutuoDetailPage:
    url = "https://www.shpt.gov.cn/zhengwu/zdgkml-qzfwj/2026/188/204477.html"

    def __init__(self, text, metadata_items=None):
        self.text = text
        self.metadata_items = metadata_items or []

    def locator(self, selector):
        if selector in {"body", "article, .article-content, .TRS_Editor, .content"}:
            return FakeLocator(self.text)
        if selector == "h1":
            return FakeLocator("测试政策标题")
        if selector == "a":
            return FakeLocator(items=[])
        if selector == ".article-info .col-md-7, .article-info .col-md-5":
            return FakeLocator(items=self.metadata_items)
        return FakeLocator("", count=0)


@pytest.mark.asyncio
async def test_putuo_detail_extracts_all_seven_header_fields():
    detail = await PutuoDistrictCollector(None, None, SCAN_TARGETS["putuo_government"])._parse_putuo_detail(
        PutuoDetailPage(
            """索引号：SY310107202603028
主题分类：土地
公开属性：主动公开
成文日期：2026年07月03日
发文字号：普府〔2026〕54号
发布日期：2026年07月03日
公开主体：上海市普陀区人民政府
正文内容"""
        ),
        "后备标题",
    )
    assert detail.header_detected is True
    assert detail.missing_fields == []
    assert detail.invalid_fields == []
    assert detail.record is not None
    assert detail.record.source_id == "SY310107202603028"
    assert detail.record.topic_category == "土地"
    assert detail.record.disclosure_attribute == "主动公开"
    assert detail.record.authored_date == detail.record.published_date == date(2026, 7, 3)
    assert detail.record.page_document_number == "普府〔2026〕54号"
    assert detail.record.issuing_agency == "上海市普陀区人民政府"


@pytest.mark.asyncio
async def test_putuo_detail_extracts_metadata_from_adjacent_spans():
    metadata_items = [
        {"label": "索引号：", "value": "SY310107202601607"},
        {"label": "主题分类：", "value": "其他"},
        {"label": "公开属性：", "value": "主动公开"},
        {"label": "成文日期：", "value": "2026年04月09日"},
        {"label": "发文字号：", "value": "普委〔2026〕37号"},
        {"label": "发布日期：", "value": "2026年04月30日"},
        {"label": "公开主体：", "value": "中共上海市普陀区委员会 上海市普陀区人民政府"},
    ]
    detail = await PutuoDistrictCollector(None, None, SCAN_TARGETS["putuo_party_government"])._parse_putuo_detail(
        PutuoDetailPage(
            """索引号：
SY310107202601607
主题分类：
其他
公开属性：
主动公开
成文日期：
2026年04月09日
发文字号：
普委〔2026〕37号
发布日期：
2026年04月30日
公开主体：
中共上海市普陀区委员会 上海市普陀区人民政府""",
            metadata_items,
        ),
        "后备标题",
    )

    assert detail.missing_fields == []
    assert detail.invalid_fields == []
    assert detail.record is not None
    assert detail.record.source_id == "SY310107202601607"
    assert detail.record.authored_date == date(2026, 4, 9)
    assert detail.record.published_date == date(2026, 4, 30)
    assert detail.record.page_document_number == "普委〔2026〕37号"
    assert detail.record.issuing_agency == "中共上海市普陀区委员会 上海市普陀区人民政府"


class FakePutuoApiResponse:
    status = 200

    def __init__(self, payload):
        self.payload = payload
        self.disposed = False

    async def text(self):
        return "ok"

    async def json(self):
        return self.payload

    async def dispose(self):
        self.disposed = True


class FakePutuoApiRequest:
    def __init__(self, payload):
        self.payload = payload
        self.data = None

    async def post(self, _url, *, data, timeout, fail_on_status_code):
        self.data = data
        assert timeout == 45_000
        assert fail_on_status_code is False
        return FakePutuoApiResponse(self.payload)


class FakePutuoApiSafety:
    async def before_request(self):
        return None

    def after_request(self, *_args, **_kwargs):
        return None


@pytest.mark.asyncio
async def test_putuo_api_uses_page_no_and_accepts_exact_last_page_count():
    payload = {"data": {"list": [{"id": 16}], "pageNo": 2, "totalPage": 2, "totalCount": 16}}
    collector = PutuoDistrictCollector(None, FakePutuoApiSafety(), SCAN_TARGETS["putuo_government"])
    request = FakePutuoApiRequest(payload)
    collector.context = type("Context", (), {"request": request})()

    records = await collector._fetch_page(2)

    assert records == [{"id": 16}]
    assert request.data["pageNo"] == 2
    assert "pageNum" not in request.data
    assert await collector.estimated_total() == 16


@pytest.mark.asyncio
async def test_putuo_api_rejects_response_page_mismatch():
    payload = {"data": {"list": [{"id": 1}], "pageNo": 1, "totalPage": 2, "totalCount": 16}}
    collector = PutuoDistrictCollector(None, FakePutuoApiSafety(), SCAN_TARGETS["putuo_government"])
    collector.context = type("Context", (), {"request": FakePutuoApiRequest(payload)})()

    with pytest.raises(SafetyPause, match="页码校验失败"):
        await collector._fetch_page(2)


@pytest.mark.asyncio
async def test_putuo_detail_marks_missing_and_invalid_header_fields_without_dropping_record():
    detail = await PutuoDistrictCollector(None, None, SCAN_TARGETS["putuo_government"])._parse_putuo_detail(
        PutuoDetailPage(
            """索引号：SY310107202603028
主题分类：土地
公开属性：主动公开
成文日期：2026年13月40日
发文字号：普府〔2026〕54号
发布日期：
公开主体：上海市普陀区人民政府"""
        ),
        "后备标题",
    )
    assert detail.record is not None
    assert detail.missing_fields == ["发布日期"]
    assert detail.invalid_fields == ["成文日期（格式无效）"]


@pytest.mark.asyncio
async def test_putuo_detail_without_header_is_passed_without_record():
    detail = await PutuoDistrictCollector(None, None, SCAN_TARGETS["putuo_government"])._parse_putuo_detail(
        PutuoDetailPage("这是没有七项元数据表头的普通信息页面。"), "后备标题"
    )
    assert detail.header_detected is False
    assert detail.record is None


class FakeLocator:
    def __init__(self, text="", count=1, items=None):
        self.text = text
        self._count = count
        self.items = items or []

    async def count(self):
        return self._count

    async def inner_text(self):
        return self.text

    @property
    def first(self):
        return self

    async def evaluate_all(self, _expression):
        return self.items


class FakeDetailPage:
    url = "https://www.shanghai.gov.cn/zhengce/detail?businessId=0075204477&siteId=0075"

    def __init__(self, text):
        self.text = text

    def locator(self, selector):
        if selector == "main":
            return FakeLocator(self.text)
        if selector == "h1":
            return FakeLocator("测试政策标题")
        if selector == "article, .article-content, .policy-content, .TRS_Editor":
            return FakeLocator(self.text)
        if selector == "a":
            return FakeLocator(items=[
                {"text": "政策解读", "href": "https://www.shanghai.gov.cn/read/1"},
                {"text": "附件", "href": "https://www.shanghai.gov.cn/file/a.pdf"},
            ])
        return FakeLocator("", count=0)


@pytest.mark.asyncio
async def test_parse_complete_dynamic_detail_and_business_id():
    text = """测试政策标题
发文单位：上海市普陀区人民政府
发布日期：2026-07-03
文号：普府〔2026〕55号
正文内容
上海市普陀区人民政府
2026年7月3日"""
    record = await BrowserCollector(None, None)._parse_detail(FakeDetailPage(text), "普陀区", "后备标题")
    assert record.source_id == "0075204477"
    assert record.issuing_agency == "上海市普陀区人民政府"
    assert record.page_document_number == "普府〔2026〕55号"
    assert record.published_date == record.authored_date == date(2026, 7, 3)
    assert [link.kind for link in record.related_links] == ["政策解读", "附件"]


@pytest.mark.asyncio
async def test_missing_dynamic_metadata_pauses_instead_of_saving_empty_record():
    with pytest.raises(SafetyPause, match="缺少发文机构或发布日期"):
        await BrowserCollector(None, None)._parse_detail(FakeDetailPage("只有页头和页脚"), "普陀区", "后备标题")


@pytest.mark.asyncio
async def test_detail_prefers_agency_signature_over_attachment_effective_date():
    text = """发文单位：上海市崇明区烟草专卖局
发布日期：2026-06-11
正文
上海市崇明区烟草专卖局
2026年5月29日
附件规定自2026年7月1日起施行"""
    record = await BrowserCollector(None, None)._parse_detail(FakeDetailPage(text), "崇明区", "后备标题")
    assert record.authored_date == date(2026, 5, 29)
