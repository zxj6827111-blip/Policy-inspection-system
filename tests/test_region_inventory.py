from openpyxl import Workbook

from app.config import ORDOS_DEPARTMENTS
from app.knowledge.organizations import ORDOS_TOWNSHIPS
from scripts.check_region_inventory import main


def inventory_rows():
    """按需求方导入表的形状还原一份完整清单：区县下辖行 + 只填区县的部门行。"""
    rows = []
    for district, chain in ORDOS_TOWNSHIPS:
        for township in chain.split(","):
            rows.append(("内蒙古自治区", "鄂尔多斯市", district, township))
    names = [name for _slug, name in ORDOS_DEPARTMENTS]
    # 清单用“市农牧局”，官网子站写作“市农牧业局”。
    names[names.index("市农牧业局")] = "市农牧局"
    names.insert(0, "市人民政府办公室")
    names.append("市人民政府外事办公室")
    for name in names:
        rows.append(("内蒙古自治区", "鄂尔多斯市", name, ""))
    return rows


def write_inventory(path, rows, header=("省份", "城市", "区县", "街道")):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(list(header))
    for row in rows:
        sheet.append(list(row))
    workbook.save(path)
    return path


def test_full_inventory_is_consistent(tmp_path, capsys):
    path = write_inventory(tmp_path / "inventory.xlsx", inventory_rows())
    assert main(["check_region_inventory", str(path)]) == 0
    output = capsys.readouterr().out
    assert "导入表与系统配置、机构知识库一致" in output
    assert "区县 9 个 · 部门 32 个 · 乡镇街道 77 项" in output
    # 两个官网确实没有子栏门的部门作为已知差异说明，不算不一致。
    assert "市人民政府办公室" in output and "市人民政府外事办公室" in output
    # 市农牧局与官网市农牧业局是同一机构，按别名组比对后不应报缺项。
    assert "市农牧业局」不在导入表清单中" not in output


def test_unknown_district_is_reported(tmp_path, capsys):
    path = write_inventory(
        tmp_path / "unknown.xlsx",
        [("内蒙古自治区", "鄂尔多斯市", "不存在旗", "某镇")],
    )
    assert main(["check_region_inventory", str(path)]) == 1
    output = capsys.readouterr().out
    assert "不存在旗" in output


def test_missing_township_seed_is_reported(tmp_path, capsys):
    path = write_inventory(
        tmp_path / "missing_township.xlsx",
        [("内蒙古自治区", "鄂尔多斯市", "东胜区", "不存在的镇")],
    )
    assert main(["check_region_inventory", str(path)]) == 1
    output = capsys.readouterr().out
    assert "不存在的镇" in output


def test_department_absent_from_knowledge_is_inconsistency(tmp_path, capsys):
    """清单里新增一个系统尚未登记的部门：应当报错，而不是只当差异说明。"""
    rows = inventory_rows() + [("内蒙古自治区", "鄂尔多斯市", "市数据局", "")]
    path = write_inventory(tmp_path / "extra.xlsx", rows)
    assert main(["check_region_inventory", str(path)]) == 1
    output = capsys.readouterr().out
    assert "「市数据局」未入库为鄂尔多斯市级机构" in output


def test_department_without_site_column_is_note_only(tmp_path, capsys):
    """外事办公室在知识库里已登记，但官网没有子栏目：作为已知差异说明，不判失败。"""
    rows = inventory_rows()
    path = write_inventory(tmp_path / "note.xlsx", rows)
    assert main(["check_region_inventory", str(path)]) == 0
    output = capsys.readouterr().out
    assert "市人民政府外事办公室" in output and "未单独开设部门文件子栏目" in output
