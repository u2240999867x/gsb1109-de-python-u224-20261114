# -*- coding: utf-8 -*-
"""覆盖工单四条规则：
1. 编号实例 + 标题层级 + 条款内容的提取与对照；
2. 同一父项内维护先后顺序；
3. 重复短句只在证据唯一时自动迁移工单；
4. 旧条款一分为二且人工确认时保留两条对应，后续编号不判为改写。
"""

import json

from clause_diff import (
    align_documents,
    extract_clauses,
    migrate_references,
    migrate_text,
)
from clause_diff.cli import main as cli_main
from clause_diff.tests.docx_factory import build_docx, literal, numbered


def _by_old(mappings):
    return {m.old: m for m in mappings if m.old is not None}


# ---------- 提取 ----------

def test_extract_uses_numbering_instances_and_levels(tmp_path):
    path = build_docx(tmp_path / "auto.docx", [
        numbered("总则", 0),
        numbered("定义", 0),
        numbered("适用范围", 1),
        numbered("地域范围", 2),
        numbered("附则", 0),
    ])
    clauses = extract_clauses(path)
    assert [c.number for c in clauses] == ["1", "2", "2.1", "2.1.1", "3"]
    assert [c.level for c in clauses] == [0, 0, 1, 2, 0]
    assert clauses[2].parent == "2"
    assert clauses[3].parent == "2.1"
    assert clauses[4].parent == ""
    assert all(c.source == "numbering" for c in clauses)


def test_extract_literal_numbers_with_heading_styles(tmp_path):
    path = build_docx(tmp_path / "lit.docx", [
        literal("4 付款方式", style="Heading1"),
        literal("4.1 账期为三十天。"),
        literal("4.2 发票按月开具。"),
        literal("2026 年 1 月 1 日起生效。"),  # 非标题、无层级圆点，不算条款
    ])
    clauses = extract_clauses(path)
    assert [c.number for c in clauses] == ["4", "4.1", "4.2"]
    assert clauses[0].text == "付款方式"
    assert clauses[1].parent == "4"


# ---------- 规则 1+2：新增条款导致编号平移 ----------

def _shift_pair(tmp_path):
    old = build_docx(tmp_path / "old.docx", [
        numbered("定义与解释", 0),
        numbered("合同价款", 0),
        numbered("按季度支付。", 1),
        numbered("甲方应在收到发票后三十日内付款。", 1),
        numbered("逾期付款按日万分之五计息。", 1),
        numbered("付款以银行转账方式完成。", 1),
    ])
    new = build_docx(tmp_path / "new.docx", [
        numbered("定义与解释", 0),
        numbered("合同价款", 0),
        numbered("按季度支付。", 1),
        numbered("本条款为昨日新增的付款前置条件。", 1),  # 新 4.2
        numbered("甲方应在收到发票后三十日内付款。", 1),  # 旧 4.2 -> 新 4.3
        numbered("逾期付款按日万分之五计息。", 1),
        numbered("付款以银行转账方式完成。", 1),
    ])
    return old, new


def test_inserted_clause_shifts_following_numbers(tmp_path):
    old_path, new_path = _shift_pair(tmp_path)
    mappings = align_documents(extract_clauses(old_path),
                               extract_clauses(new_path))
    by_old = _by_old(mappings)
    # 旧 4.2 的内容整体平移到新 4.3
    assert by_old["2.2"].new == ["2.3"]
    assert by_old["2.2"].status == "renumbered"
    assert by_old["2.3"].new == ["2.4"]
    assert by_old["2.4"].new == ["2.5"]
    # 昨日新增的新 4.2 标为 added
    added = [m for m in mappings if m.status == "added"]
    assert [m.new for m in added] == [["2.2"]]


def test_work_order_reference_follows_the_shift(tmp_path):
    old_path, new_path = _shift_pair(tmp_path)
    mappings = align_documents(extract_clauses(old_path),
                               extract_clauses(new_path))
    # 工单写着“见第 4.2 项”（此处编号为 2.2），应自动迁到 2.3
    (mig,) = migrate_references(mappings, ["2.2"])
    assert mig["auto"] is True
    assert mig["new"] == ["2.3"]
    assert migrate_text("工期见第 2.2 项。", mappings) == "工期见第 2.3 项。"


# ---------- 规则 3：重复短句不自动迁移 ----------

def test_duplicate_short_phrase_is_not_auto_migrated(tmp_path):
    old = build_docx(tmp_path / "old.docx", [
        numbered("总则", 0),
        numbered("交付", 0),
        numbered("每月交付一批。", 1),
        numbered("详见附表。", 1),          # 2.2，短句第一次出现
        numbered("验收期为七日。", 1),
        numbered("详见附表。", 1),          # 2.4，同一短句第二次出现
        numbered("详见附件。", 1),          # 2.5，唯一的短句
    ])
    new = build_docx(tmp_path / "new.docx", [
        numbered("总则", 0),
        numbered("交付", 0),
        numbered("每月交付一批。", 1),
        numbered("新增的交付前置说明。", 1),
        numbered("详见附表。", 1),          # 2.4
        numbered("验收期为七日。", 1),
        numbered("详见附表。", 1),          # 2.6
        numbered("详见附件。", 1),          # 2.7
    ])
    mappings = align_documents(extract_clauses(old), extract_clauses(new))
    by_old = _by_old(mappings)

    assert by_old["2.2"].status == "ambiguous"
    assert by_old["2.2"].auto is False
    assert sorted(by_old["2.2"].new) == ["2.3", "2.5"]
    assert by_old["2.4"].status == "ambiguous"

    # 证据唯一的短句仍然自动对应
    assert by_old["2.5"].new == ["2.6"]
    assert by_old["2.5"].status == "renumbered"

    # 工单：重复短句不自动迁移，唯一短句自动迁移
    migrations = {m["ref"]: m for m in
                  migrate_references(mappings, ["2.2", "2.5"])}
    assert migrations["2.2"]["auto"] is False
    assert sorted(migrations["2.2"]["candidates"]) == ["2.3", "2.5"]
    assert migrations["2.5"]["auto"] is True
    assert migrations["2.5"]["new"] == ["2.6"]


# ---------- 规则 4：拆分经人工确认，保留两条对应 ----------

def _split_pair(tmp_path):
    old = build_docx(tmp_path / "old.docx", [
        numbered("总则", 0),
        numbered("定义", 0),
        numbered("交付", 0),
        numbered("付款", 0),
        numbered("验收", 0),
        numbered("到货后三日内验收。", 1),
        numbered("验收包括外观检查与功能测试两部分，分别记录结果。", 1),  # 5.2
        numbered("验收不合格的可以拒收。", 1),                              # 5.3
        numbered("拒收后供应商应自费取回。", 1),                            # 5.4
    ])
    new = build_docx(tmp_path / "new.docx", [
        numbered("总则", 0),
        numbered("定义", 0),
        numbered("交付", 0),
        numbered("付款", 0),
        numbered("验收", 0),
        numbered("到货后三日内验收。", 1),
        numbered("验收记录由双方签字确认。", 1),     # 5.2 新增
        numbered("验收包括外观检查，并记录结果。", 1),  # 5.3 承接旧 5.2 前半
        numbered("功能测试结果应分别记录。", 1),       # 5.4 承接旧 5.2 后半
        numbered("验收不合格的可以拒收。", 1),         # 5.5
        numbered("拒收后供应商应自费取回。", 1),       # 5.6
    ])
    return old, new


def test_confirmed_split_keeps_both_and_does_not_rewrite_the_rest(tmp_path):
    old_path, new_path = _split_pair(tmp_path)
    confirmations = [{"old": "5.2", "new": ["5.3", "5.4"]}]
    mappings = align_documents(extract_clauses(old_path),
                               extract_clauses(new_path),
                               confirmations)
    by_old = _by_old(mappings)

    # 拆分：保留两条对应
    assert by_old["5.2"].status == "split"
    assert by_old["5.2"].new == ["5.3", "5.4"]

    # 后续编号按内容继续对应，不能全判成改写/删除
    assert by_old["5.3"].new == ["5.5"]
    assert by_old["5.3"].status == "renumbered"
    assert by_old["5.4"].new == ["5.6"]
    assert by_old["5.4"].status == "renumbered"
    assert not [m for m in mappings if m.status == "removed"]

    # 工单引用拆分的旧条款时，两条都迁移
    (mig,) = migrate_references(mappings, ["5.2"])
    assert mig["auto"] is True
    assert mig["new"] == ["5.3", "5.4"]
    assert migrate_text("验收标准见第 5.2 项。", mappings) == \
        "验收标准见第 5.3、5.4 项。"


def test_split_without_confirmation_is_not_guessed(tmp_path):
    old_path, new_path = _split_pair(tmp_path)
    mappings = align_documents(extract_clauses(old_path),
                               extract_clauses(new_path))
    by_old = _by_old(mappings)
    # 未人工确认时，旧 5.2 不会被猜成 1->2 拆分
    assert by_old["5.2"].status != "split"
    assert not (by_old["5.2"].auto and len(by_old["5.2"].new) > 1)


# ---------- CLI ----------

def test_cli_writes_report(tmp_path):
    old_path, new_path = _shift_pair(tmp_path)
    refs = tmp_path / "refs.json"
    refs.write_text(json.dumps(["2.2"]), encoding="utf-8")
    out = tmp_path / "report.json"
    rc = cli_main([str(old_path), str(new_path),
                   "--refs", str(refs), "-o", str(out)])
    assert rc == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    (mig,) = report["migrations"]
    assert mig["ref"] == "2.2"
    assert mig["new"] == ["2.3"]
