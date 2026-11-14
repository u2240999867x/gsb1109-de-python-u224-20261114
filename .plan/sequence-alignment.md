# Plan: 双卷循环计数序列对齐

## 背景理解

- 设备把 **时间列** 和 **值列** 写进两个独立压缩卷（独立文件/流），卷边界不同（两卷行数可不同）。
- 每行只带 1 字节循环计数 `0..255`（mod-256 序号），卷头偶有完整序号锚点（absolute sequence number anchor）。
- 要求一次顺读（single streaming pass，O(窗口) 内存）把两列接成事件。
- 允许给定上限 `max_gap` 内的漏行（missing rows）或重传（duplicates）。
- 两个相邻锚点间若存在两种都合法的对齐 → 把不能确定的范围标记为复核（ambiguous）。
- 后面有唯一锚点的行要能恢复（歧义不传染，锚点重新确定对齐）。
- 一侧多出一个重复 `255` 时，不能让下一轮（wrap 之后）全部平移一位 —— 即重传必须被识别为同序号重复，而不是被当作“前进”导致后续永久错位。

## 设计

### 数据模型
- `Record(counter: int, value, anchor: int | None, index)`：卷内顺读记录；`anchor` 为该卷头给出的完整序号（若该行是锚点行）。
- `Event(seq, time, value, status)`：对齐输出；`status ∈ {ok, ambiguous, missing_time, missing_value}`。
- 模块 `src/xopen/sequence.py`（纯算法、零三方依赖、可流式）。

### 单卷：计数器 → 绝对序号（核心状态机）
状态：`base`（已确认的最近完整序号基准）、`expect`（下一个期望计数）、`ambiguous` 区间。
- 锚点行：校验 `anchor % 256 == counter`（冲突 → 报错/复核）；用锚点重置基准；清空之前未决歧义。
- 普通行：给定计数 c，候选绝对序号集合
  `S(c) = {base + ((c - expect) mod 256) + 256*k}`，再用单调约束（>= 上一行绝对序号）与
  漏行/重传预算（步长 delta ∈ {0（重传）, 1（正常）, 2..max_gap+1（漏行）}）筛选。
  - 唯一候选 → 确定；多个候选 → 进入歧义，直到下一锚点（锚点处按实际偏移裁决，歧义区间整体标记复核，锚点之后恢复确定）。
- 重复 255：delta=0 视为重传（同绝对序号），`expect` 不前进；下一个 0 仍按 delta=1 接在同一序号之后，杜绝整轮平移。

### 双卷对齐（merge on absolute sequence number）
- 每卷各自顺读并解析出带绝对序号（或歧义标记）的记录。
- 归并：以两侧当前最小绝对序号配对；
  - 序号相等 → 合成事件；
  - 一侧序号小 → 该事件缺另一列（missing_time / missing_value），消耗小的一侧；
  - 序号差超过 `max_gap` → 同样按缺失处理（不做跨窗口猜测）。
- 任一侧处于歧义区间 → 合成事件标 `ambiguous`，交复核；锚点之后恢复 `ok`。
- 全程只保留窗口内未决记录，满足“一次顺读”。

## 步骤
1. 实现 `src/xopen/sequence.py`：单卷解析状态机 + 双卷归并 + 数据类与异常。
2. 在 `tests/test_sequence.py` 写覆盖：基本对齐、漏行、重传、重复 255 不平移、锚点恢复、歧义复核、卷边界独立、流式性质。
3. `pytest -q` 全量跑通；ruff/black 风格检查。
4. 更新 README 或模块 docstring 说明用法（模块级 docstring 为主）。
