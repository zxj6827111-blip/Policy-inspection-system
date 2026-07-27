from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from chinese_calendar import is_workday

from app.config import EXPORT_DIR, ensure_directories, resolve_target
from app.db import Database
from app.rules import DOC_NUMBER_RE, WorkdayCalendar, normalize_document_number
from app.findings_model import enrich_finding_row, is_customer_formal_problem


HEAD_FILL = PatternFill("solid", fgColor="1F4E78")
ALERT_FILL = PatternFill("solid", fgColor="FCE4D6")


def _excel_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except (TypeError, ValueError):
        return value


def _workday_count(detail: str):
    match = re.search(r"相隔\s*(\d+)\s*个工作日", detail or "")
    return int(match.group(1)) if match else None


def _document_number_parts(value: str) -> tuple[str, int | None, int | None, str]:
    normalized = normalize_document_number(value or "")
    match = DOC_NUMBER_RE.search(normalized)
    if not match:
        return "", None, None, normalized
    return match.group(1), int(match.group(2)), int(match.group(3)), normalized


def _timeliness(authored, published) -> tuple[int | None, str]:
    if not authored or not published:
        return None, "待复核"
    if published < authored:
        return 0, "日期倒置"
    try:
        count = WorkdayCalendar(provider=is_workday).count_between(authored, published)
    except NotImplementedError:
        count = WorkdayCalendar().count_between(authored, published)
    return count, "超期" if count > 20 else "合规"


def _finding_export_flags(f: dict) -> tuple:
    return (
        f.get("machine_status") or "",
        f.get("human_status") or "",
        f.get("rule_version") or "",
        "是" if f.get("is_customer_formal") else "否",
    )


def _format_sheet(sheet) -> None:

    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for cell in sheet[1]:
        cell.fill = HEAD_FILL
        cell.font = Font(color="FFFFFF", bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for column in range(1, sheet.max_column + 1):
        values = [str(sheet.cell(row, column).value or "") for row in range(1, min(sheet.max_row, 100) + 1)]
        width = min(max(max((len(v) for v in values), default=8) + 2, 10), 48)
        sheet.column_dimensions[get_column_letter(column)].width = width
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            if isinstance(cell.value, (datetime,)):
                cell.number_format = "yyyy-mm-dd hh:mm:ss"
            elif hasattr(cell.value, "year") and hasattr(cell.value, "month") and hasattr(cell.value, "day"):
                cell.number_format = "yyyy-mm-dd"
            elif isinstance(cell.value, int):
                cell.number_format = "0"


def _write_sheet(workbook: Workbook, name: str, headers: list[str], rows: list[tuple]) -> None:
    sheet = workbook.create_sheet(name)
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            if isinstance(cell.value, str) and cell.value.startswith(("http://", "https://")):
                cell.hyperlink = cell.value
                cell.style = "Hyperlink"
            if name in {"超期与日期问题", "文号与机构问题", "外链问题", "元数据问题",
                        "PDF 与附件问题", "异常归档问题", "重复数据问题", "双渠道对账差异",
                        "阅办联动问题", "媒体资源问题", "政策解读问题", "整改任务跟踪"}:
                cell.fill = ALERT_FILL
    _format_sheet(sheet)


def export_job(db: Database, job_id: int) -> Path:
    ensure_directories()
    with db.connect() as conn:
        job = conn.execute("SELECT * FROM scan_jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            raise ValueError("扫描任务不存在")
        findings = conn.execute(
            """SELECT f.*,d.district,d.source_id,d.source_site,d.title,d.url,d.issuing_agency,d.page_document_number,
            d.published_date,d.authored_date FROM rule_findings f JOIN policy_documents d ON d.id=f.document_id
            WHERE f.job_id=? ORDER BY d.district,d.id,f.id""", (job_id,)
        ).fetchall()
        links = conn.execute(
            """SELECT l.*,d.district,d.source_site,d.title,d.url AS document_url FROM link_checks l
            JOIN policy_documents d ON d.id=l.document_id
            WHERE l.job_id=? AND l.visible=1 AND l.review_status='confirmed'
              AND TRIM(COALESCE(l.source_area,''))!='' AND TRIM(COALESCE(l.source_page_url,''))!=''
              AND l.result IN ('broken','error')
            ORDER BY d.district,d.id,l.id""", (job_id,)
        ).fetchall()
        documents = conn.execute(
            """SELECT d.*,COALESCE(j.action,'processed') action,COALESCE(j.reason,'') reason,
            COALESCE(j.page_number,0) page_number,COALESCE(j.item_index,0) item_index,COUNT(f.id) finding_count
            ,COALESCE((SELECT raw_json FROM document_snapshots s WHERE s.document_id=d.id ORDER BY s.id DESC LIMIT 1),'{}') raw_json
            FROM policy_documents d LEFT JOIN scan_job_documents j ON d.id=j.document_id AND j.job_id=?
            LEFT JOIN rule_findings f ON f.document_id=d.id AND f.job_id=?
            WHERE j.id IS NOT NULL OR d.last_seen_job_id=?
            GROUP BY d.id,j.action,j.reason,j.page_number,j.item_index
            ORDER BY d.district,j.page_number,j.item_index""", (job_id, job_id, job_id)
        ).fetchall()
        item_results = conn.execute(
            """SELECT s.*,COALESCE(s.document_id,s.reused_document_id) AS effective_document_id
            FROM scan_item_results s WHERE s.job_id=?
            ORDER BY s.source_label,s.page_number,s.item_index""",
            (job_id,),
        ).fetchall()
        events = conn.execute(
            "SELECT * FROM job_events WHERE job_id=? ORDER BY id", (job_id,)
        ).fetchall()
        action_rows = conn.execute(
            """SELECT district,action,COUNT(*) count FROM scan_job_documents
            WHERE job_id=? GROUP BY district,action""", (job_id,)
        ).fetchall()
        try:
            archive_rows_raw = [dict(r) for r in conn.execute(
                """SELECT af.*, d.district, d.source_id, d.source_site, d.title, d.url
                FROM archive_findings af JOIN policy_documents d ON d.id=af.document_id
                WHERE af.document_id IN (SELECT document_id FROM scan_job_documents WHERE job_id=?)
                ORDER BY d.district, d.id, af.id""", (job_id,)
            ).fetchall()]
        except sqlite3.OperationalError:
            archive_rows_raw = []
        try:
            duplicate_rows_raw = [dict(r) for r in conn.execute(
                """SELECT dp.*, da.district AS district_a, da.source_id AS source_id_a,
                da.source_site AS source_site_a, da.title AS title_a, da.url AS url_a,
                db.source_id AS source_id_b, db.title AS title_b, db.url AS url_b
                FROM duplicate_pairs dp
                JOIN policy_documents da ON da.id=dp.document_id_a
                JOIN policy_documents db ON db.id=dp.document_id_b
                WHERE dp.document_id_a IN (SELECT document_id FROM scan_job_documents WHERE job_id=?)
                   OR dp.document_id_b IN (SELECT document_id FROM scan_job_documents WHERE job_id=?)
                ORDER BY dp.id""", (job_id, job_id)
            ).fetchall()]
        except sqlite3.OperationalError:
            duplicate_rows_raw = []
        try:
            recon_rows_raw = [dict(r) for r in conn.execute(
                """SELECT rm.*, m.district AS municipal_district, m.source_id AS municipal_source_id,
                m.source_site AS municipal_source_site, m.title AS municipal_title,
                d.district AS district_district, d.source_id AS district_source_id,
                d.source_site AS district_source_site, d.title AS district_title
                FROM reconciliation_matches rm
                JOIN policy_documents m ON m.id=rm.municipal_document_id
                LEFT JOIN policy_documents d ON d.id=rm.district_document_id
                WHERE rm.municipal_document_id IN (SELECT document_id FROM scan_job_documents WHERE job_id=?)
                   OR rm.district_document_id IN (SELECT document_id FROM scan_job_documents WHERE job_id=?)
                ORDER BY rm.id""", (job_id, job_id)
            ).fetchall()]
        except sqlite3.OperationalError:
            recon_rows_raw = []
        try:
            reading_rows_raw = [dict(r) for r in conn.execute(
                """SELECT rlc.*, d.district, d.source_id, d.source_site, d.title, d.url
                FROM reading_linkage_checks rlc JOIN policy_documents d ON d.id=rlc.document_id
                WHERE rlc.job_id=? AND rlc.rule_code != ''
                ORDER BY d.district, d.id, rlc.id""", (job_id,)
            ).fetchall()]
        except sqlite3.OperationalError:
            reading_rows_raw = []
        try:
            media_rows_raw = [dict(r) for r in conn.execute(
                """SELECT mc.*, d.district, d.source_id, d.source_site, d.title, d.url
                FROM media_checks mc JOIN policy_documents d ON d.id=mc.document_id
                WHERE mc.job_id=? AND mc.rule_code != ''
                ORDER BY d.district, d.id, mc.id""", (job_id,)
            ).fetchall()]
        except sqlite3.OperationalError:
            media_rows_raw = []
        try:
            interp_rows_raw = [dict(r) for r in conn.execute(
                """SELECT ic.*, pi.interpretation_type, pi.interpretation_url, pi.title AS interp_title,
                d.district, d.source_id, d.source_site, d.title AS doc_title
                FROM interpretation_checks ic
                JOIN policy_interpretations pi ON pi.id=ic.interpretation_id
                JOIN policy_documents d ON d.id=pi.document_id
                WHERE ic.job_id=? AND ic.rule_code != ''
                ORDER BY d.district, d.id, ic.id""", (job_id,)
            ).fetchall()]
        except sqlite3.OperationalError:
            interp_rows_raw = []
        try:
            rect_rows_raw = [dict(r) for r in conn.execute(
                """SELECT rt.*, d.district, d.source_id, d.source_site, d.title AS doc_title, d.url
                FROM rectification_tasks rt JOIN policy_documents d ON d.id=rt.document_id
                WHERE rt.document_id IN (SELECT document_id FROM scan_job_documents WHERE job_id=?)
                ORDER BY d.district, d.id, rt.id""", (job_id,)
            ).fetchall()]
        except sqlite3.OperationalError:
            rect_rows_raw = []

    findings = [enrich_finding_row(dict(row)) for row in findings]
    findings = [f for f in findings if int(f.get('is_current', 1) or 1) == 1]
    # 问题汇总仅统计客户正式问题；明细表仍输出全部 finding（含 AGENCY 线索）。
    formal_findings = [f for f in findings if f.get("is_customer_formal")]

    workbook = Workbook()
    workbook.remove(workbook.active)
    findings_by_document: dict[int, list] = {}
    for finding in formal_findings:
        findings_by_document.setdefault(int(finding["document_id"]), []).append(finding)
    categories: dict[tuple[str, str], int] = {}
    mapped_findings: set[int] = set()
    seen_source_findings: set[tuple[str, str, int]] = set()
    for item_result in item_results:
        document_id = item_result["effective_document_id"]
        if not document_id:
            continue
        for finding in findings_by_document.get(int(document_id), []):
            identity = (item_result["source_label"], item_result["url"], int(finding["id"]))
            if identity in seen_source_findings:
                continue
            seen_source_findings.add(identity)
            mapped_findings.add(int(finding["id"]))
            key = (item_result["source_label"], finding["category"])
            categories[key] = categories.get(key, 0) + 1
    for finding in formal_findings:
        if int(finding["id"]) in mapped_findings:
            continue
        source = finding["source_site"] or finding["district"]
        key = (source, finding["category"])
        categories[key] = categories.get(key, 0) + 1
    coverage_label = "完整" if job["coverage_status"] == "complete" else "未完成"
    remaining = max(int(job["estimated_total"]) - int(job["examined_count"]), 0)
    selected_values = json.loads(job["districts_json"])
    sources = []
    for value in selected_values:
        try:
            label = resolve_target(value).label
        except ValueError:
            label = value
        if label not in sources:
            sources.append(label)
    totals_by_district = json.loads(job["total_by_district_json"] or "{}")
    examined_by_district = json.loads(job["examined_by_district_json"] or "{}")
    actions_by_district: dict[str, dict[str, int]] = {}
    for row in action_rows:
        actions_by_district.setdefault(row["district"], {})[row["action"]] = int(row["count"])
    if len(sources) == 1:
        source = sources[0]
        totals_by_district.setdefault(source, int(job["estimated_total"]))
        examined_by_district.setdefault(source, int(job["examined_count"]))
    summary_rows = []
    for source in sources:
        source_categories = [
            (category, count) for (name, category), count in sorted(categories.items()) if name == source
        ]
        if not source_categories:
            source_categories = [("无已发现问题", 0)]
        source_total = int(totals_by_district.get(source, 0))
        source_examined = int(examined_by_district.get(source, 0))
        source_actions = actions_by_district.get(source, {})
        source_processed = int(source_actions.get("processed", 0))
        source_skipped = int(source_actions.get("skipped", 0))
        source_remaining = max(source_total - source_examined, 0)
        for category, count in source_categories:
            summary_rows.append((
                source, category, count, job["status"], coverage_label, source_examined,
                source_processed, source_skipped, source_total, source_remaining,
                job["pause_reason"] or job["last_error"],
            ))
    _write_sheet(
        workbook, "问题汇总",
        ["扫描来源", "问题类型", "客户正式问题数", "扫描状态", "扫描完整度", "已覆盖", "详情检查", "复用/跳过",
         "预计总数", "预计剩余", "暂停/未完成原因"], summary_rows,
    )

    date_rows = [(f["source_site"] or "市级平台", f["district"], f["source_id"], f["title"], _excel_date(f["authored_date"]),
                  _excel_date(f["published_date"]), _workday_count(f["detail"]),
                  "日期倒置" if f["rule_code"] == "DATE-002" else "超期", f["page_document_number"],
                  "页面", f["rule_code"], f["detail"], f["review_status"],
                  f.get("machine_status") or "", f.get("human_status") or "", f.get("rule_version") or "",
                  "是" if f.get("is_customer_formal") else "否", f["url"])
                 for f in findings if f["category"] == "日期问题"]
    _write_sheet(workbook, "超期与日期问题", ["栏目名称", "区县", "文件ID", "标题", "成文日期", "发布日期", "工作日差", "时效状态", "文号", "文号来源", "规则", "问题详情", "复核状态", "机器状态", "人工状态", "规则版本", "是否客户正式问题", "链接"], date_rows)

    doc_rows = []
    for finding in findings:
        if finding["category"] not in {"文号问题", "机构问题"}:
            continue
        number_type, number_year, number_index, combined = _document_number_parts(finding["page_document_number"])
        doc_rows.append((finding["source_site"] or "市级平台", finding["district"], finding["source_id"], finding["title"],
                         _excel_date(finding["authored_date"]), number_type, number_year, number_index, combined,
                         "页面", finding["issuing_agency"], finding["body_value"], finding["rule_code"],
                         finding["detail"], finding["evidence"], finding["review_status"],
                         finding.get("machine_status") or "", finding.get("human_status") or "",
                         finding.get("rule_version") or "",
                         "是" if finding.get("is_customer_formal") else "否", finding["url"]))
    _write_sheet(workbook, "文号与机构问题", ["栏目名称", "区县", "文件ID", "标题", "成文日期", "文号类型", "文号年份", "文号编号", "组合文号", "文号来源", "发文机构", "正文值", "问题类型", "问题详情", "证据", "复核状态", "机器状态", "人工状态", "规则版本", "是否客户正式问题", "链接"], doc_rows)

    link_rows = [
        (
            r["source_site"] or "市级平台", r["district"], r["title"], r["link_kind"], r["source_area"],
            r["link_text"], r["source_page_url"], r["original_url"], r["final_url"], r["status_code"],
            r["result"], r["error_type"], r["evidence"], r["redirect_chain_json"], r["page_title"],
            r["checked_at"], r["document_url"],
        )
        for r in links
    ]
    _write_sheet(
        workbook, "外链问题",
        ["来源", "区县", "文件标题", "关联类型", "页面位置", "控件文字", "控件所在页面", "原始URL",
         "最终URL", "状态码", "检查结果", "错误类型", "错误证据", "重定向链", "页面标题", "检查时间", "文件链接"],
        link_rows,
    )

    status_names = {
        "checked_complete": "表头完整，已检查",
        "checked_incomplete": "表头不完整，已记录问题",
        "no_header_pass": "无表头 PASS",
        "reused_baseline_detail": "基线复用（有表头）",
        "reused_baseline_no_header": "基线复用（无表头 PASS）",
        "reused_current_detail": "当期 URL 复用（有表头）",
        "reused_current_no_header": "当期 URL 复用（无表头 PASS）",
        "exception": "详情异常，待复测",
    }
    all_rows = []
    metadata_by_url: dict[str, tuple[int, tuple]] = {}
    for item_result in item_results:
        document_id = item_result["effective_document_id"]
        doc_findings = findings_by_document.get(int(document_id), []) if document_id else []
        issue_codes = "、".join(finding["rule_code"] for finding in doc_findings)
        issue_details = "；".join(finding["detail"] for finding in doc_findings)
        try:
            missing_fields = json.loads(item_result["missing_fields_json"] or "[]")
        except json.JSONDecodeError:
            missing_fields = ["缺失字段记录格式异常"]
        authored = _excel_date(item_result["authored_date"])
        published = _excel_date(item_result["published_date"])
        workdays, time_status = _timeliness(authored, published)
        number_type, number_year, number_index, combined = _document_number_parts(item_result["page_document_number"])
        header_state = "无表头" if not item_result["header_detected"] else "表头不完整" if missing_fields else "表头完整"
        all_rows.append((
            item_result["source_label"], item_result["channel_id"], item_result["page_number"],
            item_result["item_index"] + 1, _excel_date(item_result["listed_date"]), item_result["title"],
            header_state, item_result["source_id"], item_result["topic_category"], item_result["disclosure_attribute"],
            authored, item_result["page_document_number"], published, item_result["issuing_agency"],
            "、".join(missing_fields), workdays, time_status, number_type, number_year, number_index, combined,
            issue_codes, issue_details, len(doc_findings), status_names.get(item_result["detail_status"], item_result["detail_status"]),
            item_result["baseline_job_id"] or "", item_result["reason"], item_result["url"],
        ))
        if missing_fields:
            metadata_row = (
                item_result["source_label"], item_result["channel_id"], item_result["title"], item_result["source_id"],
                "、".join(missing_fields), item_result["detail_status"], item_result["reason"], item_result["url"],
            )
            priority = 2 if item_result["detail_status"] == "checked_incomplete" else 1
            current = metadata_by_url.get(item_result["url"])
            if current is None or priority > current[0]:
                metadata_by_url[item_result["url"]] = (priority, metadata_row)
    metadata_rows = [row for _priority, row in metadata_by_url.values()]
    _write_sheet(
        workbook, "全量明细",
        ["来源 Sheet", "栏目 ID", "列表页码", "列表序号", "列表发布日期", "标题", "表头状态", "索引号", "主题分类",
         "公开属性", "成文日期", "发文字号", "发布日期", "公开主体", "缺失/无效字段", "工作日差", "时效状态",
         "文号类型", "文号年份", "文号编号", "组合文号", "问题类型", "问题详情", "问题数", "处理结果",
         "基线任务 ID", "处理说明", "链接"],
        all_rows,
    )
    _write_sheet(
        workbook, "元数据问题",
        ["来源 Sheet", "栏目 ID", "标题", "索引号", "缺失/无效字段", "处理结果", "处理说明", "链接"],
        metadata_rows,
    )

    pdf_rows = [
        (f["source_site"] or "市级平台", f["district"], f["source_id"], f["title"],
         f["rule_code"], f["category"], f["detail"], f["page_value"], f["body_value"],
         f["evidence"], f.get("evidence_json") or "", f["review_status"],
         f.get("machine_status") or "", f.get("human_status") or "", f.get("rule_version") or "",
         "是" if f.get("is_customer_formal") else "否", f["url"])
        for f in findings if f["category"] in ("pdf_integrity", "pdf_compare", "attachment")
    ]
    _write_sheet(workbook, "PDF 与附件问题",
                 ["来源", "区县", "文件ID", "标题", "规则", "规则类别", "问题详情", "页面值", "正文值", "PDF值",
                  "证据", "复核状态", "机器状态", "人工状态", "规则版本", "是否客户正式问题", "链接"],
                 pdf_rows)

    archive_rows = [
        (r.get("source_site") or "市级平台", r.get("district", ""), r.get("source_id", ""),
         r.get("title", ""), r.get("rule_code", ""), r.get("severity", ""),
         r.get("status", ""), r.get("exclusion_reason", ""),
         r.get("machine_status", ""), r.get("human_status", ""), r.get("rule_version", ""),
         "是" if is_customer_formal_problem(
             rule_code=r.get("rule_code", ""), machine_status=r.get("machine_status", ""),
             human_status=r.get("human_status", ""),
             allow_customer_output=bool(r.get("allow_customer_output") or 0),
         ) else "否",
         r.get("evidence_json", ""), r.get("notes", ""), r.get("url", ""))
        for r in archive_rows_raw
    ]
    _write_sheet(workbook, "异常归档问题",
                 ["来源", "区县", "文件ID", "标题", "规则", "严重级别", "状态", "排除原因",
                  "机器状态", "人工状态", "规则版本", "是否客户正式问题", "证据", "备注", "链接"],
                 archive_rows)

    duplicate_rows = [
        (r.get("source_site_a") or "市级平台", r.get("source_id_a", ""), r.get("title_a", ""),
         r.get("source_id_b", ""), r.get("title_b", ""),
         r.get("duplicate_type", ""), r.get("similarity_score", 0),
         "是" if r.get("is_normal") else "否", r.get("status", ""),
         r.get("exclusion_reason", ""),
         r.get("machine_status", ""), r.get("human_status", ""), r.get("rule_version", ""),
         "是" if is_customer_formal_problem(
             rule_code=r.get("duplicate_type", ""), machine_status=r.get("machine_status", ""),
             human_status=r.get("human_status", ""),
             allow_customer_output=bool(r.get("allow_customer_output") or 0),
         ) else "否",
         r.get("evidence_json", ""), r.get("notes", ""),
         r.get("url_a", ""), r.get("url_b", ""))
        for r in duplicate_rows_raw
    ]
    _write_sheet(workbook, "重复数据问题",
                 ["来源", "文件A ID", "文件A标题", "文件B ID", "文件B标题", "重复类型", "相似度", "是否正常重复",
                  "状态", "排除原因", "机器状态", "人工状态", "规则版本", "是否客户正式问题", "证据", "备注",
                  "文件A链接", "文件B链接"],
                 duplicate_rows)

    recon_rows = [
        (r.get("municipal_source_site") or "市级平台",
         r.get("municipal_source_id", ""), r.get("municipal_title", ""),
         r.get("district_source_id", ""), r.get("district_title", ""),
         r.get("match_status", ""),
         r.get("title_match"), r.get("doc_number_match"), r.get("date_match"),
         r.get("body_match"), r.get("attachment_match"),
         r.get("similarity_score", 0), r.get("status", ""),
         r.get("machine_status", ""), r.get("human_status", ""), r.get("rule_version", ""),
         "是" if is_customer_formal_problem(
             rule_code=r.get("match_status", ""), machine_status=r.get("machine_status", ""),
             human_status=r.get("human_status", ""),
             allow_customer_output=bool(r.get("allow_customer_output") or 0),
         ) else "否",
         r.get("evidence_json", ""), r.get("notes", ""))
        for r in recon_rows_raw
    ]
    _write_sheet(workbook, "双渠道对账差异",
                 ["来源", "市级文件ID", "市级文件标题", "区级文件ID", "区级文件标题", "匹配状态",
                  "标题一致", "文号一致", "日期一致", "正文一致", "附件一致", "相似度", "状态",
                  "机器状态", "人工状态", "规则版本", "是否客户正式问题", "证据", "备注"],
                 recon_rows)

    reading_rows = [
        (r.get("source_site") or "市级平台", r.get("district", ""), r.get("source_id", ""),
         r.get("title", ""), r.get("button_label", ""),
         "是" if r.get("button_present") else "否",
         "是" if r.get("clickable") else "否",
         r.get("target_url", ""), r.get("http_status"), r.get("page_status", ""),
         r.get("page_title", ""), r.get("rule_code", ""),
         r.get("machine_status", ""), r.get("human_status", ""), r.get("rule_version", ""),
         "是" if is_customer_formal_problem(
             rule_code=r.get("rule_code", ""), machine_status=r.get("machine_status", ""),
             human_status=r.get("human_status", ""),
             allow_customer_output=bool(r.get("allow_customer_output") or 0),
         ) else "否",
         r.get("evidence_json", ""), r.get("notes", ""), r.get("url", ""))
        for r in reading_rows_raw
    ]
    _write_sheet(workbook, "阅办联动问题",
                 ["来源", "区县", "文件ID", "标题", "按钮文字", "按钮存在", "可点击", "目标URL", "HTTP状态",
                  "页面状态", "页面标题", "规则", "机器状态", "人工状态", "规则版本", "是否客户正式问题",
                  "证据", "备注", "链接"],
                 reading_rows)

    media_rows = [
        (r.get("source_site") or "市级平台", r.get("district", ""), r.get("source_id", ""),
         r.get("title", ""), r.get("media_type", ""), r.get("media_url", ""),
         r.get("source_area", ""), r.get("http_status"), r.get("status", ""),
         r.get("content_type", ""), r.get("detected_kind", ""), r.get("size_bytes", 0),
         r.get("rule_code", ""),
         r.get("machine_status", ""), r.get("human_status", ""), r.get("rule_version", ""),
         "是" if is_customer_formal_problem(
             rule_code=r.get("rule_code", ""), machine_status=r.get("machine_status", ""),
             human_status=r.get("human_status", ""),
             allow_customer_output=bool(r.get("allow_customer_output") or 0),
         ) else "否",
         r.get("evidence_json", ""), r.get("notes", ""), r.get("url", ""))
        for r in media_rows_raw
    ]
    _write_sheet(workbook, "媒体资源问题",
                 ["来源", "区县", "文件ID", "标题", "媒体类型", "媒体URL", "来源区域", "HTTP状态", "状态",
                  "内容类型", "检测类型", "大小字节", "规则", "机器状态", "人工状态", "规则版本",
                  "是否客户正式问题", "证据", "备注", "链接"],
                 media_rows)

    interp_rows = [
        (r.get("source_site") or "市级平台", r.get("district", ""), r.get("source_id", ""),
         r.get("doc_title", ""), r.get("interpretation_type", ""),
         r.get("interpretation_url", ""), r.get("interp_title", ""),
         r.get("link_status", ""), r.get("http_status"),
         r.get("image_status", ""), r.get("video_status", ""),
         r.get("rule_code", ""),
         r.get("machine_status", ""), r.get("human_status", ""), r.get("rule_version", ""),
         "是" if is_customer_formal_problem(
             rule_code=r.get("rule_code", ""), machine_status=r.get("machine_status", ""),
             human_status=r.get("human_status", ""),
             allow_customer_output=bool(r.get("allow_customer_output") or 0),
         ) else "否",
         r.get("evidence_json", ""), r.get("notes", ""))
        for r in interp_rows_raw
    ]
    _write_sheet(workbook, "政策解读问题",
                 ["来源", "区县", "原政策文件ID", "原政策标题", "解读类型", "解读URL", "解读标题",
                  "链接状态", "HTTP状态", "图片状态", "视频状态", "规则", "机器状态", "人工状态",
                  "规则版本", "是否客户正式问题", "证据", "备注"],
                 interp_rows)

    rect_rows = [
        (r.get("source_site") or "市级平台", r.get("district", ""), r.get("source_id", ""),
         r.get("doc_title", ""), r.get("rule_code", ""), r.get("title", ""),
         r.get("responsible_unit", ""), r.get("responsible_person", ""),
         _excel_date(r.get("deadline")),
         r.get("status", ""), r.get("priority", ""), r.get("recurrence_count", 0),
         r.get("machine_status", ""), r.get("human_status", ""), r.get("rule_version", ""),
         "是" if is_customer_formal_problem(
             rule_code=r.get("rule_code", ""), machine_status=r.get("machine_status", ""),
             human_status=r.get("human_status", ""),
             allow_customer_output=bool(r.get("allow_customer_output") or 0),
         ) else "否",
         r.get("description", ""), r.get("notes", ""), r.get("url", ""))
        for r in rect_rows_raw
    ]
    _write_sheet(workbook, "整改任务跟踪",
                 ["来源", "区县", "文件ID", "标题", "规则", "任务标题", "责任单位", "责任人", "截止时间",
                  "状态", "优先级", "复发次数", "机器状态", "人工状态", "规则版本", "是否客户正式问题",
                  "描述", "备注", "链接"],
                 rect_rows)

    run_rows = [(job["id"], "任务汇总", job["mode"], job["status"], coverage_label, job["completion_kind"],
                 job["created_at"], job["started_at"], job["finished_at"], job["examined_count"],
                 job["processed_count"], job["skipped_count"], job["estimated_total"], remaining,
                 job["access_count"], job["retry_count"], job["rest_count"], job["resumed_count"],
                 job["current_district"], job["current_page"], job["pause_reason"], job["last_error"],
                 job["safety_json"], "", "")]
    run_rows.extend((job["id"], event["event_type"], job["mode"], job["status"], coverage_label,
                     job["completion_kind"], event["created_at"], "", "", "", "", "", "", "", "", "", "", "",
                     "", "", event["message"], "", event["details_json"], event["url"], event["id"]) for event in events)
    _write_sheet(workbook, "扫描运行记录", ["任务ID", "记录类型", "模式", "状态", "扫描完整度", "结束类型", "记录时间", "开始时间", "结束时间", "已覆盖", "详情检查", "复用/跳过", "预计总数", "预计剩余", "访问次数", "重试次数", "强休息次数", "恢复次数", "当前区县", "当前页", "说明", "最后错误", "参数/详情", "URL", "事件ID"], run_rows)

    output = EXPORT_DIR / f"政策巡检结果_任务{job_id}_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    workbook.save(output)
    return output
