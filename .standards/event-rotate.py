#!/usr/bin/env python3
"""
event-rotate.py -- V8.5 事件流轮转工具
v1.0.0 | 2026-05-25 | 息壤 V8.5.0

解决 智能体事件.jsonl 和 agent-cost-events.jsonl 无限增长问题。
按日期/大小自动归档旧事件，保留最近 N 天活跃数据。

用法:
  python3 .standards/event-rotate.py rotate [--days 30] [--max-size 500] [--dry-run] [--json]
  python3 .standards/event-rotate.py status [--json]
  python3 .standards/event-rotate.py restore --archive <archive_file> [--json]

子命令:
  rotate   执行轮转（超过 days 天或 max-size KB 的事件归档）
  status   查看当前事件流状态（行数、大小、最早/最新时间戳）
  restore  从归档恢复事件到主流（追加模式）

退出码:
  0 = 成功（或 dry-run 报告）
  1 = 执行失败
  2 = 参数错误
"""
from __future__ import annotations

import sys
import os
import json
import argparse
import shutil
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional

VAULT_ROOT = Path(os.environ.get("VAULT_ROOT", os.getcwd()))
TZ = timezone(timedelta(hours=8))

# 需要轮转的事件流文件
EVENT_FILES = [
    VAULT_ROOT / "02-项目管理" / "智能体状态" / "智能体事件.jsonl",
    VAULT_ROOT / "02-项目管理" / "agent-cost-events.jsonl",
]

# 归档目录
ARCHIVE_DIR = VAULT_ROOT / "02-项目管理" / "事件归档"

# 默认配置
DEFAULT_RETAIN_DAYS = 30
DEFAULT_MAX_SIZE_KB = 500


def now_iso() -> str:
    return datetime.now(TZ).replace(microsecond=0).isoformat()


def parse_ts(ts_str: str) -> Optional[datetime]:
    """尝试解析事件时间戳，始终返回 aware datetime（+08:00）"""
    dt = None
    for fmt in [
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S+08:00",
        "%Y-%m-%dT%H:%M:%S",
    ]:
        try:
            dt = datetime.strptime(ts_str, fmt)
            break
        except (ValueError, TypeError):
            continue
    if dt is None:
        try:
            dt = datetime.fromisoformat(ts_str)
        except (ValueError, TypeError):
            return None
    # 确保 timezone-aware
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt


def get_event_ts(line: str) -> Optional[datetime]:
    """从 JSONL 行提取时间戳"""
    try:
        obj = json.loads(line.strip())
        ts_str = obj.get("ts", "")
        return parse_ts(ts_str)
    except (json.JSONDecodeError, AttributeError):
        return None


def file_stats(filepath: Path) -> dict:
    """获取文件统计信息"""
    if not filepath.exists():
        return {"exists": False, "path": str(filepath.relative_to(VAULT_ROOT))}

    size_bytes = filepath.stat().st_size
    lines = 0
    earliest = None
    latest = None

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                lines += 1
                ts = get_event_ts(line)
                if ts:
                    if earliest is None or ts < earliest:
                        earliest = ts
                    if latest is None or ts > latest:
                        latest = ts
    except IOError:
        pass

    return {
        "exists": True,
        "path": str(filepath.relative_to(VAULT_ROOT)),
        "size_kb": round(size_bytes / 1024, 1),
        "lines": lines,
        "earliest": earliest.isoformat() if earliest else None,
        "latest": latest.isoformat() if latest else None,
    }


def rotate_file(filepath: Path, retain_days: int, max_size_kb: int,
                dry_run: bool = False) -> dict:
    """轮转单个事件流文件"""
    if not filepath.exists():
        return {"file": str(filepath.name), "action": "skip", "reason": "不存在"}

    size_kb = filepath.stat().st_size / 1024
    cutoff = datetime.now(TZ) - timedelta(days=retain_days)

    # 读取所有行，分为保留和归档
    retain_lines = []
    archive_lines = []

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                ts = get_event_ts(stripped)
                if ts and ts < cutoff:
                    archive_lines.append(stripped)
                else:
                    # 无法解析时间戳的行保留（安全起见）
                    retain_lines.append(stripped)
    except IOError as e:
        return {"file": str(filepath.name), "action": "error", "reason": str(e)}

    # 判断是否需要轮转
    needs_rotate = len(archive_lines) > 0 or size_kb > max_size_kb

    if not needs_rotate:
        return {
            "file": str(filepath.name),
            "action": "skip",
            "reason": f"无需轮转（{len(retain_lines)} 行, {size_kb:.1f}KB, 全部在 {retain_days} 天内）"
        }

    # 如果超大小但没有过期行，强制归档最旧的一半
    if not archive_lines and size_kb > max_size_kb:
        midpoint = len(retain_lines) // 2
        archive_lines = retain_lines[:midpoint]
        retain_lines = retain_lines[midpoint:]

    if dry_run:
        return {
            "file": str(filepath.name),
            "action": "dry-run",
            "archive_count": len(archive_lines),
            "retain_count": len(retain_lines),
            "archive_size_kb": round(sum(len(l.encode()) for l in archive_lines) / 1024, 1),
        }

    # 执行轮转
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(TZ).strftime("%Y%m%d")
    stem = filepath.stem
    archive_path = ARCHIVE_DIR / f"{stem}-{today}.jsonl"

    # 如果归档文件已存在，追加
    mode = "a" if archive_path.exists() else "w"
    try:
        with open(archive_path, mode, encoding="utf-8") as f:
            for line in archive_lines:
                f.write(line + "\n")

        # 重写主文件（只保留活跃行）
        with open(filepath, "w", encoding="utf-8") as f:
            for line in retain_lines:
                f.write(line + "\n")

        return {
            "file": str(filepath.name),
            "action": "rotated",
            "archived": len(archive_lines),
            "retained": len(retain_lines),
            "archive_path": str(archive_path.relative_to(VAULT_ROOT)),
        }
    except IOError as e:
        return {"file": str(filepath.name), "action": "error", "reason": str(e)}


# === 子命令 ===

def cmd_status(args) -> int:
    """查看事件流状态"""
    results = []
    for filepath in EVENT_FILES:
        results.append(file_stats(filepath))

    # 检查归档
    archives = []
    if ARCHIVE_DIR.exists():
        for f in sorted(ARCHIVE_DIR.glob("*.jsonl")):
            archives.append({
                "file": f.name,
                "size_kb": round(f.stat().st_size / 1024, 1),
            })

    output = {
        "ts": now_iso(),
        "streams": results,
        "archives": archives,
        "archive_dir": str(ARCHIVE_DIR.relative_to(VAULT_ROOT)),
    }

    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print("=== 事件流状态 ===")
        for s in results:
            if s["exists"]:
                print(f"  {s['path']}: {s['lines']} 行, {s['size_kb']}KB")
                print(f"    时间范围: {s['earliest']} ~ {s['latest']}")
            else:
                print(f"  {s['path']}: 不存在")
        if archives:
            print(f"\n=== 归档 ({ARCHIVE_DIR.relative_to(VAULT_ROOT)}) ===")
            for a in archives:
                print(f"  {a['file']}: {a['size_kb']}KB")
        else:
            print("\n  无归档文件")

    return 0


def cmd_rotate(args) -> int:
    """执行轮转"""
    results = []
    for filepath in EVENT_FILES:
        result = rotate_file(
            filepath,
            retain_days=args.days,
            max_size_kb=args.max_size,
            dry_run=args.dry_run,
        )
        results.append(result)

    output = {
        "ts": now_iso(),
        "config": {"retain_days": args.days, "max_size_kb": args.max_size},
        "dry_run": args.dry_run,
        "results": results,
    }

    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        mode = "[DRY-RUN] " if args.dry_run else ""
        print(f"{mode}=== 事件流轮转 (保留 {args.days} 天 / 上限 {args.max_size}KB) ===")
        for r in results:
            action = r["action"]
            if action == "rotated":
                print(f"  {r['file']}: 归档 {r['archived']} 行 -> {r['archive_path']}")
                print(f"    保留 {r['retained']} 行")
            elif action == "dry-run":
                print(f"  {r['file']}: 将归档 {r['archive_count']} 行, 保留 {r['retain_count']} 行")
            elif action == "skip":
                print(f"  {r['file']}: 跳过 ({r['reason']})")
            elif action == "error":
                print(f"  {r['file']}: 错误 ({r['reason']})")

    has_error = any(r["action"] == "error" for r in results)
    return 1 if has_error else 0


def cmd_restore(args) -> int:
    """从归档恢复"""
    archive_path = Path(args.archive)
    if not archive_path.is_absolute():
        archive_path = VAULT_ROOT / archive_path

    if not archive_path.exists():
        print(f"[ERROR] 归档文件不存在: {archive_path}", file=sys.stderr)
        return 1

    # 判断恢复到哪个主文件
    stem = archive_path.stem.rsplit("-", 1)[0]  # 去掉日期后缀
    target = None
    for ef in EVENT_FILES:
        if ef.stem == stem:
            target = ef
            break

    if not target:
        print(f"[ERROR] 无法匹配目标文件。归档 stem='{stem}'", file=sys.stderr)
        print(f"  已知文件: {[ef.stem for ef in EVENT_FILES]}", file=sys.stderr)
        return 1

    # 读取归档行数
    try:
        with open(archive_path, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
    except IOError as e:
        print(f"[ERROR] 读取归档失败: {e}", file=sys.stderr)
        return 1

    # 追加到主文件
    try:
        with open(target, "a", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")
    except IOError as e:
        print(f"[ERROR] 追加到主文件失败: {e}", file=sys.stderr)
        return 1

    output = {
        "ts": now_iso(),
        "action": "restored",
        "archive": str(archive_path.relative_to(VAULT_ROOT)),
        "target": str(target.relative_to(VAULT_ROOT)),
        "lines_restored": len(lines),
    }

    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(f"[restore] 已恢复 {len(lines)} 行: {archive_path.name} -> {target.name}")

    return 0


def main():
    parser = argparse.ArgumentParser(
        description="V8.5 事件流轮转工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # rotate
    p_rotate = subparsers.add_parser("rotate", help="执行事件流轮转")
    p_rotate.add_argument("--days", type=int, default=DEFAULT_RETAIN_DAYS,
                          help=f"保留最近 N 天（默认 {DEFAULT_RETAIN_DAYS}）")
    p_rotate.add_argument("--max-size", type=int, default=DEFAULT_MAX_SIZE_KB,
                          help=f"单文件大小上限 KB（默认 {DEFAULT_MAX_SIZE_KB}）")
    p_rotate.add_argument("--dry-run", action="store_true",
                          help="仅预览，不实际执行")
    p_rotate.add_argument("--json", action="store_true")

    # status
    p_status = subparsers.add_parser("status", help="查看事件流状态")
    p_status.add_argument("--json", action="store_true")

    # restore
    p_restore = subparsers.add_parser("restore", help="从归档恢复事件")
    p_restore.add_argument("--archive", required=True,
                           help="归档文件路径（相对 vault root 或绝对路径）")
    p_restore.add_argument("--json", action="store_true")

    args = parser.parse_args()

    handlers = {
        "rotate": cmd_rotate,
        "status": cmd_status,
        "restore": cmd_restore,
    }

    sys.exit(handlers[args.command](args))


if __name__ == "__main__":
    main()
