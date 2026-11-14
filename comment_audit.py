#!/usr/bin/env python3
"""Recompute comment-line statistics from source with two explicit calibers.

Line classification is rebuilt from primitives instead of regexes:

* ``tokenize`` decides what a real comment is.  A ``#`` inside a triple-quoted
  literal never becomes a ``COMMENT`` token, so it cannot create a comment line.
* ``ast`` decides the *role* of a string literal: true docstring, standalone
  string expression, assignment RHS literal, or anything else.

Every physical line lands in exactly one partition bucket, so the denominator
always reconciles to the raw line count::

    loc = blank + comment_only + docstring_nonblank
          + standalone_string_nonblank + code

Two memo categories (EOL comments, assigned multi-line literals) are subsets of
``code`` lines and are reported separately because they explain dashboard
caliber changes without breaking the identity above.

Numerator calibers:

* narrow  : comment_only + eol_comments          (``#`` comments only)
* wide    : narrow + docstring_nonblank          (caller toggle, --docstrings)
* legacy  : narrow + docstring + standalone      (radon's "assumed docstring")
"""

from __future__ import annotations

import argparse
import ast
import csv
import io
import pathlib
import subprocess
import tokenize
from dataclasses import dataclass, asdict

TRIVIA = {
    tokenize.NL,
    tokenize.NEWLINE,
    tokenize.INDENT,
    tokenize.DEDENT,
    tokenize.ENDMARKER,
    tokenize.ENCODING,
}


@dataclass
class FileStats:
    path: str
    loc: int = 0
    blank: int = 0
    comment_only: int = 0
    eol_comments: int = 0
    docstring_nonblank: int = 0
    docstring_blank: int = 0
    standalone_string_nonblank: int = 0
    assigned_multiline_nonblank: int = 0
    other_multiline_nonblank: int = 0
    code: int = 0
    narrow_numerator: int = 0
    wide_numerator: int = 0
    legacy_numerator: int = 0

    @property
    def comments_total(self) -> int:
        return self.comment_only + self.eol_comments


def is_string_node(node: ast.AST) -> bool:
    if isinstance(node, ast.JoinedStr):
        return True
    return isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes))


def multiline(node: ast.AST) -> bool:
    return node.end_lineno is not None and node.end_lineno > node.lineno


def collect_string_roles(tree: ast.AST):
    """Return (docstring nodes, standalone nodes, assigned nodes, other nodes)."""
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    doc_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ) and node.body:
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                doc_ids.add(id(first.value))

    standalone, assigned, other = [], [], []
    for node in ast.walk(tree):
        if not is_string_node(node):
            continue
        parent = parents.get(node)
        if id(node) in doc_ids:
            continue  # handled by the caller through doc_ids
        if isinstance(parent, ast.Expr):
            standalone.append(node)
        elif inside_assignment_value(node, parents):
            assigned.append(node)
        else:
            other.append(node)

    doc_nodes = [
        node
        for node in ast.walk(tree)
        if id(node) in doc_ids
    ]
    return doc_nodes, standalone, assigned, other


def inside_assignment_value(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    current = node
    while current in parents:
        parent = parents[current]
        if isinstance(parent, ast.Assign) and current is parent.value:
            return True
        if isinstance(parent, ast.AnnAssign) and current is parent.value:
            return True
        if isinstance(parent, ast.AugAssign) and current is parent.value:
            return True
        current = parent
    return False


def span_nonblank(lines: list[str], node: ast.AST) -> tuple[int, int]:
    """Count (non-blank, blank) physical lines covered by a node span."""
    span = lines[node.lineno - 1: node.end_lineno]
    nonblank = sum(1 for line in span if line.strip())
    return nonblank, len(span) - nonblank


def classify_comments(source: str) -> tuple[int, int, set[int]]:
    """Comment-only lines, EOL comments, and the set of lines carrying code."""
    code_lines: set[int] = set()
    comment_lines: set[int] = set()
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    for tok in tokens:
        kind = tok.type
        row = tok.start[0]
        if kind == tokenize.COMMENT:
            comment_lines.add(row)
        elif kind not in TRIVIA:
            code_lines.add(row)
    comment_only = len(comment_lines - code_lines)
    eol = len(comment_lines & code_lines)
    return comment_only, eol, code_lines


def analyze_file(path: pathlib.Path) -> FileStats:
    with tokenize.open(path) as handle:
        source = handle.read()
    physical_lines = source.splitlines()
    loc = len(physical_lines)
    blank = sum(1 for line in physical_lines if not line.strip())

    comment_only, eol, _ = classify_comments(source)
    tree = ast.parse(source, filename=str(path))
    doc_nodes, standalone, assigned, other = collect_string_roles(tree)

    doc_nonblank = doc_blank = 0
    for node in doc_nodes:
        nb, bl = span_nonblank(physical_lines, node)
        doc_nonblank += nb
        doc_blank += bl

    standalone_nonblank = sum(
        span_nonblank(physical_lines, node)[0] for node in standalone
    )
    assigned_nonblank = sum(
        span_nonblank(physical_lines, node)[0]
        for node in assigned
        if multiline(node)
    )
    other_nonblank = sum(
        span_nonblank(physical_lines, node)[0]
        for node in other
        if multiline(node)
    )

    stats = FileStats(path=str(path))
    stats.loc = loc
    stats.blank = blank
    stats.comment_only = comment_only
    stats.eol_comments = eol
    stats.docstring_nonblank = doc_nonblank
    stats.docstring_blank = doc_blank
    stats.standalone_string_nonblank = standalone_nonblank
    stats.assigned_multiline_nonblank = assigned_nonblank
    stats.other_multiline_nonblank = other_nonblank
    stats.code = (
        loc - blank - comment_only - doc_nonblank - standalone_nonblank
    )
    stats.narrow_numerator = comment_only + eol
    stats.wide_numerator = stats.narrow_numerator + doc_nonblank
    stats.legacy_numerator = (
        stats.narrow_numerator + doc_nonblank + standalone_nonblank
    )

    # Invariant: the exclusive partition must reconcile to physical lines.
    assert (
        stats.blank
        + stats.comment_only
        + stats.docstring_nonblank
        + stats.standalone_string_nonblank
        + stats.code
        == loc
    ), f"partition does not reconcile for {path}"
    return stats


def self_test() -> None:
    """A '#' inside a triple-quoted literal must not become a comment."""
    sample = (
        'x = """not a comment\n'
        '# inside the literal, still not a comment\n'
        '"""\n'
        '# a real comment\n'
        'y = 1  # an EOL comment\n'
        '\n'
    )
    stats = analyze_file_from_text(sample)
    assert stats.comment_only == 1, stats
    assert stats.eol_comments == 1, stats
    assert stats.comments_total == 2, stats
    assert stats.assigned_multiline_nonblank == 3, stats
    assert stats.blank == 1, stats
    assert stats.loc == 6, stats
    assert stats.code == 4, stats  # 3 literal lines + the EOL code line


def analyze_file_from_text(source: str) -> FileStats:
    """Same pipeline as analyze_file but on in-memory text."""
    physical_lines = source.splitlines()
    loc = len(physical_lines)
    blank = sum(1 for line in physical_lines if not line.strip())
    comment_only, eol, _ = classify_comments(source)
    tree = ast.parse(source)
    doc_nodes, standalone, assigned, other = collect_string_roles(tree)
    doc_nonblank = sum(span_nonblank(physical_lines, n)[0] for n in doc_nodes)
    doc_blank = sum(span_nonblank(physical_lines, n)[1] for n in doc_nodes)
    stats = FileStats(path="<self-test>")
    stats.loc = loc
    stats.blank = blank
    stats.comment_only = comment_only
    stats.eol_comments = eol
    stats.docstring_nonblank = doc_nonblank
    stats.docstring_blank = doc_blank
    stats.standalone_string_nonblank = sum(
        span_nonblank(physical_lines, n)[0] for n in standalone
    )
    stats.assigned_multiline_nonblank = sum(
        span_nonblank(physical_lines, n)[0] for n in assigned if multiline(n)
    )
    stats.other_multiline_nonblank = sum(
        span_nonblank(physical_lines, n)[0] for n in other if multiline(n)
    )
    stats.code = (
        loc - blank - comment_only - doc_nonblank
        - stats.standalone_string_nonblank
    )
    stats.narrow_numerator = comment_only + eol
    stats.wide_numerator = stats.narrow_numerator + doc_nonblank
    stats.legacy_numerator = (
        stats.narrow_numerator + doc_nonblank
        + stats.standalone_string_nonblank
    )
    return stats


def default_python_files(root: pathlib.Path) -> list[pathlib.Path]:
    try:
        output = subprocess.check_output(
            ["git", "ls-files", "*.py"], cwd=root, text=True
        )
        files = [root / line for line in output.splitlines()]
        if files:
            return sorted(files)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return sorted(root.rglob("*.py"))


def percent(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "-"
    return f"{numerator / denominator * 100:.2f}%"


def reconcile_with_radon(stats: FileStats) -> dict[str, int]:
    """Cross-check token-level counts against radon.raw.analyze."""
    from radon.raw import analyze

    with tokenize.open(stats.path) as handle:
        source = handle.read()
    module = analyze(source)
    return {
        "loc_diff": stats.loc - module.loc,
        "blank_diff": stats.blank - module.blank,
        "comments_diff": stats.comments_total - module.comments,
        "radon_multi": module.multi,
        "radon_single_comments": module.single_comments,
        "radon_sloc": module.sloc,
    }


def write_csv(rows: list[FileStats], destination: pathlib.Path) -> None:
    fields = list(asdict(rows[0]).keys()) + [
        "rate_narrow_over_loc",
        "rate_wide_over_loc",
        "rate_narrow_over_nonblank",
        "rate_wide_over_nonblank",
    ]
    with destination.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            data = asdict(row)
            nonblank = row.loc - row.blank
            data["rate_narrow_over_loc"] = percent(row.narrow_numerator, row.loc)
            data["rate_wide_over_loc"] = percent(row.wide_numerator, row.loc)
            data["rate_narrow_over_nonblank"] = percent(
                row.narrow_numerator, nonblank
            )
            data["rate_wide_over_nonblank"] = percent(
                row.wide_numerator, nonblank
            )
            writer.writerow(data)


def write_markdown(
    rows: list[FileStats],
    radon_checks: dict[str, dict[str, int]],
    destination: pathlib.Path,
    include_docstrings: bool,
) -> None:
    total = FileStats(path="TOTAL")
    for row in rows:
        for field in asdict(row):
            if field == "path":
                continue
            setattr(total, field, getattr(total, field) + getattr(row, field))

    chosen = "wide_numerator" if include_docstrings else "narrow_numerator"
    lines = [
        "# 注释率逐文件重算差异账",
        "",
        "## 口径定义",
        "",
        "- 注释 token 完全由 `tokenize` 判定；三引号字面值内部的 `#` 不是 COMMENT，",
        "  不会产生注释行（脚本内置自检覆盖该用例）。",
        "- 字符串角色由 `ast` 判定：真文档字符串、独立字符串表达式、赋值右值、",
        "  其他上下文。radon 原始实现把所有独立多行字符串一律假设为文档字符串。",
        "- 物理行互斥分区恒等式：",
        "  `loc = blank + comment_only + docstring_nonblank +",
        "  standalone_string_nonblank + code`。",
        "- `eol_comments`（行尾注释）与 `assigned_multiline_nonblank`",
        "  （赋给变量的多行字面值）是 `code` 的子集备忘项，不参与分区恒等式。",
        "- 分子两口径：窄口径仅 `#` 注释；宽口径另加真文档字符串，由调用方开关。",
        "  `legacy` 列保留 radon 式宽口径（独立字符串一律算注释）供对照。",
        f"- 本次调用方选择：文档字符串{'纳入' if include_docstrings else '不纳入'}分子。",
        "- 分母两种：`loc`（全部物理行）与 `loc - blank`（非空物理行），",
        "  二者都能对回原始行数。",
        "",
        "## 与 radon.raw.analyze 的对账",
        "",
        "| 文件 | loc差 | 空行差 | 注释差 | radon multi | radon single_comments |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        check = radon_checks[row.path]
        lines.append(
            f"| {row.path} | {check['loc_diff']} | {check['blank_diff']} | "
            f"{check['comments_diff']} | {check['radon_multi']} | "
            f"{check['radon_single_comments']} |"
        )

    lines += [
        "",
        "## 逐文件分区与四类贡献",
        "",
        "| 文件 | loc | 物理空行 | 纯注释行 | 行尾注释(⊂code) | 文档字符串(非空行) | "
        "文档字符串内空行(⊂blank) | 独立字符串表达式 | 赋值型多行字面值(⊂code) | "
        "其他多行字符串(⊂code) | code |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| {row.path} | {row.loc} | {row.blank} | {row.comment_only} | "
            f"{row.eol_comments} | {row.docstring_nonblank} | "
            f"{row.docstring_blank} | {row.standalone_string_nonblank} | "
            f"{row.assigned_multiline_nonblank} | "
            f"{row.other_multiline_nonblank} | {row.code} |"
        )
    lines.append(
        f"| **TOTAL** | {total.loc} | {total.blank} | {total.comment_only} | "
        f"{total.eol_comments} | {total.docstring_nonblank} | "
        f"{total.docstring_blank} | {total.standalone_string_nonblank} | "
        f"{total.assigned_multiline_nonblank} | "
        f"{total.other_multiline_nonblank} | {total.code} |"
    )

    lines += [
        "",
        "## 两种口径的注释率",
        "",
        "| 文件 | 窄分子 | 宽分子(含文档字符串) | legacy分子 | 窄/loc | 宽/loc | "
        "窄/非空行 | 宽/非空行 | 宽-窄 百分点(loc) |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        nonblank = row.loc - row.blank
        delta = (
            (row.wide_numerator - row.narrow_numerator) / row.loc * 100
            if row.loc
            else 0.0
        )
        lines.append(
            f"| {row.path} | {row.narrow_numerator} | {row.wide_numerator} | "
            f"{row.legacy_numerator} | "
            f"{percent(row.narrow_numerator, row.loc)} | "
            f"{percent(row.wide_numerator, row.loc)} | "
            f"{percent(row.narrow_numerator, nonblank)} | "
            f"{percent(row.wide_numerator, nonblank)} | {delta:+.2f} |"
        )
    nonblank_total = total.loc - total.blank
    delta_total = (
        (total.wide_numerator - total.narrow_numerator) / total.loc * 100
    )
    lines.append(
        f"| **TOTAL** | {total.narrow_numerator} | {total.wide_numerator} | "
        f"{total.legacy_numerator} | "
        f"{percent(total.narrow_numerator, total.loc)} | "
        f"{percent(total.wide_numerator, total.loc)} | "
        f"{percent(total.narrow_numerator, nonblank_total)} | "
        f"{percent(total.wide_numerator, nonblank_total)} | {delta_total:+.2f} |"
    )

    lines += [
        "",
        "## 调用方当前选择的分子",
        "",
        f"选中口径：{'宽口径（含文档字符串）' if include_docstrings else '窄口径（仅 # 注释）'}",
        "",
        "| 文件 | 分子 | 分母 loc | 比率 | 分母 非空行 | 比率 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        numerator = getattr(row, chosen)
        nonblank = row.loc - row.blank
        lines.append(
            f"| {row.path} | {numerator} | {row.loc} | "
            f"{percent(numerator, row.loc)} | {nonblank} | "
            f"{percent(numerator, nonblank)} |"
        )
    destination.write_text("\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=pathlib.Path)
    parser.add_argument(
        "--docstrings",
        dest="include_docstrings",
        action="store_true",
        help="include true docstrings in the selected numerator",
    )
    parser.add_argument(
        "--no-docstrings",
        dest="include_docstrings",
        action="store_false",
        help="keep the numerator limited to '#' comments (default)",
    )
    parser.set_defaults(include_docstrings=False)
    parser.add_argument("--out-dir", type=pathlib.Path, default=pathlib.Path("."))
    args = parser.parse_args()

    self_test()
    root = pathlib.Path.cwd()
    files = args.paths or default_python_files(root)
    rows = [analyze_file(path) for path in files]

    radon_checks = {}
    for row in rows:
        check = reconcile_with_radon(row)
        radon_checks[row.path] = check
        assert check["loc_diff"] == 0, row.path
        assert check["blank_diff"] == 0, row.path
        assert check["comments_diff"] == 0, row.path

    display_paths = {row.path: str(pathlib.Path(row.path).relative_to(root))
                     for row in rows}
    for row in rows:
        row.path = display_paths[row.path]
    radon_checks = {display_paths[key]: value
                    for key, value in radon_checks.items()}

    args.out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.out_dir / "comment_audit.csv"
    md_path = args.out_dir / "comment_audit_report.md"
    write_csv(rows, csv_path)
    write_markdown(rows, radon_checks, md_path, args.include_docstrings)

    total = FileStats(path="TOTAL")
    for row in rows:
        for field in asdict(row):
            if field == "path":
                continue
            setattr(total, field, getattr(total, field) + getattr(row, field))
    print(f"self-test: OK")
    print(f"files: {len(rows)}")
    print(
        f"loc={total.loc} blank={total.blank} comment_only={total.comment_only} "
        f"eol={total.eol_comments} docstring={total.docstring_nonblank} "
        f"standalone={total.standalone_string_nonblank} code={total.code}"
    )
    print(
        f"narrow={percent(total.narrow_numerator, total.loc)} "
        f"wide={percent(total.wide_numerator, total.loc)} "
        f"legacy={percent(total.legacy_numerator, total.loc)} (over loc)"
    )
    print(f"wrote {csv_path} and {md_path}")


if __name__ == "__main__":
    main()
