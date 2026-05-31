#!/usr/bin/env python3
"""
xirang-lint.py — 息壤 V8 六脉合规主控脚本
v1.0 · 2026-05-17 | 息壤 V8.5.0

统一调度所有合规检查脚本，输出汇总报告。

用法：
  python3 .standards/xirang-lint.py [--path 10-项目/] [--json] [--fix]
  python3 .standards/xirang-lint.py --all            # 全 vault 扫描
  python3 .standards/xirang-lint.py --suite brand    # 单项扫描
  python3 .standards/xirang-lint.py --suite fm       # frontmatter
  python3 .standards/xirang-lint.py --suite path     # 路径规范
  python3 .standards/xirang-lint.py --suite pre      # pre-write-check（单文件）

六脉检查项：
  1. brand    — 品牌色值/字体/命名合规 (brand-lint.py)
  2. fm       — Frontmatter 深度校验 (frontmatter-lint.py)
  3. path     — 路径结构/命名/孤儿 (path-lint.py)
  4. pre      — 产物合规 pre-write (pre-write-check.py, 单文件模式)
  5. link     — Wiki-link 有效性 (TODO: link-lint.py)
  6. size     — 文件体积/行数限制 (集成在 path-lint 中)

退出码：
  0 = 全部通过（或仅 info/warning）
  1 = 存在 error 级违规
"""

import sys
import os
import json
import subprocess
import time
from pathlib import Path

# === 子脚本路径 ===
STANDARDS_DIR = os.path.dirname(os.path.abspath(__file__))

SUITES = {
    "brand": {
        "script": os.path.join(STANDARDS_DIR, "brand-lint.py"),
        "name": "品牌合规",
        "desc": "色值/字体/废弃品牌名",
        "supports_path": True,
    },
    "fm": {
        "script": os.path.join(STANDARDS_DIR, "frontmatter-lint.py"),
        "name": "Frontmatter 校验",
        "desc": "必填字段/枚举值/交叉引用",
        "supports_path": True,
    },
    "path": {
        "script": os.path.join(STANDARDS_DIR, "path-lint.py"),
        "name": "路径规范",
        "desc": "命名/深度/孤儿/空目录",
        "supports_path": True,
    },
    "pre": {
        "script": os.path.join(STANDARDS_DIR, "pre-write-check.py"),
        "name": "产物合规",
        "desc": "emoji/frontmatter/品牌/路径白名单",
        "supports_path": False,  # 单文件模式
    },
}


def run_suite(suite_name: str, target_path: str = ".", output_json: bool = True) -> dict:
    """运行单个检查套件"""
    suite = SUITES[suite_name]
    script = suite["script"]

    if not os.path.isfile(script):
        return {
            "suite": suite_name,
            "name": suite["name"],
            "status": "skip",
            "reason": f"脚本不存在: {script}",
            "violations": [],
            "summary": {"errors": 0, "warnings": 0, "info": 0}
        }

    # 构建命令
    cmd = [sys.executable, script, "--json"]
    if suite["supports_path"]:
        if target_path != ".":
            cmd.extend(["--path", target_path])
        else:
            cmd.append("--all")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=os.getcwd()
        )
        # 解析 JSON 输出
        if result.stdout.strip():
            data = json.loads(result.stdout)
            return {
                "suite": suite_name,
                "name": suite["name"],
                "status": data.get("status", "unknown"),
                "violations": data.get("violations", []),
                "summary": data.get("summary", {"errors": 0, "warnings": 0, "info": 0})
            }
        else:
            return {
                "suite": suite_name,
                "name": suite["name"],
                "status": "pass",
                "violations": [],
                "summary": {"errors": 0, "warnings": 0, "info": 0}
            }
    except subprocess.TimeoutExpired:
        return {
            "suite": suite_name,
            "name": suite["name"],
            "status": "error",
            "reason": "执行超时（120s）",
            "violations": [],
            "summary": {"errors": 1, "warnings": 0, "info": 0}
        }
    except json.JSONDecodeError as e:
        return {
            "suite": suite_name,
            "name": suite["name"],
            "status": "error",
            "reason": f"输出解析失败: {e}",
            "violations": [],
            "summary": {"errors": 1, "warnings": 0, "info": 0}
        }
    except Exception as e:
        return {
            "suite": suite_name,
            "name": suite["name"],
            "status": "error",
            "reason": str(e),
            "violations": [],
            "summary": {"errors": 1, "warnings": 0, "info": 0}
        }


def main():
    target = "."
    output_json = "--json" in sys.argv
    scan_all = "--all" in sys.argv
    selected_suite = None

    for i, arg in enumerate(sys.argv):
        if arg == "--path" and i + 1 < len(sys.argv):
            target = sys.argv[i + 1]
        if arg == "--suite" and i + 1 < len(sys.argv):
            selected_suite = sys.argv[i + 1]

    if scan_all:
        target = "."

    # 确定运行哪些套件
    if selected_suite:
        if selected_suite not in SUITES:
            print(f"未知套件: {selected_suite}", file=sys.stderr)
            print(f"可用: {', '.join(SUITES.keys())}", file=sys.stderr)
            sys.exit(2)
        suites_to_run = [selected_suite]
    else:
        # 跳过 pre（单文件模式，不适合批量扫描）
        suites_to_run = [k for k, v in SUITES.items() if v["supports_path"]]

    # 执行
    start_time = time.time()
    results = []

    for suite_name in suites_to_run:
        result = run_suite(suite_name, target)
        results.append(result)

    elapsed = time.time() - start_time

    # 汇总
    total_errors = sum(r["summary"].get("errors", 0) for r in results)
    total_warnings = sum(r["summary"].get("warnings", 0) for r in results)
    total_info = sum(r["summary"].get("info", 0) for r in results)
    overall_status = "fail" if total_errors > 0 else "pass"

    if output_json:
        report = {
            "status": overall_status,
            "elapsed_sec": round(elapsed, 2),
            "target": target,
            "summary": {
                "suites_run": len(results),
                "errors": total_errors,
                "warnings": total_warnings,
                "info": total_info,
            },
            "suites": results
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        # 人类可读格式
        print("=" * 60)
        print(f"  息壤 V8 六脉合规检查报告")
        print(f"  扫描路径: {target}")
        print(f"  耗时: {elapsed:.1f}s")
        print("=" * 60)
        print()

        for r in results:
            icon = {
                "pass": "[PASS]",
                "fail": "[FAIL]",
                "error": "[ERR!]",
                "skip": "[SKIP]"
            }.get(r["status"], "[????]")

            s = r["summary"]
            detail = f"E:{s.get('errors', 0)} W:{s.get('warnings', 0)} I:{s.get('info', 0)}"
            print(f"  {icon} {r['name']:<16} {detail}")

            if r.get("reason"):
                print(f"       原因: {r['reason']}")

        print()
        print("-" * 60)
        print(f"  总计: Errors={total_errors} Warnings={total_warnings} Info={total_info}")
        print(f"  状态: {'PASS' if overall_status == 'pass' else 'FAIL'}")
        print("-" * 60)

        # 输出 top 违规（最多 20 条 error）
        if total_errors > 0:
            print()
            print("  Top Errors:")
            count = 0
            for r in results:
                for v in r.get("violations", []):
                    if v.get("severity") == "error" and count < 20:
                        print(f"    [{r['suite']}] {v.get('file', '?')} — {v.get('message', '?')}")
                        count += 1
            if total_errors > 20:
                print(f"    ... 还有 {total_errors - 20} 条 error")

        sys.exit(1 if total_errors > 0 else 0)


if __name__ == "__main__":
    main()
