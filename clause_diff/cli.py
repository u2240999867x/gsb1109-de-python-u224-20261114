"""命令行入口：

    python -m clause_diff.cli OLD.docx NEW.docx \
        [--confirm confirmations.json] [--refs refs.json] [-o report.json]

confirmations.json: [{"old": "5.2", "new": ["5.3", "5.4"]}, ...]  人工确认的对应（含拆分）
refs.json:          ["4.2", ...] 或 {"references": [...]}          工单引用的旧条款号
"""

import argparse
import json
import sys

from .align import align_documents, migrate_references
from .docx_extract import extract_clauses


def main(argv=None):
    parser = argparse.ArgumentParser(prog="clause_diff",
                                     description="两份 DOCX 间的条款对照与工单迁移")
    parser.add_argument("old_docx")
    parser.add_argument("new_docx")
    parser.add_argument("--confirm", help="人工确认的对应关系 JSON")
    parser.add_argument("--refs", help="工单引用的旧条款号 JSON")
    parser.add_argument("-o", "--output", help="对照报告输出路径（JSON）")
    args = parser.parse_args(argv)

    confirmations = []
    if args.confirm:
        with open(args.confirm, encoding="utf-8") as f:
            confirmations = json.load(f)

    old_clauses = extract_clauses(args.old_docx)
    new_clauses = extract_clauses(args.new_docx)
    mappings = align_documents(old_clauses, new_clauses, confirmations)

    report = {
        "old": args.old_docx,
        "new": args.new_docx,
        "mappings": [
            {"old": m.old, "new": m.new, "status": m.status,
             "auto": m.auto, "note": m.note}
            for m in mappings
        ],
    }

    print(f"{'旧条款':<10} {'新条款':<12} 状态")
    for m in mappings:
        old = m.old or "—"
        new = "、".join(m.new) if m.new else "—"
        note = f"（{m.note}）" if m.note else ""
        print(f"{old:<10} {new:<12} {m.status}{note}")

    if args.refs:
        with open(args.refs, encoding="utf-8") as f:
            payload = json.load(f)
        refs = payload["references"] if isinstance(payload, dict) else payload
        migrations = migrate_references(mappings, refs)
        report["migrations"] = migrations
        print("\n工单迁移：")
        for mig in migrations:
            if mig["auto"]:
                print(f"  第 {mig['ref']} 项 -> 第 {'、'.join(mig['new'])} 项"
                      f"（{mig.get('status', '')}）")
            else:
                cands = mig.get("candidates")
                extra = f"，候选：{'、'.join(cands)}" if cands else ""
                print(f"  第 {mig['ref']} 项 -> 不自动迁移：{mig['reason']}{extra}")

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
