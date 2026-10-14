from __future__ import annotations

import os

from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
EXPORT_DIR = DATA_DIR / "exports"
DB_PATH = DATA_DIR / "policy_inspector.db"
TARGET_URL = "https://www.shanghai.gov.cn/zhengce/more?level=district&siteId=all"
PUTUO_DISTRICT_URL = "https://www.shpt.gov.cn/zhengwu/qzfwj-zfwj/index.html"

@dataclass(frozen=True)
class ScanTarget:
    key: str
    label: str
    district: str
    source_level: str
    source_name: str
    collector_type: str = "municipal"
    channel_id: str = ""
    list_url: str = ""
    host: str = ""
    # 鄂尔多斯各栏目共用一套资源库采集器，靠这两个字段选择列表/详情页解析模板。
    list_layout: str = ""
    detail_layout: str = ""


@dataclass(frozen=True)
class ScanSite:
    key: str
    label: str
    district: str
    source_level: str
    source_name: str
    target_keys: tuple[str, ...]
    host: str
    province: str = "上海市"
    city: str = "上海市"


# 上海市 16 个行政区县（市级政策文件库 siteId，已与 gwk/policy/page 核对）
MUNICIPAL_DISTRICTS: tuple[tuple[str, str, str], ...] = (
    ("municipal_pudong", "浦东新区", "0070"),
    ("municipal_huangpu", "黄浦区", "0071"),
    ("municipal_xuhui", "徐汇区", "0073"),
    ("municipal_changning", "长宁区", "0074"),
    ("municipal_jingan", "静安区", "0072"),
    ("municipal_putuo", "普陀区", "0075"),
    ("municipal_hongkou", "虹口区", "0076"),
    ("municipal_yangpu", "杨浦区", "0077"),
    ("municipal_minhang", "闵行区", "0079"),
    ("municipal_baoshan", "宝山区", "0078"),
    ("municipal_jiading", "嘉定区", "0080"),
    ("municipal_jinshan", "金山区", "0081"),
    ("municipal_songjiang", "松江区", "0082"),
    ("municipal_qingpu", "青浦区", "0083"),
    ("municipal_fengxian", "奉贤区", "0084"),
    ("municipal_chongming", "崇明区", "0085"),
)


def _municipal_target(key: str, district: str) -> ScanTarget:
    return ScanTarget(
        key, f"市级平台·{district}", district, "市级", "上海市政策文件库",
        host="www.shanghai.gov.cn",
    )


def _municipal_site(key: str, district: str) -> ScanSite:
    return ScanSite(
        key, f"市级平台·{district}", district, "市级", "上海市政策文件库",
        (key,), "www.shanghai.gov.cn",
    )


ORDOS_HOST = "www.ordos.gov.cn"
YJHL_HOST = "www.yjhl.gov.cn"
ORDOS_PROVINCE = "内蒙古自治区"
ORDOS_CITY = "鄂尔多斯市"

# 鄂尔多斯市政务公开“部门文件”栏目下的 30 个部门子站目录（与官网 bmwj 菜单一致）。
# 官网无“市人民政府办公室”“市人民政府外事办公室”独立子栏目：前者文件在“政策文件”栏目，
# 后者未单独开栏；“市农牧业局”即需求清单中的“市农牧局”。
ORDOS_DEPARTMENTS: tuple[tuple[str, str], ...] = (
    ("sfzhggwyh_n", "市发展和改革委员会"),
    ("sgyhxxhj", "市工业和信息化局"),
    ("sjytyj", "市教育体育局"),
    ("skxjsj_n", "市科学技术局"),
    ("smzswwyh_n", "市民族事务委员会"),
    ("smzj_n", "市民政局"),
    ("sczj_n", "市财政局"),
    ("srlzyhshbzj_n", "市人力资源和社会保障局"),
    ("szrzyj", "市自然资源局"),
    ("szfhcxjsj", "市住房和城乡建设局"),
    ("ssthjj", "市生态环境局"),
    ("sjtysj_n", "市交通运输局"),
    ("sslj", "市水利局"),
    ("snmyj_n", "市农牧业局"),
    ("slyhcyj", "市林业和草原局"),
    ("sswj_n", "市商务局"),
    ("swhhlyj", "市文化和旅游局"),
    ("ssjj_n", "市审计局"),
    ("syjglj", "市应急管理局"),
    ("stjj_n", "市统计局"),
    ("snyj", "市能源局"),
    ("sxfj", "市信访局"),
    ("sgyzcjdglwyh", "市国有资产监督管理委员会"),
    ("swshjkwyh", "市卫生健康委员会"),
    ("sgaj_n", "市公安局"),
    ("ssfj_n", "市司法局"),
    ("styjrswj", "市退役军人事务局"),
    ("sscjdglj", "市市场监督管理局"),
    ("sylbzj", "市医疗保障局"),
    ("szwfwj", "市行政审批政务服务与数据管理局"),
)


def _ordos_target(
    key: str, column: str, district: str, path: str, source_name: str,
    list_layout: str, detail_layout: str, host: str = ORDOS_HOST,
) -> ScanTarget:
    prefix = "地市级网站·鄂尔多斯市" if host == ORDOS_HOST else "区级网站·伊金霍洛旗"
    return ScanTarget(
        key, f"{prefix}·{column}", district, "市级" if host == ORDOS_HOST else "区级",
        source_name, "ordos", path, f"https://{host}{path}", host,
        list_layout, detail_layout,
    )


def _ordos_department_target(slug: str, name: str) -> ScanTarget:
    return _ordos_target(
        f"ordos_dept_{slug}", f"部门文件·{name}", ORDOS_CITY,
        f"/ordosml/ordoszf/bmwj/{slug}/", "部门文件", "rightlist", "bg_sl",
    )


SCAN_TARGETS = {
    **{
        key: _municipal_target(key, district)
        for key, district, _site_id in MUNICIPAL_DISTRICTS
    },
    "putuo_government": ScanTarget(
        "putuo_government", "区级网站·普陀区·区政府文件", "普陀区", "区级", "区政府文件",
        "putuo", "3", "https://www.shpt.gov.cn/zhengwu/qzfwj-zfwj/index.html",
        "www.shpt.gov.cn",
    ),
    "putuo_bureaus": ScanTarget(
        "putuo_bureaus", "区级网站·普陀区·委办局", "普陀区", "区级", "委办局",
        "putuo", "6", "https://www.shpt.gov.cn/zhengwu/wbj-zfwj/index.html",
        "www.shpt.gov.cn",
    ),
    "putuo_towns": ScanTarget(
        "putuo_towns", "区级网站·普陀区·街道镇", "普陀区", "区级", "街道镇",
        "putuo", "1225", "https://www.shpt.gov.cn/zhengwu/jdz-zfwj/index.html",
        "www.shpt.gov.cn",
    ),
    "putuo_normative": ScanTarget(
        "putuo_normative", "区级网站·普陀区·规范性文件", "普陀区", "区级", "规范性文件",
        "putuo", "1614", "https://www.shpt.gov.cn/zhengwu/gfxwj-zfwj/index.html",
        "www.shpt.gov.cn",
    ),
    "putuo_party_government": ScanTarget(
        "putuo_party_government", "区级网站·普陀区·党政混合信息", "普陀区", "区级", "党政混合信息",
        "putuo", "1621", "https://www.shpt.gov.cn/zhengwu/dzhhxx-zfwj/index.html",
        "www.shpt.gov.cn",
    ),
    # 江苏省·淮安市政策文件库（zfwjKey 区分市政府文件/部门文件/县区文件三类）
    "huaian_government": ScanTarget(
        "huaian_government", "地市级网站·淮安市·市政府文件", "淮安市", "市级", "市政府文件",
        "huaian", "市政府文件", "https://www.huaian.gov.cn/cmsweb/zwgk/zcwj/m/index.html",
        "www.huaian.gov.cn",
    ),
    "huaian_bureaus": ScanTarget(
        "huaian_bureaus", "地市级网站·淮安市·部门文件", "淮安市", "市级", "部门文件",
        "huaian", "bm", "https://www.huaian.gov.cn/cmsweb/zwgk/zcwj/m/index.html",
        "www.huaian.gov.cn",
    ),
    "huaian_counties": ScanTarget(
        "huaian_counties", "地市级网站·淮安市·县区文件", "淮安市", "市级", "县区文件",
        "huaian", "xq", "https://www.huaian.gov.cn/cmsweb/zwgk/zcwj/m/index.html",
        "www.huaian.gov.cn",
    ),
    # 江苏省·宿迁市政策文件库（单发 JSONP 全量列表，含市级/县区部门/宿豫三类）
    "suqian_zcwj": ScanTarget(
        "suqian_zcwj", "地市级网站·宿迁市·政策文件库", "宿迁市", "市级", "政策文件库",
        "suqian", "", "https://www.suqian.gov.cn/zcwjk/index.shtml",
        "www.suqian.gov.cn",
    ),
    # 江苏省·宿迁市·市政府文件（信息公开体系，静态分页，约1005条）
    "suqian_szfwj": ScanTarget(
        "suqian_szfwj", "地市级网站·宿迁市·市政府文件", "宿迁市", "市级", "市政府文件",
        "suqian", "szfwj", "https://www.suqian.gov.cn/cnsq/szfwj/xxgk_list.shtml",
        "www.suqian.gov.cn",
    ),
    # 内蒙古自治区·鄂尔多斯市政务公开“文件库”（宏科 was40 资源库，index_N.html 静态分页）
    "ordos_policy": _ordos_target(
        "ordos_policy", "政策文件", ORDOS_CITY, "/ordosml/ordoszf/4033/",
        "政策文件库", "table1", "bg_sl",
    ),
    "ordos_regulations": _ordos_target(
        "ordos_regulations", "规章", ORDOS_CITY, "/ordosml/ordoszf/gz/",
        "政策文件库", "ruleslist", "rules",
    ),
    "ordos_local_regulations": _ordos_target(
        "ordos_local_regulations", "地方性法规", ORDOS_CITY, "/ordosml/ordoszf/dfxfg/",
        "政策文件库", "rightlist", "plain",
    ),
    "ordos_valid_normative": _ordos_target(
        "ordos_valid_normative", "现行有效规范性文件库", ORDOS_CITY,
        "/ordosml/xzgfxwj_153966/xxyxxgfxwj/", "政策文件库", "table1", "gfxwj_table",
    ),
    "ordos_invalid_normative": _ordos_target(
        "ordos_invalid_normative", "废止失效规范性文件", ORDOS_CITY,
        "/ordosml/xzgfxwj_153966/fzsxgfxwj/", "政策文件库", "table1", "gfxwj_table",
    ),
    **{
        f"ordos_dept_{slug}": _ordos_department_target(slug, name)
        for slug, name in ORDOS_DEPARTMENTS
    },
    # 内蒙古自治区·鄂尔多斯市·伊金霍洛旗（同族资源库，列表表头为索引号/标题/文号/成文日期）
    "yjhl_policy": _ordos_target(
        "yjhl_policy", "政策文件", "伊金霍洛旗", "/yqxxgk_zyk/",
        "政策文件资源库", "zyk_table", "tx_content", YJHL_HOST,
    ),
    "yjhl_valid_normative": _ordos_target(
        "yjhl_valid_normative", "现行有效规范性文件库", "伊金霍洛旗",
        "/yqxxgk_zyk/gfxwj/xxyxgfxwjk/", "政策文件资源库", "zyk_table", "tx_content", YJHL_HOST,
    ),
}


# 区级单独成组；市级 16 区按官方惯用顺序排列，供界面与 API 一致使用。
SCAN_SITES = {
    "putuo_district": ScanSite(
        "putuo_district", "区级网站·普陀区", "普陀区", "区级", "政策文件（五类栏目）",
        (
            "putuo_government",
            "putuo_bureaus",
            "putuo_towns",
            "putuo_normative",
            "putuo_party_government",
        ),
        "www.shpt.gov.cn",
    ),
    **{
        key: _municipal_site(key, district)
        for key, district, _site_id in MUNICIPAL_DISTRICTS
    },
    "jiangsu_huaian": ScanSite(
        "jiangsu_huaian", "地市级网站·淮安市", "淮安市", "市级", "政策文件库（三类栏目）",
        ("huaian_government", "huaian_bureaus", "huaian_counties"),
        "www.huaian.gov.cn", "江苏省", "淮安市",
    ),
    "jiangsu_suqian": ScanSite(
        "jiangsu_suqian", "地市级网站·宿迁市", "宿迁市", "市级", "政策文件库",
        ("suqian_zcwj", "suqian_szfwj"),
        "www.suqian.gov.cn", "江苏省", "宿迁市",
    ),
    "ordos_municipal": ScanSite(
        "ordos_municipal", "地市级网站·鄂尔多斯市", ORDOS_CITY, "市级",
        "文件库（政策文件等五类栏目）",
        (
            "ordos_policy",
            "ordos_regulations",
            "ordos_local_regulations",
            "ordos_valid_normative",
            "ordos_invalid_normative",
        ),
        ORDOS_HOST, ORDOS_PROVINCE, ORDOS_CITY,
    ),
    "ordos_departments": ScanSite(
        "ordos_departments", "地市级网站·鄂尔多斯市·部门文件", ORDOS_CITY, "市级",
        f"部门文件（{len(ORDOS_DEPARTMENTS)} 个部门栏目）",
        tuple(f"ordos_dept_{slug}" for slug, _name in ORDOS_DEPARTMENTS),
        ORDOS_HOST, ORDOS_PROVINCE, ORDOS_CITY,
    ),
    "yjhl_banner": ScanSite(
        "yjhl_banner", "区级网站·伊金霍洛旗", "伊金霍洛旗", "区级", "政策文件资源库",
        ("yjhl_policy", "yjhl_valid_normative"),
        YJHL_HOST, ORDOS_PROVINCE, ORDOS_CITY,
    ),
}

# 保留给旧数据和测试使用的区县映射；新增任务使用 SCAN_TARGETS 的 key。
DISTRICTS = {key: target.label for key, target in SCAN_TARGETS.items()}

DISTRICT_SITE_IDS = {
    district: site_id for _key, district, site_id in MUNICIPAL_DISTRICTS
}


def resolve_target(value: str) -> ScanTarget:
    """兼容历史任务中的区县名称，统一解析为可扫描来源。"""
    if value in SCAN_TARGETS:
        return SCAN_TARGETS[value]
    for target in SCAN_TARGETS.values():
        if value == target.label:
            return target
    legacy = {
        "普陀区": "municipal_putuo",
        "崇明区": "municipal_chongming",
        "区级网站·普陀区": "putuo_government",
    }
    if value in legacy:
        return SCAN_TARGETS[legacy[value]]
    raise ValueError(f"不支持的扫描目标：{value}")


def resolve_site(value: str) -> ScanSite:
    try:
        return SCAN_SITES[value]
    except KeyError as exc:
        raise ValueError(f"不支持的扫描站点：{value}") from exc


def targets_for_site(value: str) -> list[ScanTarget]:
    site = resolve_site(value)
    return [SCAN_TARGETS[key] for key in site.target_keys]


def district_sites() -> list[ScanSite]:
    return [site for site in SCAN_SITES.values() if site.source_level == "区级"]


def municipal_sites() -> list[ScanSite]:
    return [
        site for site in SCAN_SITES.values()
        if site.source_level == "市级" and site.province == "上海市"
    ]


@dataclass(frozen=True)
class SafetyConfig:
    min_delay_seconds: float = 3.0
    max_delay_seconds: float = 3.0
    rest_every_pages: int = 50
    rest_min_seconds: float = 90.0
    rest_max_seconds: float = 90.0
    max_retries: int = 2
    max_run_seconds: float = 4 * 60 * 60
    cooldown_seconds: float = 30 * 60
    consecutive_failure_limit: int = 3

    def validate(self) -> None:
        if self.min_delay_seconds < 3:
            raise ValueError("最小访问间隔不得低于 3 秒")
        if self.max_delay_seconds < self.min_delay_seconds:
            raise ValueError("最大访问间隔不得小于最小访问间隔")
        if self.rest_every_pages > 50 or self.rest_every_pages < 1:
            raise ValueError("每轮连续访问页数不得超过 50")
        if self.rest_min_seconds < 90:
            raise ValueError("强制休息不得少于 90 秒")


def ensure_directories() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class ContinuousScanConfig:
    """持续增量发现与完整列表对账调度配置。"""
    incremental_interval_hours: float = 6.0
    full_reconcile_interval_days: float = 7.0
    max_catchup_rounds: int = 3
    catchup_empty_rounds_to_finish: int = 2
    failure_retry_minutes: int = 15
    site_keys: tuple[str, ...] = ()
    enabled: bool = False

    @classmethod
    def from_env(cls) -> "ContinuousScanConfig":
        """Read opt-in continuous-scan settings without scheduling at service startup."""
        def flag(name: str, default: bool) -> bool:
            return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}

        def number(name: str, default: float, cast):
            raw = os.getenv(name)
            return cast(raw) if raw not in (None, "") else default

        site_keys = tuple(key.strip() for key in os.getenv("CONTINUOUS_SCAN_SITE_KEYS", "").split(",") if key.strip())
        config = cls(
            incremental_interval_hours=number("CONTINUOUS_SCAN_INCREMENTAL_HOURS", 6.0, float),
            full_reconcile_interval_days=number("CONTINUOUS_SCAN_FULL_RECONCILE_DAYS", 7.0, float),
            max_catchup_rounds=number("CONTINUOUS_SCAN_MAX_CATCHUP_ROUNDS", 3, int),
            catchup_empty_rounds_to_finish=number("CONTINUOUS_SCAN_EMPTY_CATCHUP_ROUNDS", 2, int),
            failure_retry_minutes=number("CONTINUOUS_SCAN_FAILURE_RETRY_MINUTES", 15, int),
            site_keys=site_keys,
            enabled=flag("CONTINUOUS_SCAN_ENABLED", False),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.incremental_interval_hours < 1:
            raise ValueError("增量发现间隔不得低于 1 小时")
        if self.full_reconcile_interval_days < 1:
            raise ValueError("完整对账间隔不得低于 1 天")
        if self.max_catchup_rounds < 1:
            raise ValueError("追赶轮数至少为 1")
        if self.failure_retry_minutes < 1:
            raise ValueError('failure retry interval must be at least one minute')
        if self.catchup_empty_rounds_to_finish < 1:
            raise ValueError("结束前无新增轮数至少为 1")
