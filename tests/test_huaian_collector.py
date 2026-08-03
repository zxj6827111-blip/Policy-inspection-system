from datetime import date

import pytest

from app.config import SCAN_TARGETS
from app.domain import PolicyListItem, SafetyPause
from app.huaian_collector import HuaianCollector, parse_huaian_date


def sample_record(**overrides):
    record = {
        "id": "2c9493909f5e72a9019fb60652412880",
        "title": "市政府办公室关于印发淮安市促进法律服务高质量发展十条措施（试行）的通知",
        "path": "col/4122_231438/art/17828352/1785223346940hGKeeuyw.html",
        "domain": "https://www.huaian.gov.cn/",
        "link": "",
        "release_time": "2026-07-28T07:15:34.000Z",
        "docno": "淮政办发〔2026〕10号",
        "quomark": "014289810/2026-00028",
    }
    record.update(overrides)
    return record


def test_huaian_date_parser_accepts_iso():
    assert parse_huaian_date("2026-07-28T07:15:34.000Z") == date(2026, 7, 28)
    assert parse_huaian_date("2026-07-28") == date(2026, 7, 28)
    assert parse_huaian_date("") is None
    assert parse_huaian_date(None) is None


def test_huaian_list_item_maps_fields_and_identity():
    collector = HuaianCollector(None, None, SCAN_TARGETS["huaian_government"])
    item = collector._item_from_record(sample_record(), 3, 2)
    assert isinstance(item, PolicyListItem)
    assert item.district == "淮安市"
    assert item.page_number == 3
    assert item.item_index == 2
    assert item.title == sample_record()["title"]
    assert item.url == "https://www.huaian.gov.cn/col/4122_231438/art/17828352/1785223346940hGKeeuyw.html"
    assert item.published_date == date(2026, 7, 28)
    assert item.source_key == "huaian_government"
    assert item.stable_id == "huaian_government|id:2c9493909f5e72a9019fb60652412880"
    assert item.api_record_id == "2c9493909f5e72a9019fb60652412880"


def test_huaian_list_item_uses_external_link_when_present():
    collector = HuaianCollector(None, None, SCAN_TARGETS["huaian_government"])
    item = collector._item_from_record(
        sample_record(link="https://www.huaian.gov.cn/col/other/art/1.html"), 1, 0,
    )
    assert item.url == "https://www.huaian.gov.cn/col/other/art/1.html"


def test_huaian_list_item_marks_external_link_as_skip():
    collector = HuaianCollector(None, None, SCAN_TARGETS["huaian_government"])
    item = collector._item_from_record(sample_record(link="https://www.example.com/x.html"), 1, 0)
    assert item.doc_flag == "external"


def test_huaian_list_item_accepts_subdomain_and_county_hosts():
    collector = HuaianCollector(None, None, SCAN_TARGETS["huaian_government"])
    item = collector._item_from_record(
        sample_record(domain="https://fgw.huaian.gov.cn/"), 1, 0,
    )
    assert item.doc_flag == ""
    item = collector._item_from_record(
        sample_record(domain="https://www.lianshui.gov.cn/"), 1, 0,
    )
    assert item.doc_flag == ""


def test_huaian_list_item_requires_title_and_path():
    collector = HuaianCollector(None, None, SCAN_TARGETS["huaian_government"])
    with pytest.raises(SafetyPause):
        collector._item_from_record(sample_record(title=""), 1, 0)
    with pytest.raises(SafetyPause):
        collector._item_from_record(sample_record(path=""), 1, 0)


def test_huaian_record_identity_falls_back_to_url():
    collector = HuaianCollector(None, None, SCAN_TARGETS["huaian_government"])
    record = sample_record(id="", path="col/x/art/y.html")
    identity = collector._record_identity(record)
    assert identity == "huaian_government|url:https://www.huaian.gov.cn/col/x/art/y.html"


def test_huaian_target_config_carries_three_categories():
    assert SCAN_TARGETS["huaian_government"].channel_id == "市政府文件"
    assert SCAN_TARGETS["huaian_bureaus"].channel_id == "bm"
    assert SCAN_TARGETS["huaian_counties"].channel_id == "xq"
    for key in ("huaian_government", "huaian_bureaus", "huaian_counties"):
        assert SCAN_TARGETS[key].collector_type == "huaian"
        assert SCAN_TARGETS[key].host == "www.huaian.gov.cn"


class HuaianFakeLocator:
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


class HuaianDetailPage:
    url = "https://www.huaian.gov.cn/col/4122_231438/art/17828352/1785223346940hGKeeuyw.html"

    def __init__(self, table_rows=None, wz3="", interpret_items=None):
        self.table_rows = table_rows
        self.wz3 = wz3
        self.interpret_items = interpret_items or []

    def locator(self, selector):
        if selector == "body":
            return HuaianFakeLocator("页面正文")
        if selector == "h1":
            return HuaianFakeLocator("", count=0)
        if selector == "table.wk1 tr, table.xxgk_table tr":
            return HuaianFakeLocator(items=self.table_rows or [], count=1 if self.table_rows else 0)
        if selector == ".wz3":
            return HuaianFakeLocator(self.wz3, count=1 if self.wz3 else 0)
        if selector == "#podecipDiv":
            return HuaianFakeLocator(items=self.interpret_items, count=1 if self.interpret_items else 0)
        if selector == "a":
            return HuaianFakeLocator(items=[])
        return HuaianFakeLocator("", count=0)


def table_row(*cells):
    out = []
    for index in range(0, len(cells) - 1, 2):
        out.append({"label": cells[index], "value": cells[index + 1]})
    return out


@pytest.mark.asyncio
async def test_huaian_detail_extracts_own_header_fields():
    page = HuaianDetailPage(
        table_rows=[
            table_row("主题分类", "政府文件", "体裁分类", "通知"),
            table_row("索 引 号", "014289810/2026-00028", "发布机构", "淮安市人民政府办公室"),
            table_row("文 号", "淮政办发〔2026〕10号", "公开日期", "2026-07-28"),
        ],
        wz3="各县区人民政府，市各委办局：\n淮安市人民政府办公室\n2026年7月27日\n（此件公开发布）",
    )
    collector = HuaianCollector(None, None, SCAN_TARGETS["huaian_government"])
    detail = await collector._parse_huaian_detail(page, "后备标题")
    assert detail.header_detected is True
    assert detail.missing_fields == []
    assert detail.invalid_fields == []
    assert detail.record is not None
    assert detail.record.source_id == "014289810/2026-00028"
    assert detail.record.issuing_agency == "淮安市人民政府办公室"
    assert detail.record.page_document_number == "淮政办发〔2026〕10号"
    assert detail.record.published_date == date(2026, 7, 28)
    assert detail.record.authored_date == date(2026, 7, 27)
    assert detail.record.topic_category == "政府文件"


@pytest.mark.asyncio
async def test_huaian_detail_no_header_passes():
    page = HuaianDetailPage(table_rows=[], wz3="无表头的页面正文")
    collector = HuaianCollector(None, None, SCAN_TARGETS["huaian_government"])
    detail = await collector._parse_huaian_detail(page, "后备标题")
    assert detail.header_detected is False
    assert detail.record is None


@pytest.mark.asyncio
async def test_huaian_detail_missing_field_reports_meta():
    page = HuaianDetailPage(
        table_rows=[
            table_row("索 引 号", "014289810/2026-00028", "发布机构", "淮安市人民政府办公室"),
            table_row("公开日期", "2026-07-28"),
        ],
        wz3="正文",
    )
    collector = HuaianCollector(None, None, SCAN_TARGETS["huaian_government"])
    detail = await collector._parse_huaian_detail(page, "后备标题")
    assert detail.header_detected is True
    assert detail.missing_fields == ["文号"]


@pytest.mark.asyncio
async def test_huaian_detail_skips_hidden_rows():
    page = HuaianDetailPage(
        table_rows=[
            table_row("索 引 号", "014289810/2026-00028", "发布机构", "淮安市人民政府办公室"),
            table_row("文 号", "淮政办发〔2026〕10号", "公开日期", "2026-07-28"),
        ],
        wz3="正文",
    )
    collector = HuaianCollector(None, None, SCAN_TARGETS["huaian_government"])
    detail = await collector._parse_huaian_detail(page, "后备标题")
    assert detail.record is not None
    assert detail.record.published_date == date(2026, 7, 28)


@pytest.mark.asyncio
async def test_huaian_detail_supports_county_xxgk_table():
    """县区站 xxgk_table（tdlab 标签）结构同样可解析。"""
    page = HuaianDetailPage(
        table_rows=[
            [{"label": "索 引 号", "value": "014345851/2026-00021"},
             {"label": "主题分类", "value": "政府文件/规划计划"}],
            [{"label": "发布机构", "value": "\u200b盱眙县人民政府"},
             {"label": "公开日期", "value": "2026-07-06"}],
            [{"label": "文 号", "value": "\u200b盱政发〔2026〕9号"}],
        ],
        wz3="各县区人民政府：\n盱眙县人民政府\n2026年7月5日\n（此件公开发布）",
    )
    collector = HuaianCollector(None, None, SCAN_TARGETS["huaian_counties"])
    detail = await collector._parse_huaian_detail(page, "后备标题")
    assert detail.header_detected is True
    assert detail.record is not None
    assert detail.record.source_id == "014345851/2026-00021"
    assert detail.record.issuing_agency == "盱眙县人民政府"
    assert detail.record.page_document_number == "盱政发〔2026〕9号"
    assert detail.record.published_date == date(2026, 7, 6)
    assert detail.record.authored_date == date(2026, 7, 5)


class FakeHuaianApiResponse:
    status = 200

    def __init__(self, payload):
        self.payload = payload
        self.disposed = False

    async def text(self):
        return "ok"

    async def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    async def dispose(self):
        self.disposed = True


class FakeHuaianApiRequest:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def post(self, _url, *, form, timeout, fail_on_status_code):
        self.calls.append(int(form["pageNo"]))
        assert timeout == 45_000
        assert fail_on_status_code is False
        return FakeHuaianApiResponse(self.pages.get(int(form["pageNo"]), {"value": {"total": 0, "list": []}}))


class FakeHuaianApiSafety:
    async def before_request(self):
        return None

    def after_request(self, *_args, **_kwargs):
        return None


@pytest.mark.asyncio
async def test_huaian_fetch_page_validates_row_count_and_total_change():
    collector = HuaianCollector(None, FakeHuaianApiSafety(), SCAN_TARGETS["huaian_government"])
    collector.context = type(
        "Context", (), {"request": FakeHuaianApiRequest({
            1: {"value": {"total": 250, "list": [sample_record(id=f"r{i}") for i in range(100)]}},
        })}
    )()
    records = await collector._fetch_page(1)
    assert len(records) == 100
    assert await collector.estimated_total() == 250

    # 末页条数异常：声明 250 条 / pageSize 100 => 末页应 50 条
    collector.context.request = FakeHuaianApiRequest({
        1: {"value": {"total": 250, "list": [sample_record(id=f"r{i}") for i in range(100)]}},
        3: {"value": {"total": 250, "list": [sample_record(id=f"r{i}") for i in range(60)]}},
    })
    with pytest.raises(SafetyPause, match="条数异常"):
        await collector._fetch_page(3)

    # 扫描期间总量变化
    collector.context.request = FakeHuaianApiRequest({
        1: {"value": {"total": 250, "list": [sample_record(id=f"r{i}") for i in range(100)]}},
        2: {"value": {"total": 251, "list": [sample_record(id=f"r{i}") for i in range(100)]}},
    })
    with pytest.raises(SafetyPause, match="总量在扫描期间发生变化"):
        await collector._fetch_page(2)


@pytest.mark.asyncio
async def test_huaian_fetch_page_raises_safety_pause_on_non_json_body():
    collector = HuaianCollector(None, FakeHuaianApiSafety(), SCAN_TARGETS["huaian_government"])
    collector.context = type(
        "Context", (), {"request": FakeHuaianApiRequest({
            1: {"value": {"total": 0, "list": []}},
        })}
    )()
    collector.context.request.pages = {1: Exception(ValueError("Expecting value"))}
    with pytest.raises(SafetyPause, match="JSON 解析失败"):
        await collector._fetch_page(1)


@pytest.mark.asyncio
async def test_huaian_iter_items_paginates_and_dedupes_across_pages():
    collector = HuaianCollector(None, FakeHuaianApiSafety(), SCAN_TARGETS["huaian_government"])
    collector.page_size = 2
    collector.context = type(
        "Context", (), {"request": FakeHuaianApiRequest({
            1: {"value": {"total": 5, "list": [
                sample_record(id="a", path="col/1/art/a.html"),
                sample_record(id="b", path="col/1/art/b.html"),
            ]}},
            2: {"value": {"total": 5, "list": [
                sample_record(id="c", path="col/1/art/c.html"),
                sample_record(id="d", path="col/1/art/d.html"),
            ]}},
            3: {"value": {"total": 5, "list": [
                sample_record(id="e", path="col/1/art/e.html"),
            ]}},
        })}
    )()
    await collector.select_district("淮安市")
    items = [item async for item in collector.iter_items("淮安市")]
    assert [item.stable_id for item in items] == [
        "huaian_government|id:a", "huaian_government|id:b",
        "huaian_government|id:c", "huaian_government|id:d",
        "huaian_government|id:e",
    ]


@pytest.mark.asyncio
async def test_huaian_iter_items_detects_cross_page_duplicates():
    collector = HuaianCollector(None, FakeHuaianApiSafety(), SCAN_TARGETS["huaian_government"])
    collector.page_size = 2
    collector.context = type(
        "Context", (), {"request": FakeHuaianApiRequest({
            1: {"value": {"total": 4, "list": [
                sample_record(id="a", path="col/1/art/a.html"),
                sample_record(id="b", path="col/1/art/b.html"),
            ]}},
            2: {"value": {"total": 4, "list": [
                sample_record(id="b", path="col/1/art/b.html"),
                sample_record(id="c", path="col/1/art/c.html"),
            ]}},
        })}
    )()
    await collector.select_district("淮安市")
    with pytest.raises(SafetyPause, match="跨页重复记录"):
        async for _item in collector.iter_items("淮安市"):
            pass


@pytest.mark.asyncio
async def test_huaian_iter_items_resume_skips_previous_items():
    collector = HuaianCollector(None, FakeHuaianApiSafety(), SCAN_TARGETS["huaian_government"])
    collector.page_size = 2
    collector.context = type(
        "Context", (), {"request": FakeHuaianApiRequest({
            1: {"value": {"total": 4, "list": [
                sample_record(id="a", path="col/1/art/a.html"),
                sample_record(id="b", path="col/1/art/b.html"),
            ]}},
            2: {"value": {"total": 4, "list": [
                sample_record(id="c", path="col/1/art/c.html"),
                sample_record(id="d", path="col/1/art/d.html"),
            ]}},
        })}
    )()
    await collector.select_district("淮安市")
    items = [item async for item in collector.iter_items("淮安市", start_page=1, start_item_index=0)]
    assert [item.stable_id for item in items] == [
        "huaian_government|id:b", "huaian_government|id:c", "huaian_government|id:d",
    ]
