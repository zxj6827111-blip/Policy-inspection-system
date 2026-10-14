import random
from datetime import date

import pytest

from app.config import ORDOS_DEPARTMENTS, SCAN_SITES, SCAN_TARGETS, resolve_target, targets_for_site
from app.domain import SafetyPause
from app.ordos_collector import (
    OrdosZykCollector,
    declared_pagination,
    header_fields,
    page_url,
    pair_values,
    parse_ordos_date,
    parse_ordos_list_page,
)


TABLE1_HTML = """
<table id="table1"><thead><tr><th class="th1">序号</th><th class="th2">标题</th>
<th class="th3">发文字号</th><th class="th4">成文日期</th><th class="th5">发布日期</th></tr></thead>
<tbody id="resList"><tr>
<td> 1 </td>
<td> <a href="../202609/t20260918_3917319.html" target="_blank">鄂尔多斯市人民政府办公室关于《鄂尔多斯市地震应急预案》的补充通知</a> </td>
<td class="mobile"> <a href="../202609/t20260918_3917319.html">移动端重复标题</a><p><span>鄂府办发〔2026〕38号</span></p> </td>
<td> 鄂府办发〔2026〕38号 </td>
<td> 2026-08-31 </td>
<td> 2026-08-31 </td>
</tr><tr><td></td><td></td><td></td><td></td><td></td></tr></tbody></table>
"""

RIGHTLIST_HTML = """
<ul class="d_list_ul clearfix" id="d_list_ul">
<li><div class="right_l rightNum"><span>1</span></div>
<div class="right_l rightTitle"><a href="./202608/t20260821_3914409.html" target="_blank" style="font-size:15px">鄂尔多斯市财政局关于下达2026年市级财政衔接推进乡村振兴补助资金（少数民族发展任务）的通知</a></div>
<div class="right_l rightTime"><span class="zcxx_tim">2026-03-20</span></div></li>
<li class="d_show_list_a">规章</li>
</ul>
"""

ZYK_HTML = """
<table class="tab-xxgk"><tbody>
<tr class="btou"><td class="syh" align="center">索引号</td><td class="bt" align="center">标题</td>
<td class="wh" align="center">文号</td><td class="cwrq" align="center">成文日期</td></tr>
<tr><td align="center"> 000014349/2026-00473 </td>
<td> <a href="./qq_yjhlq_10367/qq_yjhlq_0237/202609/t20260930_3918017.html">伊金霍洛旗人民政府办公室关于调整全旗住宅小区包联单位的通知</a> </td>
<td align="center"> 伊政办发〔2026〕49号 </td><td align="center"> 2026-08-24 </td></tr>
</tbody></table>
"""

RULESLIST_HTML = """
<div class="rules-list"><ul><li class="clearfix"><span class="num l">1</span>
<div class="title l"> <p><a href="./202601/t20260106_3866973.html">鄂尔多斯市人民政府令第4号 鄂尔多斯市民用爆炸物品安全管理暂行办法</a></p><i></i> </div>
<div class="download r"><a href="" download>下载文字版</a></div></li></ul></div>
"""

PAGINATION_HTML = """
<script>var currentPage = 0;//所在页从0开始
var countPage = 105//共多少页
var countRecord = 1363//共多少条</script>
"""

BG_SL_VALUES = [
    "索 引 号", "000014349/2026-00469",
    "发文字号", "鄂府办发〔2026〕38号",
    "成文日期", "2026-08-31",
    "发文机关", "市人民政府办公室",
    "信息分类", "公安、安全、司法  通知  鄂府办发",
    "公开日期", "2026-08-31",
]


def test_declared_pagination_reads_inline_script():
    assert declared_pagination(PAGINATION_HTML) == (105, 1363)
    assert declared_pagination("<html>无分页声明</html>") == (None, None)


def test_rules_page_takes_pc_block_not_mobile():
    # 规章栏目同时写了两段 countPage，PC 段在前且只有一页。
    html = "<script>var countPage = 1</script><script>var countPage = 6</script>"
    assert declared_pagination(html) == (1, None)


def test_page_url_uses_zero_based_static_paging():
    target = SCAN_TARGETS["ordos_policy"]
    assert page_url(target.list_url, 1) == "https://www.ordos.gov.cn/ordosml/ordoszf/4033/"
    assert page_url(target.list_url, 2) == "https://www.ordos.gov.cn/ordosml/ordoszf/4033/index_1.html"
    assert page_url(target.list_url, 105) == "https://www.ordos.gov.cn/ordosml/ordoszf/4033/index_104.html"


async def test_resumed_batch_still_reports_declared_column_total():
    # 从末页位置恢复时本轮一条都不产出，总量必须仍是站点声明值，否则收尾修订永远判负。
    collector = OrdosZykCollector(None, None, SCAN_TARGETS["ordos_policy"])
    collector._declared_records = 1363
    collector._discovered = True
    collector._exhausted = True
    collector._enumerated = 0
    assert await collector.estimated_total() == 1363


async def test_column_without_declared_records_counts_fetched_pages():
    # 规章栏目不写 countRecord：本轮只产出尾部一条时，仍按已读到的分页条目数计总量。
    collector = OrdosZykCollector(None, None, SCAN_TARGETS["ordos_regulations"])
    items = parse_ordos_list_page(RIGHTLIST_HTML, 1, SCAN_TARGETS["ordos_local_regulations"])
    collector._pages = {1: items, 2: items}
    collector._discovered = True
    collector._exhausted = True
    collector._enumerated = 1
    assert await collector.estimated_total() == 2 * len(items)


async def test_column_total_before_any_pass_is_known():
    collector = OrdosZykCollector(None, None, SCAN_TARGETS["ordos_policy"])
    collector._declared_records = 1363
    assert await collector.estimated_total() == 1363


class _FakeResponse:
    def __init__(self, status, text):
        self.status = status
        self._text = text

    async def text(self):
        return self._text

    async def dispose(self):
        return None


class _FakeRequest:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    async def get(self, url, **_kwargs):
        self.calls.append(url)
        status, text = self.pages.pop(0) if self.pages else (404, "")
        return _FakeResponse(status, text)


def _list_collector(pages):
    from types import SimpleNamespace

    from app.config import SafetyConfig
    from app.safety import SafetyController

    async def _no_sleep(_seconds):
        return None

    collector = OrdosZykCollector(
        None, SafetyController(SafetyConfig(), sleep=_no_sleep, rng=random.Random(1)),
        SCAN_TARGETS["ordos_policy"],
    )
    collector.context = SimpleNamespace(request=_FakeRequest(pages))
    return collector


async def test_page_beyond_declared_count_ends_list_instead_of_pausing():
    collector = _list_collector([(200, "<script>var countPage = 2</script>")])
    collector._declared_pages = 2
    assert await collector._fetch_page(3) == []


async def test_empty_mid_page_is_refetched_before_it_pauses_the_job():
    # 瞬态风控页会伪装成一张 200 的空列表；重取一次拿到条目就不该停整批扫描。
    collector = _list_collector([(200, "<html>加载中</html>"), (200, TABLE1_HTML)])
    collector._declared_pages = 105
    items = await collector._fetch_page(3)
    assert len(items) == 1
    assert collector.context.request.calls == [collector.list_url + "index_2.html"] * 2


async def test_mid_page_still_empty_after_retry_pauses_for_collector_fix():
    collector = _list_collector([(200, "<html>加载中</html>"), (200, "<html>加载中</html>")])
    collector._declared_pages = 105
    with pytest.raises(SafetyPause):
        await collector._fetch_page(3)


async def test_gateway_5xx_on_list_page_is_refetched_once():
    # 伊金霍洛旗实测出现过列表第 3 页 504、首页 0.3 秒就恢复：不该为一次网关抖动冷却半小时。
    collector = _list_collector([(504, "<html>gateway</html>"), (200, TABLE1_HTML)])
    collector._declared_pages = 105
    items = await collector._fetch_page(3)
    assert len(items) == 1
    assert len(collector.context.request.calls) == 2


async def test_persistent_gateway_5xx_still_pauses_into_cooldown():
    collector = _list_collector([(504, ""), (504, "")])
    collector._declared_pages = 105
    with pytest.raises(SafetyPause, match="HTTP 504"):
        await collector._fetch_page(3)


def _served_pages(counts: dict[int, tuple[int, int]], *, from_first=True, to_end=True):
    """搭一个「走完声明分页、每页解析数=页面详情链接数」的采集器状态。"""
    from app.domain import PolicyListItem

    collector = OrdosZykCollector(None, None, SCAN_TARGETS["yjhl_valid_normative"])
    collector._declared_records = 27
    collector._declared_pages = 2
    for page, (parsed, _raw) in counts.items():
        collector._pages[page] = [
            PolicyListItem(
                district="伊金霍洛旗", page_number=page, item_index=index,
                title=f"文件{page}-{index}",
                url=f"https://www.yjhl.gov.cn/x/t20260101_{page}{index}.html",
                stable_id=f"yjhl_valid_normative|url:{page}/{index}",
            )
            for index in range(parsed)
        ]
    collector._page_link_counts = counts
    collector._walked_from_first = from_first
    collector._reached_declared_end = to_end
    return collector


async def test_overstated_declared_count_falls_back_to_measured_pages():
    # 伊金霍洛旗「现行有效规范性文件库」实测：标称 27 条，走完声明的 2 页只有 15 条。
    collector = _served_pages({1: (8, 8), 2: (7, 7)})
    assert await collector.estimated_total() == 15
    assert "标称 27" in collector.total_discrepancy
    assert "实际只发布 15" in collector.total_discrepancy


async def test_resumed_pass_never_overrides_the_declared_count():
    collector = _served_pages({1: (8, 8), 2: (7, 7)}, from_first=False)
    assert await collector.estimated_total() == 27
    assert collector.total_discrepancy == ""


async def test_unreached_declared_end_keeps_the_declared_count():
    collector = _served_pages({1: (8, 8), 2: (7, 7)}, to_end=False)
    assert await collector.estimated_total() == 27
    assert collector.total_discrepancy == ""


async def test_dropped_rows_are_blamed_on_the_collector_not_the_site():
    # 某页解析数少于页面真实详情链接数 = 采集器漏解析，不能反过来认定站点少发布。
    collector = _served_pages({1: (5, 8), 2: (7, 7)})
    assert await collector.estimated_total() == 27
    assert collector.total_discrepancy == ""


async def test_consistent_declared_count_reports_no_discrepancy():
    collector = _served_pages({1: (14, 14), 2: (13, 13)})
    assert await collector.estimated_total() == 27
    assert collector.total_discrepancy == ""


def test_table1_layout_skips_mobile_duplicate_cell():
    target = SCAN_TARGETS["ordos_policy"]
    items = parse_ordos_list_page(TABLE1_HTML, 1, target)
    assert len(items) == 1
    item = items[0]
    assert item.title == "鄂尔多斯市人民政府办公室关于《鄂尔多斯市地震应急预案》的补充通知"
    assert item.url == "https://www.ordos.gov.cn/ordosml/ordoszf/202609/t20260918_3917319.html"
    assert item.published_date == date(2026, 8, 31)
    assert item.source_key == "ordos_policy"
    assert item.source_channel_id == "/ordosml/ordoszf/4033/"
    assert item.stable_id == f"ordos_policy|url:{item.url}"


def test_rightlist_layout_carries_publish_time():
    target = SCAN_TARGETS["ordos_dept_sczj_n"]
    items = parse_ordos_list_page(RIGHTLIST_HTML, 3, target)
    assert len(items) == 1
    assert items[0].url == "https://www.ordos.gov.cn/ordosml/ordoszf/bmwj/sczj_n/202608/t20260821_3914409.html"
    assert items[0].published_date == date(2026, 3, 20)
    assert items[0].page_number == 3
    assert items[0].district == "鄂尔多斯市"


def test_zyk_layout_maps_ejin_horo_columns():
    target = SCAN_TARGETS["yjhl_policy"]
    items = parse_ordos_list_page(ZYK_HTML, 1, target)
    assert len(items) == 1
    assert items[0].title.startswith("伊金霍洛旗人民政府办公室")
    assert items[0].url.startswith("https://www.yjhl.gov.cn/yqxxgk_zyk/qq_yjhlq_10367/")
    assert items[0].published_date == date(2026, 8, 24)
    assert items[0].district == "伊金霍洛旗"


def test_ruleslist_layout_has_no_list_date():
    target = SCAN_TARGETS["ordos_regulations"]
    items = parse_ordos_list_page(RULESLIST_HTML, 1, target)
    assert len(items) == 1
    assert items[0].published_date is None
    assert items[0].url == "https://www.ordos.gov.cn/ordosml/ordoszf/gz/202601/t20260106_3866973.html"


def test_unknown_list_layout_pauses_task():
    broken = SCAN_TARGETS["ordos_policy"].__class__(**{**SCAN_TARGETS["ordos_policy"].__dict__, "list_layout": ""})
    with pytest.raises(SafetyPause, match="采集器需要更新"):
        parse_ordos_list_page(TABLE1_HTML, 1, broken)


def test_pair_values_normalizes_site_label_spelling():
    values = pair_values(BG_SL_VALUES)
    assert values["索引号"] == "000014349/2026-00469"
    assert values["文号"] == "鄂府办发〔2026〕38号"
    assert values["发布机构"] == "市人民政府办公室"
    assert values["发布日期"] == "2026-08-31"
    assert values["主题分类"].startswith("公安、安全、司法")


def test_pair_values_strips_label_colons_on_ejin_horo_template():
    """伊金霍洛旗详情表标签带冒号，不去掉就一项都匹配不上（曾整栏判成无表头 PASS）。"""
    values = pair_values([
        "名称:", "伊金霍洛旗人民政府办公室关于调整全旗住宅小区包联单位的通知",
        "索引号:", "000014349/2026-00473",
        "分类:", "自然资源、能源;城乡建设、环境保护 其它",
        "发布机构:", "伊金霍洛旗人民政府办公室",
        "成文日期:", "2026年08月24日",
        "文号:", "伊政办发〔2026〕49号",
        "时效性：", "有效",
        "发布日期:", "2026年08月24日",
    ])
    assert header_fields(values) == ([], [])
    assert values["文号"] == "伊政办发〔2026〕49号"
    assert values["主题分类"].startswith("自然资源、能源")


@pytest.mark.parametrize("raw,expected", [
    ("2026-08-24", date(2026, 8, 24)),
    ("2026年08月24日", date(2026, 8, 24)),
    ("2026年8月4日", date(2026, 8, 4)),
    ("2019-02-05 00:00", date(2019, 2, 5)),
    ("二〇二六年八月二十四日", None),
    ("", None),
])
def test_parse_ordos_date_accepts_both_formats(raw, expected):
    assert parse_ordos_date(raw) == expected


def test_header_fields_reports_missing_and_invalid():
    values = pair_values(BG_SL_VALUES)
    assert header_fields(values) == ([], [])
    partial = {key: values[key] for key in ("索引号", "文号", "成文日期", "发布机构")}
    assert header_fields(partial) == (["发布日期"], [])
    bad_date = {**partial, "发布日期": "二〇二六年八月三十一日"}
    assert header_fields(bad_date) == ([], ["发布日期（格式无效）"])


def test_gfxwj_detail_uses_label_aliases_and_info_line_date():
    values = pair_values([
        "索 引 号", "11152700117272136 /2019-2561314", "主题分类", "卫生、体育",
        "发布机构", "市人民政府办公室", "文 号", "鄂府办发〔2019〕5号",
        "成文日期", "2019-01-29", "公文时效", "有效",
    ])
    values["发布日期"] = "2019-02-05"
    assert header_fields(values) == ([], [])
    assert values["文号"] == "鄂府办发〔2019〕5号"


def test_ordos_sites_and_targets_configuration():
    assert len(ORDOS_DEPARTMENTS) == 30
    library = targets_for_site("ordos_municipal")
    assert [t.key for t in library] == [
        "ordos_policy", "ordos_regulations", "ordos_local_regulations",
        "ordos_valid_normative", "ordos_invalid_normative",
    ]
    for target in library + targets_for_site("ordos_departments"):
        assert target.collector_type == "ordos"
        assert target.host == "www.ordos.gov.cn"
        assert target.district == "鄂尔多斯市"
        assert target.list_url.startswith(f"https://www.ordos.gov.cn{target.channel_id}")
    assert [t.source_name for t in targets_for_site("ordos_departments")] == ["部门文件"] * 30
    assert resolve_target("ordos_dept_sczj_n").label == "地市级网站·鄂尔多斯市·部门文件·市财政局"
    assert resolve_target("ordos_dept_sfzhggwyh_n").channel_id == "/ordosml/ordoszf/bmwj/sfzhggwyh_n/"

    banner = targets_for_site("yjhl_banner")
    assert [t.key for t in banner] == ["yjhl_policy", "yjhl_valid_normative"]
    for target in banner:
        assert target.host == "www.yjhl.gov.cn"
        assert target.district == "伊金霍洛旗"
        assert target.source_level == "区级"

    for key in ("ordos_municipal", "ordos_departments", "yjhl_banner"):
        site = SCAN_SITES[key]
        assert site.province == "内蒙古自治区"
        assert site.city == "鄂尔多斯市"


def test_department_columns_are_unique():
    slugs = [slug for slug, _name in ORDOS_DEPARTMENTS]
    assert len(set(slugs)) == len(slugs)
    names = [name for _slug, name in ORDOS_DEPARTMENTS]
    assert len(set(names)) == len(names)


def test_ordos_host_lock_is_per_site():
    from app.jobs import JobManager

    class _DB:
        def initialize(self):
            return None

    manager = JobManager(_DB())
    safety = object()
    collector = manager._collector_for_target(None, safety, SCAN_TARGETS["ordos_policy"])
    assert isinstance(collector, OrdosZykCollector)
    assert collector.rendered_hosts == {"www.ordos.gov.cn"}
    yjhl = manager._collector_for_target(None, safety, SCAN_TARGETS["yjhl_policy"])
    assert yjhl.rendered_hosts == {"www.yjhl.gov.cn"}
