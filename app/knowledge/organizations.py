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


RELATIONS = [
    ("中共上海市普陀区委办公室", "中共上海市普陀区委员会", "office_of"),
    ("上海市普陀区人民政府办公室", "上海市普陀区人民政府", "office_of"),
    ("中共上海市普陀区委员会", "上海市普陀区人民政府", "party_government_pair"),
    ("中共上海市普陀区委办公室", "上海市普陀区人民政府办公室", "joint_forward_pair"),
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
