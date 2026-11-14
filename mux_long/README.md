# mux_long — 归档帧 → 测点长表

用离线字段表把 ASC 归档帧展开成测点长表，纯 Python + python-can，无其他依赖。

## 文件

- `field_table.csv` — 离线字段表：总线、帧 ID、复用选择值（`mux` 空 = 常驻信号）、
  起始位、位长、端序、有符号、倍率、偏移、单位。`is_selector=yes` 标记选择器信号。
- `make_archive.py` — 生成示例归档 `frames.asc`。
- `decoder.py` — 解码器（库 + CLI）。
- `test_decoder.py` — pytest 用例。

## 位编号约定（两种端序都按 DBC）

- 小端（Intel）：起始位是信号 LSB，位号 = 字节号 × 8 + 字节内位号（位 0 = 字节 0 的 LSB）。
- 大端（Motorola）：起始位是信号 MSB 的 DBC 位号，内部换算成 MSB 优先的序贯位号
  `seq = 8*(start//8) + (7 - start%8)` 再取 `length` 位。

## 复用规则

- 先解选择器；选择器关键字节缺失（如 DLC 截断）时，所有复用分支保持未知：
  长表中输出空 raw/physical，状态 `selector_missing`。
- `mux` 值与选择器不匹配的信号不解码、不出行——同一位段上另一分支的有符号值不会漏进来。

## 用法

```sh
python make_archive.py
python decoder.py decode --table field_table.csv --archive frames.asc --out long_table.csv
python decoder.py trace --table field_table.csv --bus 1 --frame 0x100 \
    --signal SignedA --data "01 E7 0F 00 00 00 00 00"
python -m pytest test_decoder.py -o addopts=""
```

`trace` 把一个跨字节边界的负读数追回到原始位段（字节范围、位段二进制、补码解释），
并把物理值按倍率/偏移换算回原始位段做闭环校验。

## 长表列

`timestamp, bus, frame_id, frame_name, signal, mux, raw, physical, unit, status`

`status`：`ok` / `missing_bytes`（信号自身字节缺失）/ `selector_missing`（选择器缺失，分支未知）。
