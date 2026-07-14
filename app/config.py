from __future__ import annotations

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


@dataclass(frozen=True)
class ScanSite:
    key: str
    label: str
    district: str
    source_level: str
    source_name: str
    target_keys: tuple[str, ...]
    host: str


SCAN_TARGETS = {
    "municipal_putuo": ScanTarget("municipal_putuo", "市级平台·普陀区", "普陀区", "市级", "上海市政策文件库"),
    "municipal_chongming": ScanTarget("municipal_chongming", "市级平台·崇明区", "崇明区", "市级", "上海市政策文件库"),
    "putuo_government": ScanTarget(
        "putuo_government", "区级网站·普陀区·区政府文件", "普陀区", "区级", "区政府文件",
        "putuo", "3", "https://www.shpt.gov.cn/zhengwu/qzfwj-zfwj/index.html",
    ),
    "putuo_bureaus": ScanTarget(
        "putuo_bureaus", "区级网站·普陀区·委办局", "普陀区", "区级", "委办局",
        "putuo", "6", "https://www.shpt.gov.cn/zhengwu/wbj-zfwj/index.html",
    ),
    "putuo_towns": ScanTarget(
        "putuo_towns", "区级网站·普陀区·街道镇", "普陀区", "区级", "街道镇",
        "putuo", "1225", "https://www.shpt.gov.cn/zhengwu/jdz-zfwj/index.html",
    ),
    "putuo_normative": ScanTarget(
        "putuo_normative", "区级网站·普陀区·规范性文件", "普陀区", "区级", "规范性文件",
        "putuo", "1614", "https://www.shpt.gov.cn/zhengwu/gfxwj-zfwj/index.html",
    ),
    "putuo_party_government": ScanTarget(
        "putuo_party_government", "区级网站·普陀区·党政混合信息", "普陀区", "区级", "党政混合信息",
        "putuo", "1621", "https://www.shpt.gov.cn/zhengwu/dzhhxx-zfwj/index.html",
    ),
}


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
    "municipal_putuo": ScanSite(
        "municipal_putuo", "市级平台·普陀区", "普陀区", "市级", "上海市政策文件库",
        ("municipal_putuo",), "www.shanghai.gov.cn",
    ),
    "municipal_chongming": ScanSite(
        "municipal_chongming", "市级平台·崇明区", "崇明区", "市级", "上海市政策文件库",
        ("municipal_chongming",), "www.shanghai.gov.cn",
    ),
}

# 保留给旧数据和测试使用的区县映射；新增任务使用 SCAN_TARGETS 的 key。
DISTRICTS = {key: target.label for key, target in SCAN_TARGETS.items()}

DISTRICT_SITE_IDS = {
    "普陀区": "0075",
    "崇明区": "0085",
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


@dataclass(frozen=True)
class SafetyConfig:
    min_delay_seconds: float = 5.0
    max_delay_seconds: float = 10.0
    rest_every_pages: int = 50
    rest_min_seconds: float = 180.0
    rest_max_seconds: float = 300.0
    max_retries: int = 2
    max_run_seconds: float = 4 * 60 * 60
    cooldown_seconds: float = 30 * 60
    consecutive_failure_limit: int = 3

    def validate(self) -> None:
        if self.min_delay_seconds < 5:
            raise ValueError("最小访问间隔不得低于 5 秒")
        if self.max_delay_seconds < self.min_delay_seconds:
            raise ValueError("最大访问间隔不得小于最小访问间隔")
        if self.rest_every_pages > 50 or self.rest_every_pages < 1:
            raise ValueError("每轮连续访问页数不得超过 50")
        if self.rest_min_seconds < 180:
            raise ValueError("强制休息不得少于 3 分钟")


def ensure_directories() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
