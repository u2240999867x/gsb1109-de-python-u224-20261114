# 注释率逐文件重算差异账

## 口径定义

- 注释 token 完全由 `tokenize` 判定；三引号字面值内部的 `#` 不是 COMMENT，
  不会产生注释行（脚本内置自检覆盖该用例）。
- 字符串角色由 `ast` 判定：真文档字符串、独立字符串表达式、赋值右值、
  其他上下文。radon 原始实现把所有独立多行字符串一律假设为文档字符串。
- 物理行互斥分区恒等式：
  `loc = blank + comment_only + docstring_nonblank +
  standalone_string_nonblank + code`。
- `eol_comments`（行尾注释）与 `assigned_multiline_nonblank`
  （赋给变量的多行字面值）是 `code` 的子集备忘项，不参与分区恒等式。
- 分子两口径：窄口径仅 `#` 注释；宽口径另加真文档字符串，由调用方开关。
  `legacy` 列保留 radon 式宽口径（独立字符串一律算注释）供对照。
- 本次调用方选择：文档字符串纳入分子。
- 分母两种：`loc`（全部物理行）与 `loc - blank`（非空物理行），
  二者都能对回原始行数。

## 与 radon.raw.analyze 的对账

| 文件 | loc差 | 空行差 | 注释差 | radon multi | radon single_comments |
| --- | ---: | ---: | ---: | ---: | ---: |
| docs/conf.py | 0 | 0 | 0 | 0 | 141 |
| radon/__init__.py | 0 | 0 | 0 | 2 | 1 |
| radon/__main__.py | 0 | 0 | 0 | 0 | 1 |
| radon/cli/__init__.py | 0 | 0 | 0 | 117 | 10 |
| radon/cli/colors.py | 0 | 0 | 0 | 3 | 1 |
| radon/cli/harvest.py | 0 | 0 | 0 | 41 | 32 |
| radon/cli/tools.py | 0 | 0 | 0 | 68 | 30 |
| radon/complexity.py | 0 | 0 | 0 | 47 | 1 |
| radon/contrib/__init__.py | 0 | 0 | 0 | 0 | 0 |
| radon/contrib/flake8.py | 0 | 0 | 0 | 0 | 5 |
| radon/metrics.py | 0 | 0 | 0 | 48 | 6 |
| radon/raw.py | 0 | 0 | 0 | 57 | 31 |
| radon/tests/__init__.py | 0 | 0 | 0 | 0 | 0 |
| radon/tests/conftest.py | 0 | 0 | 0 | 0 | 0 |
| radon/tests/data/__init__.py | 0 | 0 | 0 | 0 | 1 |
| radon/tests/data/no_encoding.py | 0 | 0 | 0 | 0 | 0 |
| radon/tests/data/py3unicode.py | 0 | 0 | 0 | 0 | 1 |
| radon/tests/run.py | 0 | 0 | 0 | 0 | 3 |
| radon/tests/test_cli.py | 0 | 0 | 0 | 0 | 0 |
| radon/tests/test_cli_colors.py | 0 | 0 | 0 | 0 | 0 |
| radon/tests/test_cli_harvest.py | 0 | 0 | 0 | 0 | 0 |
| radon/tests/test_cli_tools.py | 0 | 0 | 0 | 0 | 1 |
| radon/tests/test_complexity_utils.py | 0 | 0 | 0 | 0 | 3 |
| radon/tests/test_complexity_visitor.py | 0 | 0 | 0 | 0 | 4 |
| radon/tests/test_halstead.py | 0 | 0 | 0 | 0 | 0 |
| radon/tests/test_ipynb.py | 0 | 0 | 0 | 0 | 1 |
| radon/tests/test_other_metrics.py | 0 | 0 | 0 | 0 | 2 |
| radon/tests/test_raw.py | 0 | 0 | 0 | 0 | 0 |
| radon/visitors.py | 0 | 0 | 0 | 81 | 46 |
| setup.py | 0 | 0 | 0 | 0 | 0 |

## 逐文件分区与四类贡献

| 文件 | loc | 物理空行 | 纯注释行 | 行尾注释(⊂code) | 文档字符串(非空行) | 文档字符串内空行(⊂blank) | 独立字符串表达式 | 赋值型多行字面值(⊂code) | 其他多行字符串(⊂code) | code |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| docs/conf.py | 253 | 65 | 147 | 0 | 0 | 0 | 0 | 0 | 0 | 41 |
| radon/__init__.py | 21 | 6 | 0 | 0 | 3 | 0 | 0 | 0 | 0 | 12 |
| radon/__main__.py | 4 | 1 | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 2 |
| radon/cli/__init__.py | 433 | 52 | 2 | 1 | 125 | 9 | 0 | 0 | 0 | 254 |
| radon/cli/colors.py | 49 | 9 | 1 | 1 | 3 | 0 | 0 | 0 | 0 | 36 |
| radon/cli/harvest.py | 449 | 66 | 2 | 0 | 71 | 9 | 0 | 0 | 0 | 310 |
| radon/cli/tools.py | 597 | 73 | 23 | 0 | 75 | 7 | 0 | 26 | 0 | 426 |
| radon/complexity.py | 108 | 24 | 1 | 0 | 47 | 9 | 0 | 0 | 0 | 36 |
| radon/contrib/__init__.py | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| radon/contrib/flake8.py | 72 | 9 | 0 | 2 | 5 | 0 | 0 | 0 | 0 | 58 |
| radon/metrics.py | 157 | 26 | 4 | 0 | 50 | 7 | 0 | 3 | 0 | 77 |
| radon/raw.py | 245 | 46 | 30 | 3 | 58 | 14 | 0 | 0 | 0 | 111 |
| radon/tests/__init__.py | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| radon/tests/conftest.py | 31 | 9 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 22 |
| radon/tests/data/__init__.py | 5 | 2 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 2 |
| radon/tests/data/no_encoding.py | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 2 |
| radon/tests/data/py3unicode.py | 5 | 2 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 2 |
| radon/tests/run.py | 15 | 2 | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 10 |
| radon/tests/test_cli.py | 282 | 60 | 2 | 0 | 0 | 0 | 0 | 0 | 6 | 220 |
| radon/tests/test_cli_colors.py | 22 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 14 |
| radon/tests/test_cli_harvest.py | 392 | 77 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 315 |
| radon/tests/test_cli_tools.py | 616 | 62 | 1 | 1 | 0 | 0 | 0 | 7 | 42 | 553 |
| radon/tests/test_complexity_utils.py | 118 | 25 | 3 | 0 | 0 | 0 | 0 | 14 | 0 | 90 |
| radon/tests/test_complexity_visitor.py | 754 | 57 | 7 | 0 | 0 | 0 | 0 | 347 | 6 | 690 |
| radon/tests/test_halstead.py | 113 | 10 | 0 | 0 | 0 | 0 | 0 | 43 | 8 | 103 |
| radon/tests/test_ipynb.py | 141 | 19 | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 121 |
| radon/tests/test_other_metrics.py | 169 | 28 | 11 | 0 | 0 | 0 | 0 | 35 | 0 | 130 |
| radon/tests/test_raw.py | 467 | 21 | 5 | 0 | 0 | 0 | 0 | 248 | 0 | 441 |
| radon/visitors.py | 460 | 64 | 29 | 0 | 98 | 5 | 0 | 0 | 0 | 269 |
| setup.py | 61 | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 58 |
| **TOTAL** | 6041 | 826 | 273 | 8 | 537 | 60 | 0 | 723 | 62 | 4405 |

## 两种口径的注释率

| 文件 | 窄分子 | 宽分子(含文档字符串) | legacy分子 | 窄/loc | 宽/loc | 窄/非空行 | 宽/非空行 | 宽-窄 百分点(loc) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| docs/conf.py | 147 | 147 | 147 | 58.10% | 58.10% | 78.19% | 78.19% | +0.00 |
| radon/__init__.py | 0 | 3 | 3 | 0.00% | 14.29% | 0.00% | 20.00% | +14.29 |
| radon/__main__.py | 0 | 1 | 1 | 0.00% | 25.00% | 0.00% | 33.33% | +25.00 |
| radon/cli/__init__.py | 3 | 128 | 128 | 0.69% | 29.56% | 0.79% | 33.60% | +28.87 |
| radon/cli/colors.py | 2 | 5 | 5 | 4.08% | 10.20% | 5.00% | 12.50% | +6.12 |
| radon/cli/harvest.py | 2 | 73 | 73 | 0.45% | 16.26% | 0.52% | 19.06% | +15.81 |
| radon/cli/tools.py | 23 | 98 | 98 | 3.85% | 16.42% | 4.39% | 18.70% | +12.56 |
| radon/complexity.py | 1 | 48 | 48 | 0.93% | 44.44% | 1.19% | 57.14% | +43.52 |
| radon/contrib/__init__.py | 0 | 0 | 0 | - | - | - | - | +0.00 |
| radon/contrib/flake8.py | 2 | 7 | 7 | 2.78% | 9.72% | 3.17% | 11.11% | +6.94 |
| radon/metrics.py | 4 | 54 | 54 | 2.55% | 34.39% | 3.05% | 41.22% | +31.85 |
| radon/raw.py | 33 | 91 | 91 | 13.47% | 37.14% | 16.58% | 45.73% | +23.67 |
| radon/tests/__init__.py | 0 | 0 | 0 | - | - | - | - | +0.00 |
| radon/tests/conftest.py | 0 | 0 | 0 | 0.00% | 0.00% | 0.00% | 0.00% | +0.00 |
| radon/tests/data/__init__.py | 1 | 1 | 1 | 20.00% | 20.00% | 33.33% | 33.33% | +0.00 |
| radon/tests/data/no_encoding.py | 0 | 0 | 0 | 0.00% | 0.00% | 0.00% | 0.00% | +0.00 |
| radon/tests/data/py3unicode.py | 1 | 1 | 1 | 20.00% | 20.00% | 33.33% | 33.33% | +0.00 |
| radon/tests/run.py | 3 | 3 | 3 | 20.00% | 20.00% | 23.08% | 23.08% | +0.00 |
| radon/tests/test_cli.py | 2 | 2 | 2 | 0.71% | 0.71% | 0.90% | 0.90% | +0.00 |
| radon/tests/test_cli_colors.py | 0 | 0 | 0 | 0.00% | 0.00% | 0.00% | 0.00% | +0.00 |
| radon/tests/test_cli_harvest.py | 0 | 0 | 0 | 0.00% | 0.00% | 0.00% | 0.00% | +0.00 |
| radon/tests/test_cli_tools.py | 2 | 2 | 2 | 0.32% | 0.32% | 0.36% | 0.36% | +0.00 |
| radon/tests/test_complexity_utils.py | 3 | 3 | 3 | 2.54% | 2.54% | 3.23% | 3.23% | +0.00 |
| radon/tests/test_complexity_visitor.py | 7 | 7 | 7 | 0.93% | 0.93% | 1.00% | 1.00% | +0.00 |
| radon/tests/test_halstead.py | 0 | 0 | 0 | 0.00% | 0.00% | 0.00% | 0.00% | +0.00 |
| radon/tests/test_ipynb.py | 0 | 1 | 1 | 0.00% | 0.71% | 0.00% | 0.82% | +0.71 |
| radon/tests/test_other_metrics.py | 11 | 11 | 11 | 6.51% | 6.51% | 7.80% | 7.80% | +0.00 |
| radon/tests/test_raw.py | 5 | 5 | 5 | 1.07% | 1.07% | 1.12% | 1.12% | +0.00 |
| radon/visitors.py | 29 | 127 | 127 | 6.30% | 27.61% | 7.32% | 32.07% | +21.30 |
| setup.py | 0 | 0 | 0 | 0.00% | 0.00% | 0.00% | 0.00% | +0.00 |
| **TOTAL** | 281 | 818 | 818 | 4.65% | 13.54% | 5.39% | 15.69% | +8.89 |

## 调用方当前选择的分子

选中口径：宽口径（含文档字符串）

| 文件 | 分子 | 分母 loc | 比率 | 分母 非空行 | 比率 |
| --- | ---: | ---: | ---: | ---: | ---: |
| docs/conf.py | 147 | 253 | 58.10% | 188 | 78.19% |
| radon/__init__.py | 3 | 21 | 14.29% | 15 | 20.00% |
| radon/__main__.py | 1 | 4 | 25.00% | 3 | 33.33% |
| radon/cli/__init__.py | 128 | 433 | 29.56% | 381 | 33.60% |
| radon/cli/colors.py | 5 | 49 | 10.20% | 40 | 12.50% |
| radon/cli/harvest.py | 73 | 449 | 16.26% | 383 | 19.06% |
| radon/cli/tools.py | 98 | 597 | 16.42% | 524 | 18.70% |
| radon/complexity.py | 48 | 108 | 44.44% | 84 | 57.14% |
| radon/contrib/__init__.py | 0 | 0 | - | 0 | - |
| radon/contrib/flake8.py | 7 | 72 | 9.72% | 63 | 11.11% |
| radon/metrics.py | 54 | 157 | 34.39% | 131 | 41.22% |
| radon/raw.py | 91 | 245 | 37.14% | 199 | 45.73% |
| radon/tests/__init__.py | 0 | 0 | - | 0 | - |
| radon/tests/conftest.py | 0 | 31 | 0.00% | 22 | 0.00% |
| radon/tests/data/__init__.py | 1 | 5 | 20.00% | 3 | 33.33% |
| radon/tests/data/no_encoding.py | 0 | 2 | 0.00% | 2 | 0.00% |
| radon/tests/data/py3unicode.py | 1 | 5 | 20.00% | 3 | 33.33% |
| radon/tests/run.py | 3 | 15 | 20.00% | 13 | 23.08% |
| radon/tests/test_cli.py | 2 | 282 | 0.71% | 222 | 0.90% |
| radon/tests/test_cli_colors.py | 0 | 22 | 0.00% | 14 | 0.00% |
| radon/tests/test_cli_harvest.py | 0 | 392 | 0.00% | 315 | 0.00% |
| radon/tests/test_cli_tools.py | 2 | 616 | 0.32% | 554 | 0.36% |
| radon/tests/test_complexity_utils.py | 3 | 118 | 2.54% | 93 | 3.23% |
| radon/tests/test_complexity_visitor.py | 7 | 754 | 0.93% | 697 | 1.00% |
| radon/tests/test_halstead.py | 0 | 113 | 0.00% | 103 | 0.00% |
| radon/tests/test_ipynb.py | 1 | 141 | 0.71% | 122 | 0.82% |
| radon/tests/test_other_metrics.py | 11 | 169 | 6.51% | 141 | 7.80% |
| radon/tests/test_raw.py | 5 | 467 | 1.07% | 446 | 1.12% |
| radon/visitors.py | 127 | 460 | 27.61% | 396 | 32.07% |
| setup.py | 0 | 61 | 0.00% | 58 | 0.00% |
