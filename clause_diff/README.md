# clause_diff — 两份 DOCX 间的条款对照与工单迁移

## 用法

```bash
python -m clause_diff.cli 旧版.docx 新版.docx \
    [--confirm confirmations.json] [--refs refs.json] [-o report.json]
```

- `confirmations.json`：人工确认的对应关系，支持一对多拆分
  `[{"old": "5.2", "new": ["5.3", "5.4"]}]`
- `refs.json`：工单引用的旧条款号 `["4.2", ...]`

## 规则

1. **证据来源**：条款编号取自 DOCX 自动编号实例（numbering.xml 还原）
   或标题/正文中的字面编号；层级取自标题样式与编号级别；对齐依据条款内容。
2. **顺序保持**：对齐在同一父项内递归进行，不允许交叉匹配。
3. **重复短句**：归一化后长度不超过 20 字的短句若在任一文档中出现多次，
   证据不唯一，标记 `ambiguous`，工单不自动迁移，仅列出候选。
4. **拆分**：旧条款一分为二且人工确认两者均承接时，保留两条对应
   （`split`）；锚点之后的条款继续按内容对齐，不会全部判为改写。

## 状态

`same` / `renumbered` / `modified` / `split` / `confirmed` / `added` /
`removed` / `ambiguous`。工单只对 `auto=true` 的对应自动迁移。

## 测试

```bash
.venv/bin/python -m pytest clause_diff/
```
