"""Organization knowledge base seed and lookup helpers."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any


@dataclass
class OrgRecord:
    id: int
    standard_name: str
    org_type: str
    admin_level: str
    district: str
    aliases: list[str]
    symbols: list[str]


# First-batch Putuo knowledge (phase 1). No URL whitelist.
SEED_ORGANIZATIONS: list[dict[str, Any]] = [
    {
        "standard_name": "上海市普陀区人民政府",
        "org_type": "government",
        "admin_level": "district",
        "district": "普陀区",
        "aliases": ["普陀区人民政府", "普陀区政府", "区政府"],
        "symbols": ["普府"],
        "effective_from": "2000-01-01",
    },
    {
        "standard_name": "上海市普陀区人民政府办公室",
        "org_type": "government_office",
        "admin_level": "district",
        "district": "普陀区",
        "aliases": ["普陀区人民政府办公室", "普陀区政府办公室", "区府办", "区政府办"],
        "symbols": ["普府办"],
        "effective_from": "2000-01-01",
    },
    {
        "standard_name": "中共上海市普陀区委员会",
        "org_type": "party",
        "admin_level": "district",
        "district": "普陀区",
        "aliases": ["中共普陀区委", "普陀区委", "区委"],
        "symbols": ["普委", "普委发"],
        "effective_from": "2000-01-01",
    },
    {
        "standard_name": "中共上海市普陀区委办公室",
        "org_type": "party_office",
        "admin_level": "district",
        "district": "普陀区",
        "aliases": [
            "中共上海市普陀区委员会办公室",
            "普陀区委办公室",
            "区委办",
            "普委办",
        ],
        "symbols": ["普委办"],
        "effective_from": "2000-01-01",
    },
    {
        "standard_name": "上海市普陀区宜川路街道办事处",
        "org_type": "street_office",
        "admin_level": "street",
        "district": "普陀区",
        "aliases": ["宜川路街道办事处", "宜川路街道", "宜川街道", "普宜办"],
        "symbols": ["普宜办"],
        "effective_from": "2000-01-01",
    },
    {
        "standard_name": "上海市普陀区长征镇人民政府",
        "org_type": "town_government",
        "admin_level": "street",
        "district": "普陀区",
        "aliases": ["长征镇人民政府", "长征镇", "长街办", "长征街道"],
        "symbols": ["长街办", "普长镇"],
        "effective_from": "2000-01-01",
    },
    {
        "standard_name": "上海市普陀区曹杨新村街道办事处",
        "org_type": "street_office",
        "admin_level": "street",
        "district": "普陀区",
        "aliases": ["曹杨新村街道办事处", "曹杨街道", "曹杨新村街道"],
        "symbols": ["普曹办"],
        "effective_from": "2000-01-01",
    },
    {
        "standard_name": "上海市普陀区长风新村街道办事处",
        "org_type": "street_office",
        "admin_level": "street",
        "district": "普陀区",
        "aliases": ["长风新村街道办事处", "长风街道"],
        "symbols": ["普长风办"],
        "effective_from": "2000-01-01",
    },
    {
        "standard_name": "上海市普陀区石泉路街道办事处",
        "org_type": "street_office",
        "admin_level": "street",
        "district": "普陀区",
        "aliases": ["石泉路街道办事处", "石泉街道"],
        "symbols": ["普石办"],
        "effective_from": "2000-01-01",
    },
    {
        "standard_name": "上海市普陀区甘泉路街道办事处",
        "org_type": "street_office",
        "admin_level": "street",
        "district": "普陀区",
        "aliases": ["甘泉路街道办事处", "甘泉街道"],
        "symbols": ["普甘办"],
        "effective_from": "2000-01-01",
    },
]


# ==================== 鄂尔多斯市（内蒙古自治区）====================
# 区划与部门清单来自需求方提供的导入表，并与官网政务公开栏目菜单逐项核对过。
# 文号代字只收录在官网列表或详情页里实测出现过的（鄂府发/鄂府办发/鄂环发/伊政办发），
# 其余部门代字待真实扫描数据回填，不凭空补写，避免机构规则产生假线索。
ORDOS_CITY_DISTRICT = "鄂尔多斯市"

ORDOS_BANNERS: tuple[tuple[str, str], ...] = (
    ("东胜区", "鄂尔多斯市东胜区人民政府"),
    ("康巴什区", "鄂尔多斯市康巴什区人民政府"),
    ("达拉特旗", "鄂尔多斯市达拉特旗人民政府"),
    ("准格尔旗", "鄂尔多斯市准格尔旗人民政府"),
    ("伊金霍洛旗", "鄂尔多斯市伊金霍洛旗人民政府"),
    ("乌审旗", "鄂尔多斯市乌审旗人民政府"),
    ("杭锦旗", "鄂尔多斯市杭锦旗人民政府"),
    ("鄂托克旗", "鄂尔多斯市鄂托克旗人民政府"),
    ("鄂托克前旗", "鄂尔多斯市鄂托克前旗人民政府"),
)

# 官网“部门文件”栏目子站名与需求清单存在一处差异：清单里的“市农牧局”在官网写作
# “市农牧业局”，两处都收为别名；“市人民政府外事办公室”官网未单独开栏，仍按区划入库。
# “市人民政府办公室”已在上面带“鄂府办发”代字单独入库，这里不再重复。
ORDOS_DEPARTMENTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("市发展和改革委员会", ("市发改委",)),
    ("市教育体育局", ("市教体局",)),
    ("市科学技术局", ("市科技局",)),
    ("市工业和信息化局", ("市工信局",)),
    ("市民族事务委员会", ("市民委",)),
    ("市公安局", ()),
    ("市民政局", ()),
    ("市司法局", ()),
    ("市财政局", ()),
    ("市人力资源和社会保障局", ("市人社局",)),
    ("市自然资源局", ()),
    ("市生态环境局", ()),
    ("市住房和城乡建设局", ("市住建局",)),
    ("市交通运输局", ()),
    ("市水利局", ()),
    ("市农牧局", ("市农牧业局",)),
    ("市商务局", ()),
    ("市文化和旅游局", ("市文旅局",)),
    ("市卫生健康委员会", ("市卫健委",)),
    ("市退役军人事务局", ()),
    ("市应急管理局", ()),
    ("市审计局", ()),
    ("市人民政府外事办公室", ("市外办",)),
    ("市国有资产监督管理委员会", ("市国资委",)),
    ("市市场监督管理局", ("市市场监管局",)),
    ("市统计局", ()),
    ("市能源局", ()),
    ("市林业和草原局", ("市林草局",)),
    ("市医疗保障局", ("市医保局",)),
    ("市信访局", ()),
    ("市行政审批政务服务与数据管理局", ("市行政审批局", "市政数局")),
)

# 每个旗区下辖的街道镇/苏木（“苏木”是内蒙古的乡级建制，与镇同级）。
ORDOS_TOWNSHIPS: tuple[tuple[str, str], ...] = (
    ("东胜区", "罕台镇,铜川镇,泊尔江海子镇,巴音门克街道,富兴街道,公园街道,诃额伦街道,"
               "交通街道,建设街道,天骄街道,林荫街道,兴胜街道,民族街道,纺织街道,幸福街道"),
    ("康巴什区", "哈巴格希街道,青春山街道,滨河街道,康新街道"),
    ("达拉特旗", "展旦召苏木,吉格斯太镇,白泥井镇,风水梁镇,王爱召镇,树林召镇,昭君镇,"
                "恩格贝镇,中和西镇,白塔街道,锡尼街道,昭君街道,工业街道,西园街道,平原街道"),
    ("准格尔旗", "布尔陶亥苏木,十二连城乡,暖水乡,薛家湾镇,沙圪堵镇,大路镇,纳日松镇,"
                "龙口镇,魏家峁镇,准格尔召镇,蓝天街道,友谊街道,兴隆街道,迎泽街道"),
    ("伊金霍洛旗", "阿勒腾席热镇,苏布尔嘎镇,札萨克镇,伊金霍洛镇,乌兰木伦镇,纳林陶亥镇,红庆河镇"),
    ("乌审旗", "嘎鲁图镇,乌审召镇,图克镇,乌兰陶勒盖镇,无定河镇,苏力德苏木"),
    ("杭锦旗", "锡尼镇,巴拉贡镇,呼和木独镇,吉日嘎朗图镇,独贵塔拉镇,伊和乌素苏木"),
    ("鄂托克旗", "乌兰镇,棋盘井镇,蒙西镇,木凯淖尔镇,阿尔巴斯苏木,苏米图苏木"),
    ("鄂托克前旗", "敖勒召其镇,上海庙镇,城川镇,昂素镇"),
)


def _township_organization(district: str, name: str) -> dict[str, Any]:
    if name.endswith("街道"):
        return {
            "standard_name": f"鄂尔多斯市{district}{name}街道办事处",
            "org_type": "street_office",
            "admin_level": "street",
            "district": district,
            "aliases": [f"{district}{name}", name],
            "effective_from": "2000-01-01",
        }
    return {
        "standard_name": f"鄂尔多斯市{district}{name}人民政府",
        "org_type": "town_government",
        "admin_level": "street",
        "district": district,
        "aliases": [f"{district}{name}", name],
        "effective_from": "2000-01-01",
    }


def _ordos_organizations() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {
            "standard_name": "鄂尔多斯市人民政府",
            "org_type": "government",
            "admin_level": "city",
            "district": ORDOS_CITY_DISTRICT,
            "aliases": ["鄂尔多斯市政府", "市人民政府"],
            "symbols": ["鄂府发"],
            "effective_from": "2000-01-01",
        },
        {
            "standard_name": "鄂尔多斯市人民政府办公室",
            "org_type": "government_office",
            "admin_level": "city",
            "district": ORDOS_CITY_DISTRICT,
            "aliases": ["市政府办公室", "市人民政府办公室"],
            "symbols": ["鄂府办发"],
            "effective_from": "2000-01-01",
        },
    ]
    for short, extra in ORDOS_DEPARTMENTS:
        rows.append({
            "standard_name": f"鄂尔多斯{short}",
            "org_type": "government_department",
            "admin_level": "city",
            "district": ORDOS_CITY_DISTRICT,
            "aliases": [short, *extra],
            "symbols": ["鄂环发"] if short == "市生态环境局" else [],
            "effective_from": "2000-01-01",
        })
    for district, standard in ORDOS_BANNERS:
        rows.append({
            "standard_name": standard,
            "org_type": "government",
            "admin_level": "district",
            "district": district,
            "aliases": [f"{district}人民政府", district],
            "effective_from": "2000-01-01",
        })
    rows.append({
        "standard_name": "鄂尔多斯市伊金霍洛旗人民政府办公室",
        "org_type": "government_office",
        "admin_level": "district",
        "district": "伊金霍洛旗",
        # 「伊旗政府办」「伊政办」是伊金霍洛旗详情页「发布机构」字段的实测写法。
        # 登记后「伊旗政府」会靠子串并入本机构而不报 ORG-001，与「伊金霍洛旗人民政府」
        # 「伊金霍洛旗政府」挂伊政办发代字一律放行的既有口径一致。
        "aliases": [
            "伊金霍洛旗人民政府办公室", "伊金霍洛旗政府办公室", "伊金霍洛旗政府办",
            "伊旗政府办", "伊政办",
        ],
        "symbols": ["伊政办发"],
        "effective_from": "2000-01-01",
    })
    for district, chain in ORDOS_TOWNSHIPS:
        for name in (item.strip() for item in chain.split(",")):
            if name:
                rows.append(_township_organization(district, name))
    return rows


SEED_ORGANIZATIONS.extend(_ordos_organizations())

RELATIONS = [
    ("中共上海市普陀区委办公室", "中共上海市普陀区委员会", "office_of"),
    ("上海市普陀区人民政府办公室", "上海市普陀区人民政府", "office_of"),
    ("中共上海市普陀区委员会", "上海市普陀区人民政府", "party_government_pair"),
    ("中共上海市普陀区委办公室", "上海市普陀区人民政府办公室", "joint_forward_pair"),
    ("鄂尔多斯市人民政府办公室", "鄂尔多斯市人民政府", "office_of"),
    ("鄂尔多斯市伊金霍洛旗人民政府办公室", "鄂尔多斯市伊金霍洛旗人民政府", "office_of"),
]


def seed_organizations(conn: sqlite3.Connection) -> dict[str, int]:
    name_to_id: dict[str, int] = {}
    created = 0
    for item in SEED_ORGANIZATIONS:
        row = conn.execute(
            "SELECT id FROM organizations WHERE standard_name=?",
            (item["standard_name"],),
        ).fetchone()
        if row:
            org_id = int(row["id"])
        else:
            cur = conn.execute(
                """INSERT INTO organizations(
                    standard_name, org_type, admin_level, district, effective_from, effective_to, notes
                ) VALUES(?,?,?,?,?,?,?)""",
                (
                    item["standard_name"],
                    item["org_type"],
                    item["admin_level"],
                    item["district"],
                    item.get("effective_from"),
                    item.get("effective_to"),
                    item.get("notes", ""),
                ),
            )
            org_id = int(cur.lastrowid)
            created += 1
        name_to_id[item["standard_name"]] = org_id
        for alias in item.get("aliases", []):
            conn.execute(
                """INSERT OR IGNORE INTO organization_aliases(
                    organization_id, alias, alias_kind, effective_from, effective_to
                ) VALUES(?,?,?,?,?)""",
                (org_id, alias, "alias", item.get("effective_from"), item.get("effective_to")),
            )
        # standard name is also a matchable alias
        conn.execute(
            """INSERT OR IGNORE INTO organization_aliases(
                organization_id, alias, alias_kind, effective_from, effective_to
            ) VALUES(?,?,?,?,?)""",
            (org_id, item["standard_name"], "standard", item.get("effective_from"), None),
        )
        for symbol in item.get("symbols", []):
            conn.execute(
                """INSERT OR IGNORE INTO document_symbols(
                    symbol, organization_id, doc_type, scope, effective_from, effective_to
                ) VALUES(?,?,?,?,?,?)""",
                (symbol, org_id, "", item["district"], item.get("effective_from"), None),
            )

    for from_name, to_name, rel in RELATIONS:
        if from_name not in name_to_id or to_name not in name_to_id:
            continue
        conn.execute(
            """INSERT OR IGNORE INTO organization_relations(
                from_organization_id, to_organization_id, relation_type, notes
            ) VALUES(?,?,?,?)""",
            (name_to_id[from_name], name_to_id[to_name], rel, ""),
        )

    return {
        "organizations": conn.execute("SELECT COUNT(*) FROM organizations").fetchone()[0],
        "aliases": conn.execute("SELECT COUNT(*) FROM organization_aliases").fetchone()[0],
        "symbols": conn.execute("SELECT COUNT(*) FROM document_symbols").fetchone()[0],
        "relations": conn.execute("SELECT COUNT(*) FROM organization_relations").fetchone()[0],
        "created_orgs": created,
    }


def load_knowledge(conn: sqlite3.Connection, district: str | None = None) -> list[OrgRecord]:
    sql = "SELECT * FROM organizations"
    params: tuple = ()
    if district:
        sql += " WHERE district=?"
        params = (district,)
    rows = conn.execute(sql, params).fetchall()
    result: list[OrgRecord] = []
    for row in rows:
        org_id = int(row["id"])
        aliases = [
            r["alias"]
            for r in conn.execute(
                "SELECT alias FROM organization_aliases WHERE organization_id=?",
                (org_id,),
            ).fetchall()
        ]
        symbols = [
            r["symbol"]
            for r in conn.execute(
                "SELECT symbol FROM document_symbols WHERE organization_id=?",
                (org_id,),
            ).fetchall()
        ]
        result.append(
            OrgRecord(
                id=org_id,
                standard_name=row["standard_name"],
                org_type=row["org_type"],
                admin_level=row["admin_level"],
                district=row["district"],
                aliases=aliases,
                symbols=symbols,
            )
        )
    return result


def match_organization_by_name(knowledge: list[OrgRecord], text: str) -> OrgRecord | None:
    if not text:
        return None
    # Longer names first to avoid partial mis-hits
    candidates: list[tuple[int, OrgRecord]] = []
    for org in knowledge:
        names = [org.standard_name, *org.aliases]
        for name in names:
            if name and (name in text or text in name):
                candidates.append((len(name), org))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def match_organization_by_symbol(knowledge: list[OrgRecord], symbol: str) -> list[OrgRecord]:
    if not symbol:
        return []
    hits = []
    for org in knowledge:
        for item in org.symbols:
            if symbol == item or symbol.startswith(item) or item.startswith(symbol):
                hits.append(org)
                break
    return hits
