from datetime import date

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeout

from app.collector import (
    BrowserCollector,
    attachment_payload_error,
    comparable_title,
    extract_labeled_value,
    is_attachment_url,
    is_district_list_response,
    parse_iso_date,
    policy_list_item_from_api,
)
from app.config import SCAN_TARGETS
from app.domain import RelatedLink, SafetyPause
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


def test_public_list_record_ignores_hidden_api_related_links():
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
    assert item.related_links == []


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
    payload = {
        "data": {
            "list": [{"id": 16, "doc_flag": "1"}],
            "pageNo": 2,
            "totalPage": 2,
            "totalCount": 16,
        }
    }
    collector = PutuoDistrictCollector(None, FakePutuoApiSafety(), SCAN_TARGETS["putuo_government"])
    request = FakePutuoApiRequest(payload)
    collector.context = type("Context", (), {"request": request})()

    records = await collector._fetch_page(2)

    assert records == [{"id": 16, "doc_flag": "1"}]
    assert request.data["pageNo"] == 2
    assert request.data["pageSize"] == 15
    assert request.data["orderFields"] == ["display_date"]
    assert request.data["orderTypes"] == ["desc"]
    assert request.data["docFlag"] == "1"
    assert "pageNum" not in request.data
    assert await collector.estimated_total() == 16


@pytest.mark.asyncio
async def test_putuo_api_rejects_response_page_mismatch():
    payload = {
        "data": {
            "list": [{"id": 1, "doc_flag": "1"}],
            "pageNo": 1,
            "totalPage": 2,
            "totalCount": 16,
        }
    }
    collector = PutuoDistrictCollector(None, FakePutuoApiSafety(), SCAN_TARGETS["putuo_government"])
    collector.context = type("Context", (), {"request": FakePutuoApiRequest(payload)})()

    with pytest.raises(SafetyPause, match="页码校验失败"):
        await collector._fetch_page(2)


@pytest.mark.asyncio
async def test_putuo_api_requests_doc_flag_filter_for_policy_files():
    payload = {
        "data": {
            "list": [{"id": 1, "doc_flag": "1", "title": "政策文件", "url": "/a.html"}],
            "pageNo": 1,
            "totalPage": 1,
            "totalCount": 1,
        }
    }
    collector = PutuoDistrictCollector(None, FakePutuoApiSafety(), SCAN_TARGETS["putuo_bureaus"])
    request = FakePutuoApiRequest(payload)
    collector.context = type("Context", (), {"request": request})()

    records = await collector._fetch_page(1)

    assert request.data["docFlag"] == "1"
    assert request.data["channelList"] == ["6"]
    assert request.data["pageNo"] == 1
    assert request.data["pageSize"] == 15
    assert request.data["orderFields"] == ["display_date"]
    assert request.data["orderTypes"] == ["desc"]
    assert records == payload["data"]["list"]


@pytest.mark.asyncio
async def test_putuo_api_accepts_all_doc_flag_one_records():
    payload = {
        "data": {
            "list": [
                {"id": 144800, "doc_flag": "1", "title": "文件甲"},
                {"id": 144801, "doc_flag": "1", "title": "文件乙"},
            ],
            "pageNo": 1,
            "totalPage": 1,
            "totalCount": 2,
        }
    }
    collector = PutuoDistrictCollector(None, FakePutuoApiSafety(), SCAN_TARGETS["putuo_government"])
    collector.context = type("Context", (), {"request": FakePutuoApiRequest(payload)})()

    records = await collector._fetch_page(1)
    assert [item["id"] for item in records] == [144800, 144801]


@pytest.mark.asyncio
async def test_putuo_api_rejects_non_policy_doc_flag_with_filter_failure():
    payload = {
        "data": {
            "list": [
                {
                    "id": 144838,
                    "doc_flag": "0",
                    "channel_id": "3390",
                    "title": "建设项目环境影响登记表备案信息公开",
                    "url": "https://beian.china-eia.com/f/announcement/announcementShow",
                }
            ],
            "pageNo": 1,
            "totalPage": 1,
            "totalCount": 1,
        }
    }
    collector = PutuoDistrictCollector(None, FakePutuoApiSafety(), SCAN_TARGETS["putuo_bureaus"])
    collector.context = type("Context", (), {"request": FakePutuoApiRequest(payload)})()

    with pytest.raises(SafetyPause, match="政策文件过滤失效") as exc_info:
        await collector._fetch_page(1)
    message = str(exc_info.value)
    assert "非文件记录" in message
    assert "page_number=1" in message
    assert "id=144838" in message
    assert "channel_id=3390" in message
    assert "doc_flag='0'" in message


def test_putuo_list_item_rejects_external_detail_domain():
    with pytest.raises(SafetyPause, match="异常详情域名"):
        putuo_list_item_from_api(
            {
                "title": "建设项目环境影响登记表备案系统",
                "url": "https://beian.china-eia.com/",
                "display_date": "2026-07-03",
                "doc_flag": "0",
            },
            1,
            0,
        )


@pytest.mark.asyncio

@pytest.mark.asyncio
async def test_putuo_uncapped_pagination_still_strict():
    payload = {
        "data": {
            "list": [{"id": i, "doc_flag": "1", "title": f"t{i}", "url": f"/a{i}.html"} for i in range(1, 3)],
            "pageNo": 2,
            "totalPage": 2,
            "totalCount": 17,
        }
    }
    # totalCount=17, pageSize=15 => page2 should have 2 rows. Not a cap.
    collector = PutuoDistrictCollector(
        None, FakePutuoApiSafety(), SCAN_TARGETS["putuo_government"], page_size=15, api_total_cap=10_000,
    )
    collector.context = type("Context", (), {"request": FakePutuoApiRequest(payload)})()
    records = await collector._fetch_page(2)
    assert len(records) == 2
    assert collector.capped_pagination_active is False


@pytest.mark.asyncio
async def test_putuo_capped_last_page_overflow_enters_capped_mode():
    # cap=10, page_size=3 => declared residual on last page is 1, but API returns 3.
    page_size = 3
    cap = 10
    declared_pages = 4  # ceil(10/3)=4, residual=1
    payload = {
        "data": {
            "list": [
                {"id": 100 + i, "doc_flag": "1", "title": f"t{i}", "url": f"/p4-{i}.html"}
                for i in range(3)
            ],
            "pageNo": 4,
            "totalPage": declared_pages,
            "totalCount": cap,
        }
    }
    collector = PutuoDistrictCollector(
        None, FakePutuoApiSafety(), SCAN_TARGETS["putuo_bureaus"],
        page_size=page_size, api_total_cap=cap,
    )
    collector.context = type("Context", (), {"request": FakePutuoApiRequest(payload)})()
    records = await collector._fetch_page(4)
    assert len(records) == 3
    assert collector.capped_pagination_active is True


@pytest.mark.asyncio
async def test_putuo_capped_mode_continues_past_declared_page_with_fixed_response_page():
    page_size = 3
    cap = 10
    pages = {
        1: {
            "list": [{"id": i, "doc_flag": "1", "title": f"p1-{i}", "url": f"/p1-{i}.html"} for i in range(1, 4)],
            "pageNo": 1, "totalPage": 4, "totalCount": cap,
        },
        2: {
            "list": [{"id": i, "doc_flag": "1", "title": f"p2-{i}", "url": f"/p2-{i}.html"} for i in range(4, 7)],
            "pageNo": 2, "totalPage": 4, "totalCount": cap,
        },
        3: {
            "list": [{"id": i, "doc_flag": "1", "title": f"p3-{i}", "url": f"/p3-{i}.html"} for i in range(7, 10)],
            "pageNo": 3, "totalPage": 4, "totalCount": cap,
        },
        4: {
            "list": [{"id": i, "doc_flag": "1", "title": f"p4-{i}", "url": f"/p4-{i}.html"} for i in range(10, 13)],
            "pageNo": 4, "totalPage": 4, "totalCount": cap,
        },
        5: {
            "list": [{"id": i, "doc_flag": "1", "title": f"p5-{i}", "url": f"/p5-{i}.html"} for i in range(13, 16)],
            "pageNo": 4, "totalPage": 4, "totalCount": cap,  # response page stuck at declared last
        },
        6: {
            "list": [{"id": i, "doc_flag": "1", "title": f"p6-{i}", "url": f"/p6-{i}.html"} for i in range(16, 18)],
            "pageNo": 4, "totalPage": 4, "totalCount": cap,
        },
        7: {
            "list": [],
            "pageNo": 0, "totalPage": 0, "totalCount": 0,
        },
    }

    class MultiPageRequest:
        def __init__(self):
            self.calls = []

        async def post(self, _url, *, data, timeout, fail_on_status_code):
            self.calls.append(dict(data))
            page_no = int(data["pageNo"])
            return FakePutuoApiResponse({"data": pages[page_no]})

    class PageCollector(PutuoDistrictCollector):
        async def safe_goto(self, page, url):
            return None

    collector = PageCollector(
        None, FakePutuoApiSafety(), SCAN_TARGETS["putuo_bureaus"],
        page_size=page_size, api_total_cap=cap, capped_max_extra_pages=20,
    )
    collector.page = object()
    collector.context = type("Context", (), {"request": MultiPageRequest()})()

    items = []
    async for item in collector.iter_items("普陀区"):
        items.append(item)

    assert collector.capped_pagination_active is True
    assert collector.capped_pagination_resolved is True
    assert len(items) == 17
    assert [item.page_number for item in items[-5:]] == [5, 5, 5, 6, 6]
    assert await collector.estimated_total() == 17
    # page 5 request kept requested pageNo=5 even if response said 4
    requested_pages = [call["pageNo"] for call in collector.context.request.calls]
    assert 5 in requested_pages and 6 in requested_pages and 7 in requested_pages


@pytest.mark.asyncio
async def test_putuo_capped_mode_stops_on_repeated_page_not_infinite():
    page_size = 2
    cap = 4
    repeated = {
        "list": [
            {"id": 90, "doc_flag": "1", "title": "r1", "url": "/r1.html"},
            {"id": 91, "doc_flag": "1", "title": "r2", "url": "/r2.html"},
        ],
        "pageNo": 2, "totalPage": 2, "totalCount": cap,
    }
    pages = {
        1: {
            "list": [
                {"id": 1, "doc_flag": "1", "title": "a", "url": "/a.html"},
                {"id": 2, "doc_flag": "1", "title": "b", "url": "/b.html"},
            ],
            "pageNo": 1, "totalPage": 2, "totalCount": cap,
        },
        2: {
            "list": [
                {"id": 3, "doc_flag": "1", "title": "c", "url": "/c.html"},
                {"id": 4, "doc_flag": "1", "title": "d", "url": "/d.html"},
            ],
            "pageNo": 2, "totalPage": 2, "totalCount": cap,
        },
        3: repeated,
        4: repeated,
    }

    class MultiPageRequest:
        async def post(self, _url, *, data, timeout, fail_on_status_code):
            return FakePutuoApiResponse({"data": pages[int(data["pageNo"])]})

    class PageCollector(PutuoDistrictCollector):
        async def safe_goto(self, page, url):
            return None

    collector = PageCollector(
        None, FakePutuoApiSafety(), SCAN_TARGETS["putuo_bureaus"],
        page_size=page_size, api_total_cap=cap, capped_max_extra_pages=20,
    )
    collector.page = object()
    collector.context = type("Context", (), {"request": MultiPageRequest()})()

    with pytest.raises(SafetyPause, match="重复返回已扫描记录"):
        async for _ in collector.iter_items("普陀区"):
            pass


@pytest.mark.asyncio
async def test_putuo_capped_mode_rejects_doc_flag_zero():
    payload = {
        "data": {
            "list": [{"id": 1, "doc_flag": "0", "title": "x", "url": "/x.html"}],
            "pageNo": 1, "totalPage": 1, "totalCount": 10,
        }
    }
    collector = PutuoDistrictCollector(
        None, FakePutuoApiSafety(), SCAN_TARGETS["putuo_bureaus"],
        page_size=3, api_total_cap=10,
    )
    collector.context = type("Context", (), {"request": FakePutuoApiRequest(payload)})()
    with pytest.raises(SafetyPause, match="政策文件过滤失效"):
        await collector._fetch_page(1)


@pytest.mark.asyncio
async def test_putuo_capped_resume_keeps_request_page_cursor():
    page_size = 2
    cap = 4
    pages = {
        1: {
            "list": [
                {"id": 1, "doc_flag": "1", "title": "a", "url": "/a.html"},
                {"id": 2, "doc_flag": "1", "title": "b", "url": "/b.html"},
            ],
            "pageNo": 1, "totalPage": 2, "totalCount": cap,
        },
        2: {
            "list": [
                {"id": 3, "doc_flag": "1", "title": "c", "url": "/c.html"},
                {"id": 4, "doc_flag": "1", "title": "d", "url": "/d.html"},
            ],
            "pageNo": 2, "totalPage": 2, "totalCount": cap,
        },
        3: {
            "list": [
                {"id": 5, "doc_flag": "1", "title": "e", "url": "/e.html"},
                {"id": 6, "doc_flag": "1", "title": "f", "url": "/f.html"},
            ],
            "pageNo": 2, "totalPage": 2, "totalCount": cap,
        },
        4: {"list": [], "pageNo": 0, "totalPage": 0, "totalCount": 0},
    }

    class MultiPageRequest:
        def __init__(self):
            self.calls = []

        async def post(self, _url, *, data, timeout, fail_on_status_code):
            self.calls.append(int(data["pageNo"]))
            return FakePutuoApiResponse({"data": pages[int(data["pageNo"])]})

    class PageCollector(PutuoDistrictCollector):
        async def safe_goto(self, page, url):
            return None

    collector = PageCollector(
        None, FakePutuoApiSafety(), SCAN_TARGETS["putuo_bureaus"],
        page_size=page_size, api_total_cap=cap, capped_max_extra_pages=20,
    )
    collector.page = object()
    request = MultiPageRequest()
    collector.context = type("Context", (), {"request": request})()
    collector.note_resume_examined(4)  # finished pages 1-2

    items = []
    async for item in collector.iter_items("普陀区", start_page=3, start_item_index=-1):
        items.append((item.page_number, item.item_index, item.title))

    assert items == [(3, 0, "e"), (3, 1, "f")]
    assert collector.capped_pagination_resolved is True
    assert await collector.estimated_total() == 6  # 4 resumed + 2 new
    assert 3 in request.calls


def test_putuo_list_item_still_rejects_external_domain_in_capped_payload():
    with pytest.raises(SafetyPause, match="异常详情域名"):
        putuo_list_item_from_api(
            {
                "title": "环评",
                "url": "https://beian.china-eia.com/",
                "doc_flag": "1",
            },
            5,
            0,
        )


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

    def locator(self, _selector):
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
        if selector in {
            "article, .article-content, .policy-content, .TRS_Editor",
            "article, .article-content, .policy-content, .TRS_Editor, .content, .article, .detail-content, .pages_content, .zw",
        }:
            return FakeLocator(self.text, items=[
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
    assert [link.source_area for link in record.related_links] == ["详情页正文", "详情页正文"]


@pytest.mark.asyncio
async def test_detail_controls_use_section_heading_and_attachment_url_for_classification():
    links = await BrowserCollector(None, None)._extract_visible_links_from_locator(
        FakeLocator(items=[
            {
                "index": 0,
                "text": "视频解读：《政策公开讲》政策引路",
                "context": "政策解读",
                "href": "",
            },
            {
                "index": 1,
                "text": "附件1《申报指南》.pdf",
                "context": "相关附件",
                "href": "https://www.shanghai.gov.cn/gwk/resource/file?filename=guide.pdf",
            },
        ]),
        "https://www.shanghai.gov.cn/detail",
        "详情页侧栏",
    )
    assert [(link.kind, link.link_text, link.element_index) for link in links] == [
        ("政策解读", "视频解读：《政策公开讲》政策引路", 0),
        ("附件", "附件1《申报指南》.pdf", 1),
    ]


class FakeListTrigger:
    def __init__(self, page, kind):
        self.page = page
        self.kind = kind
        self.click_count = 0

    async def is_visible(self):
        return True

    async def get_attribute(self, name):
        return self.kind if name == "alt" else None

    async def scroll_into_view_if_needed(self, **_kwargs):
        return None

    async def click(self, **_kwargs):
        self.click_count += 1
        self.page.current_kind = self.kind
        self.page.popover_open = True


class FakeTriggerLocator:
    def __init__(self, triggers):
        self.triggers = triggers

    async def count(self):
        return len(self.triggers)

    def nth(self, index):
        return self.triggers[index]


class FakePopoverItem:
    def __init__(self, page, index):
        self.page = page
        self.index = index

    async def inner_text(self):
        return self.page.items[self.page.current_kind][self.index]

    async def is_visible(self):
        return self.page.popover_open

    async def wait_for(self, **_kwargs):
        if not self.page.popover_open:
            raise PlaywrightTimeout("popover is hidden")


class FakePopoverLocator:
    def __init__(self, page):
        self.page = page

    async def count(self):
        if not self.page.popover_open or not self.page.current_kind:
            return 0
        return len(self.page.items[self.page.current_kind])

    @property
    def first(self):
        return self.nth(0)

    def nth(self, index):
        return FakePopoverItem(self.page, index)


class FakeKeyboard:
    def __init__(self, page):
        self.page = page

    async def press(self, _key):
        self.page.popover_open = False


class FakePopoverPage:
    def __init__(self):
        self.items = {
            "政策解读": ["视频解读一", "图文解读二"],
            "阅办联动": ["服务业发展引导资金申报"],
        }
        self.current_kind = ""
        self.popover_open = False
        self.keyboard = FakeKeyboard(self)
        self.triggers = [FakeListTrigger(self, kind) for kind in self.items]

    def locator(self, _selector):
        return FakePopoverLocator(self)

    async def wait_for_timeout(self, _milliseconds):
        return None


class FakeListContainer:
    def __init__(self, page):
        self.page = page

    def locator(self, _selector):
        return FakeTriggerLocator(self.page.triggers)


@pytest.mark.asyncio
async def test_list_related_icons_expand_popover_and_check_each_visible_item():
    page = FakePopoverPage()
    collector = BrowserCollector(None, None)

    async def fake_check(_page, _locator, related):
        # 真实 Ant Popover 在打开新标签后可能仍保持可见，下一类图标必须主动切换。
        page.popover_open = True
        slug = f"{related.kind}-{related.link_text}"
        url = f"https://www.shanghai.gov.cn/checked/{slug}"
        return {
            "kind": related.kind,
            "url": url,
            "final_url": url,
            "result": "ok",
            "error_type": "",
        }

    collector._check_related_click = fake_check
    links = await collector._extract_and_check_list_popovers(
        page,
        FakeListContainer(page),
        "https://www.shanghai.gov.cn/zhengce/more?siteId=all",
    )

    assert [(link.kind, link.link_text) for link in links] == [
        ("政策解读", "视频解读一"),
        ("政策解读", "图文解读二"),
        ("阅办联动", "服务业发展引导资金申报"),
    ]
    assert all(link.url.startswith("https://www.shanghai.gov.cn/checked/") for link in links)
    assert all(link.check_result["result"] == "ok" for link in links)
    assert [trigger.click_count for trigger in page.triggers] == [2, 1]


def test_attachment_payload_validation_detects_common_bad_files():
    assert attachment_payload_error("https://example.test/a.pdf", "application/pdf", b"%PDF-1.7")[0] == ""
    assert attachment_payload_error("https://example.test/a.pdf", "text/html", b"<html>error</html>")[0] == "html_error"
    assert attachment_payload_error("https://example.test/a.pdf", "application/pdf", b"not a pdf")[0] == "invalid_pdf"
    assert attachment_payload_error("https://example.test/a.docx", "", b"not a zip")[0] == "invalid_office_file"


class FakeDownload:
    def __init__(self, url, path, failure=None):
        self.url = url
        self._path = path
        self._failure = failure

    async def failure(self):
        return self._failure

    async def path(self):
        return str(self._path)


@pytest.mark.asyncio
async def test_downloaded_attachment_html_error_is_broken(tmp_path):
    downloaded = tmp_path / "bad.pdf"
    downloaded.write_bytes(b"<html>not a file</html>")
    related = RelatedLink("附件", "https://example.test/bad.pdf", source_area="详情页正文", link_text="附件")
    result = await BrowserCollector(None, None)._classify_download(
        FakeDownload("https://example.test/bad.pdf", downloaded),
        related,
    )
    assert result["result"] == "broken"
    assert result["error_type"] == "html_error"
    assert result["source_area"] == "详情页正文"


class FakeClickRoute:
    def __init__(self, url, resource_type="document"):
        self.request = type("Request", (), {"resource_type": resource_type, "url": url})()
        self.continued = False
        self.aborted = False

    async def continue_(self):
        self.continued = True

    async def abort(self, _error_code=None):
        self.aborted = True


class FakeClickContext:
    def __init__(self):
        self.handler = None
        self.unrouted = False
        self.routes = []

    def on(self, *_args):
        return None

    def remove_listener(self, *_args):
        return None

    async def route(self, pattern, handler):
        assert pattern == "**/*"
        self.handler = handler

    async def unroute(self, pattern, handler):
        assert pattern == "**/*"
        assert handler is self.handler
        self.unrouted = True

    async def wait_for_event(self, *_args, **_kwargs):
        raise PlaywrightTimeout("no popup")


class FakeClickPage:
    url = "https://www.shanghai.gov.cn/zhengce/detail"

    async def wait_for_event(self, *_args, **_kwargs):
        raise PlaywrightTimeout("no download")

    async def wait_for_timeout(self, _milliseconds):
        return None


class FakeClickLocator:
    def __init__(self, context):
        self.context = context

    async def scroll_into_view_if_needed(self, **_kwargs):
        return None

    async def click(self, **_kwargs):
        for url, resource_type in (
            ("https://1.1.1.1/start", "document"),
            ("http://127.0.0.1/private", "fetch"),
        ):
            route = FakeClickRoute(url, resource_type)
            self.context.routes.append(route)
            await self.context.handler(route)


@pytest.mark.asyncio
async def test_browser_click_blocks_private_redirect_before_request():
    context = FakeClickContext()
    collector = BrowserCollector(None, FakePutuoApiSafety())
    collector.context = context
    related = RelatedLink(
        "阅办联动", "", source_area="列表页", link_text="在线办理",
        source_page_url="https://www.shanghai.gov.cn/zhengce/more",
    )

    result = await collector._check_related_click(
        FakeClickPage(), FakeClickLocator(context), related
    )

    assert result["result"] == "error"
    assert result["error_type"] == "unsafe_destination"
    assert context.routes[0].continued is True
    assert context.routes[0].aborted is False
    assert context.routes[1].continued is False
    assert context.routes[1].aborted is True
    assert context.unrouted is True


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
