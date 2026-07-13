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


SCAN_TARGETS = {
    "municipal_putuo": ScanTarget("municipal_putuo", "市级平台·普陀区", "普陀区", "市级", "上海市政策文件库"),
    "municipal_chongming": ScanTarget("municipal_chongming", "市级平台·崇明区", "崇明区", "市级", "上海市政策文件库"),
    "putuo_district": ScanTarget("putuo_district", "区级网站·普陀区", "普陀区", "区级", "普陀区政府网站"),
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
    legacy = {"普陀区": "municipal_putuo", "崇明区": "municipal_chongming"}
    if value in legacy:
        return SCAN_TARGETS[legacy[value]]
    raise ValueError(f"不支持的扫描目标：{value}")


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
