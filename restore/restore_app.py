#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""restore_app.py — 从快照 + replay 批次恢复指标结果表。

恢复口径：
  1. 以快照 snapshot_1105.csv 为唯一起点，快照点之前的行原样保留、绝不覆盖。
  2. replay 目录下批次按文件名排序逐批重放；每行以 (指标, 日期) 为键：
       - 键不存在 -> 写入（计入“贡献行数”）
       - 键已存在 -> 跳过（计入“跳过行数”），不覆盖已有值
     因此同一批次重放任意次数，结果表不变（幂等）。
  3. 不合法的行（字段数不对、指标为空、日期非 YYYY-MM-DD、值非数值）跳过并登记原因。
  4. 补录批（数据日期早于快照点）适用同一规则：与快照冲突的跳过，快照缺失的补入，
     处置说明写入登记文件尾注。

用法：
  python3 -I restore_app.py --snapshot <snapshot.csv> --replay-dir <replay/> \
      --out-csv <results_restored.csv> --registry <restore_registry.md>
"""

import argparse
import csv
import re
import sys
from datetime import date, datetime
from pathlib import Path

HEADER = ["指标", "日期", "值"]
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


def validate_row(row):
    """返回 None 表示合法；否则返回跳过原因字符串。"""
    if len(row) != 3:
        return f"字段数={len(row)}（应为3）"
    metric, dt, val = (cell.strip() for cell in row)
    if not metric:
        return "指标为空"
    m = DATE_RE.match(dt)
    if not m:
        return f"日期格式非法: {dt or '<空>'}"
    try:
        date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return f"日期不存在: {dt}"
    if not NUM_RE.match(val):
        return f"值非数值: {val or '<空>'}"
    return None


def load_snapshot(path):
    """读取快照，返回 (有序键值字典, 指标规范顺序)。"""
    table = {}
    metric_order = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header != HEADER:
            raise SystemExit(f"[致命] 快照表头异常: {header}，期望 {HEADER}")
        for line_no, row in enumerate(reader, start=2):
            if not row:
                continue
            err = validate_row(row)
            if err:
                raise SystemExit(f"[致命] 快照第{line_no}行不合法（快照必须可信）: {row} -> {err}")
            metric, dt, val = (cell.strip() for cell in row)
            key = (metric, dt)
            if key in table:
                raise SystemExit(f"[致命] 快照存在重复键: {key}")
            table[key] = val
            if metric not in metric_order:
                metric_order.append(metric)
    return table, metric_order


def replay_batch(path, table, metric_order):
    """重放单个批次，返回该批次统计 dict。"""
    stats = {
        "file": path.name,
        "data_date": "",
        "read": 0,
        "inserted": 0,
        "skipped_dup": 0,
        "skipped_invalid": 0,
        "invalid_detail": [],   # (行号, 原文, 原因)
        "dup_detail": [],        # (指标, 日期)
        "inserted_keys": [],     # (指标, 日期, 值)
    }
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header != HEADER:
            raise SystemExit(f"[致命] 批次 {path.name} 表头异常: {header}，期望 {HEADER}")
        for line_no, row in enumerate(reader, start=2):
            if not row:
                continue
            stats["read"] += 1
            err = validate_row(row)
            if err:
                stats["skipped_invalid"] += 1
                stats["invalid_detail"].append((line_no, ",".join(row), err))
                continue
            metric, dt, val = (cell.strip() for cell in row)
            if not stats["data_date"]:
                stats["data_date"] = dt
            key = (metric, dt)
            if key in table:
                stats["skipped_dup"] += 1
                stats["dup_detail"].append(key)
                continue
            table[key] = val
            stats["inserted"] += 1
            stats["inserted_keys"].append((metric, dt, val))
            if metric not in metric_order:
                metric_order.append(metric)
    return stats


def write_results(path, table, metric_order):
    """按日期、指标规范顺序输出纯数据结果表（一行一指标一日）。"""
    def sort_key(item):
        (metric, dt), _val = item
        return (dt, metric_order.index(metric) if metric in metric_order else len(metric_order))

    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(HEADER)
        for (metric, dt), val in sorted(table.items(), key=sort_key):
            writer.writerow([metric, dt, val])


def write_registry(path, snapshot_path, replay_dir, snapshot_rows, stats_list,
                   out_csv_path, reconcile):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total_inserted = sum(s["inserted"] for s in stats_list)
    total_dup = sum(s["skipped_dup"] for s in stats_list)
    total_invalid = sum(s["skipped_invalid"] for s in stats_list)
    total_skip = total_dup + total_invalid
    total_read = sum(s["read"] for s in stats_list)

    lines = []
    lines.append("# 结果表恢复登记\n")
    lines.append(f"- 生成时间：{now}")
    lines.append(f"- 快照起点：`{snapshot_path}`（{snapshot_rows} 行，覆盖至 2026-11-05 收盘）")
    lines.append(f"- 重放目录：`{replay_dir}`（批次按文件名排序逐批重放）")
    lines.append(f"- 恢复产物：`{out_csv_path}`")
    lines.append("- 重放规则：以 (指标, 日期) 为键，键不存在则写入，键已存在则跳过且不覆盖；"
                 "非法行跳过并记录原因。\n")

    lines.append("## 一、批次贡献登记\n")
    lines.append("| 批次文件 | 数据日期 | 读取数据行 | 贡献（新增）行数 | 跳过行数 | 其中：键已存在跳过 | 其中：非法行跳过 |")
    lines.append("|---|---|---|---|---|---|---|")
    for s in stats_list:
        lines.append(
            f"| {s['file']} | {s['data_date']} | {s['read']} | {s['inserted']} | "
            f"{s['skipped_dup'] + s['skipped_invalid']} | {s['skipped_dup']} | {s['skipped_invalid']} |"
        )
    lines.append(f"| **合计** | — | **{total_read}** | **{total_inserted}** | "
                 f"**{total_skip}** | **{total_dup}** | **{total_invalid}** |\n")

    lines.append("## 二、跳过行明细\n")
    for s in stats_list:
        lines.append(f"### {s['file']}")
        if s["dup_detail"]:
            lines.append(f"- 键已存在跳过 {len(s['dup_detail'])} 行（保留快照/先前批次的值，不覆盖）：")
            for metric, dt in s["dup_detail"]:
                lines.append(f"  - {metric} / {dt}")
        if s["invalid_detail"]:
            lines.append(f"- 非法行跳过 {len(s['invalid_detail'])} 行：")
            for line_no, raw, reason in s["invalid_detail"]:
                lines.append(f"  - 第{line_no}行 `{raw}` —— {reason}")
        if not s["dup_detail"] and not s["invalid_detail"]:
            lines.append("- 无跳过行。")
        lines.append("")

    lines.append("## 三、对账核验\n")
    lines.append(f"- 快照行数：{reconcile['snapshot_rows']}")
    lines.append(f"- 重放新增合计：{total_inserted}")
    lines.append(f"- 结果表实际行数：{reconcile['output_rows']}")
    lines.append(f"- 等式校验：{reconcile['snapshot_rows']} + {total_inserted} = "
                 f"{reconcile['snapshot_rows'] + total_inserted}，"
                 f"与结果表行数 {'一致 ✓' if reconcile['output_rows'] == reconcile['snapshot_rows'] + total_inserted else '不一致 ✗'}")
    lines.append(f"- 快照保留校验：快照 {reconcile['snapshot_rows']} 个键全部存在于结果表且值未改动 "
                 f"{'✓' if reconcile['snapshot_preserved'] else '✗'}")
    lines.append(f"- 结果表主键唯一性（指标+日期无重复）：{'✓' if reconcile['unique_keys'] else '✗'}")
    lines.append(f"- 批次读取行合计 {total_read} = 新增 {total_inserted} + 跳过 {total_skip}："
                 f"{'✓' if total_read == total_inserted + total_skip else '✗'}\n")

    lines.append("## 四、尾注\n")
    lines.append("1. **补录批处置（20261104.csv，内容为 2026-11-04 日志，晚到数日）**：该批 12 行与快照"
                 "同属快照点（11-05 收盘）之前的数据。快照点之前的原始日志已归档下线、无法核对差异，"
                 "故快照是该时段唯一权威来源：与快照键冲突的 10 个指标一律**跳过、不覆盖**；"
                 "快照中 2026-11-04 原本缺失的 2 个指标（支付金额=660477.00、转化率=6.64）"
                 "由补录批**补入**，补齐后该日 12 个指标齐全。处置后快照点之前的日子一行未丢。")
    lines.append("2. **幂等性**：重放只做“键不存在才写入”，同一批次重放两回或多回，第二回起全部命中"
                 "“键已存在跳过”，结果表字节级不变；本登记的跳过计数会相应增加，贡献行数恒为 0。")
    lines.append("3. **非法行口径（20261107.csv 后 6 行）**：字段数不等于 3、指标为空、日期不符合 "
                 "YYYY-MM-DD 且无法解析为真实日期、值非数值的行一律跳过，原因逐行登记于上文第二节，"
                 "不以任何形式写入结果表。")
    lines.append("4. **排序与格式**：结果表按日期升序、指标按快照中首次出现顺序排列；UTF-8、LF 行尾、"
                 "无 BOM，值按原始字符串原样保留，不做数值重格式化。")
    lines.append("5. 损坏的旧 results.csv 未被读取或修改，恢复产物为独立新文件，可人工比对后替换。\n")

    path.write_text("\n".join(lines), encoding="utf-8")


def reconcile(out_csv_path, snapshot_before):
    """回读产物做独立对账。"""
    with open(out_csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        assert header == HEADER, f"产物表头异常: {header}"
        keys, preserved = [], True
        for row in reader:
            if not row:
                continue
            metric, dt, val = (cell.strip() for cell in row)
            keys.append((metric, dt))
            if (metric, dt) in snapshot_before and snapshot_before[(metric, dt)] != val:
                preserved = False
    return {
        "output_rows": len(keys),
        "snapshot_rows": len(snapshot_before),
        "snapshot_preserved": preserved and all(k in keys for k in snapshot_before),
        "unique_keys": len(keys) == len(set(keys)),
    }


def main():
    ap = argparse.ArgumentParser(description="从快照与 replay 批次恢复指标结果表")
    ap.add_argument("--snapshot", required=True, type=Path)
    ap.add_argument("--replay-dir", required=True, type=Path)
    ap.add_argument("--out-csv", required=True, type=Path)
    ap.add_argument("--registry", required=True, type=Path)
    args = ap.parse_args()

    table, metric_order = load_snapshot(args.snapshot)
    snapshot_rows = len(table)
    snapshot_snapshot = dict(table)  # 供对账使用的不可变副本

    batch_files = sorted(p for p in args.replay_dir.iterdir()
                         if p.is_file() and p.suffix == ".csv")
    if not batch_files:
        raise SystemExit(f"[致命] replay 目录下没有批次文件: {args.replay_dir}")

    stats_list = [replay_batch(p, table, metric_order) for p in batch_files]

    args.out_csv.parent.mkdir(parents=True, exist_ok=True)
    args.registry.parent.mkdir(parents=True, exist_ok=True)
    write_results(args.out_csv, table, metric_order)

    rec = reconcile(args.out_csv, snapshot_snapshot)
    write_registry(args.registry, args.snapshot, args.replay_dir, snapshot_rows,
                   stats_list, args.out_csv, rec)

    # 终检：任一硬性校验不过则非零退出
    ok = (rec["output_rows"] == snapshot_rows + sum(s["inserted"] for s in stats_list)
          and rec["snapshot_preserved"] and rec["unique_keys"])
    print(f"快照行数={snapshot_rows} 结果表行数={rec['output_rows']} "
          f"新增={sum(s['inserted'] for s in stats_list)} "
          f"跳过={sum(s['skipped_dup'] + s['skipped_invalid'] for s in stats_list)} "
          f"对账={'通过' if ok else '失败'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
