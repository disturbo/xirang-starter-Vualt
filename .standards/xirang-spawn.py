#!/usr/bin/env python3
"""
xirang-spawn.py — Xi Rang V8.5 Subagent Spawn Prompt Generator
v1.1.0 · 2026-05-24 (V8.5 upgrade) | 息壤 V8.5.0

将 Subagent 的约束注入封装为一条命令，自动拼装完整 spawn prompt。
解决 retrospective P3"spawn + 约束注入手工拼装容易遗漏"的问题。

V8.5 新增：
  --emit-record      同时创建子任务运行时记录（默认开启）
  --no-emit-record   禁用记录创建
  --task-id ID       父任务 ID（emit-record 必填）
  --sub-id ID        子任务 ID（默认自动生成 sub-01, sub-02...）
  --tool-blacklist   工具黑名单（逗号分隔）

用法：
  python3 .standards/xirang-spawn.py --task "原型迭代" --module 08-工单管理 --type prototype --task-id T-20260524-01
  python3 .standards/xirang-spawn.py --task "代码实现" --module 06-商务补偿 --type code --budget M --task-id T-20260524-01
  python3 .standards/xirang-spawn.py --task "资料采集" --type research --inject brand,emoji,markdown --no-emit-record
  python3 .standards/xirang-spawn.py --list-types    # 列出所有任务类型
  python3 .standards/xirang-spawn.py --list-modules  # 列出所有模块

输出：标准化 spawn prompt（Markdown 格式），可直接复制到父 Agent 的 spawn 调用中。

设计原则：
  - 不遗漏：所有约束项自动注入，无需人工记忆
  - 可定制：--inject 参数选择额外约束维度
  - 可审计：输出 prompt 含来源标记 + sub_id，方便回溯
  - 成本感知：自动填入 budget_level 和 timeout
  - 运行时记录：自动创建 subtask record JSON（V8.5）
"""

import sys
import os
import json
import datetime
import subprocess
from pathlib import Path

VAULT_ROOT = Path(os.environ.get("VAULT_ROOT", "$VAULT_ROOT"))

# === 任务类型 × 配置矩阵（对齐 V8 S6.1 超时量化规范） ===
TASK_TYPES = {
    "prototype": {
        "name": "原型产出",
        "timeout_sec": 600,
        "budget_level": "M",
        "typical_tokens": 30000,
        "inject_default": ["brand", "emoji", "markdown", "frontmatter", "path"],
        "write_scope": "10-项目/{项目名}/{module}/prototype/",
        "description": "HTML 原型页面、交互逻辑、样式文件",
    },
    "code": {
        "name": "代码实现",
        "timeout_sec": 900,
        "budget_level": "L",
        "typical_tokens": 50000,
        "inject_default": ["emoji", "frontmatter", "path"],
        "write_scope": "10-项目/{项目名}/{module}/",
        "description": "业务逻辑代码、脚本、配置文件",
    },
    "prd": {
        "name": "PRD 产出",
        "timeout_sec": 900,
        "budget_level": "L",
        "typical_tokens": 45000,
        "inject_default": ["emoji", "markdown", "frontmatter", "path"],
        "write_scope": "10-项目/{项目名}/{module}/",
        "description": "PRD 文档、需求规格、功能说明",
    },
    "research": {
        "name": "资料采集",
        "timeout_sec": 300,
        "budget_level": "S",
        "typical_tokens": 15000,
        "inject_default": ["emoji", "frontmatter"],
        "write_scope": "20-资料/ 或 _temp/",
        "description": "竞品分析、资料摘要、技术调研",
    },
    "spec": {
        "name": "规范编写",
        "timeout_sec": 600,
        "budget_level": "M",
        "typical_tokens": 25000,
        "inject_default": ["emoji", "markdown", "frontmatter", "path"],
        "write_scope": "50-经验/ 或父 Agent 指定",
        "description": "方法论、规范、指南文档",
    },
    "review": {
        "name": "审核检核",
        "timeout_sec": 300,
        "budget_level": "S",
        "typical_tokens": 10000,
        "inject_default": ["emoji"],
        "write_scope": "不写文件（只返回摘要）",
        "description": "代码审核、品牌审计、质量检核",
    },
    "batch": {
        "name": "批量操作",
        "timeout_sec": 1800,
        "budget_level": "XL",
        "typical_tokens": 80000,
        "inject_default": ["emoji", "path"],
        "write_scope": "父 Agent 指定的多目录",
        "description": "文件批量修改、迁移、格式统一",
    },
    "diagram": {
        "name": "图表生成",
        "timeout_sec": 600,
        "budget_level": "M",
        "typical_tokens": 20000,
        "inject_default": ["brand", "path"],
        "write_scope": "10-项目/{项目名}/{module}/",
        "description": "drawio 流程图、SVG 架构图",
    },
}

# === 约束维度定义（每个维度对应约束包中的一段） ===
CONSTRAINT_BLOCKS = {
    "brand": """### Brand Compliance
- Primary: #861B2F (Xi Jing Red), Accent: #2D9C4F (Green)
- Font: PingFang SC / Microsoft YaHei
- Spacing: 8px base grid
- Buttons: primary fill, border-radius 6px
- Allowed colors: see `.standards/brand-lint.py` BRAND_COLORS
- Deprecated names: DO NOT use "QiJing M7" / "qi jing"
""",
    "emoji": """### Emoji Ban [IRON RULE]
- NO emoji in output files (HTML/Markdown/YAML/CSS/SVG)
- Even if reference files contain emoji, output MUST remove them
- Replace with lucide icons or plain text
- Remove leading spaces when removing emoji (avoid "< h1> Title</h1>")
- Exception: agent role emoji in methodology docs only
""",
    "markdown": """### Markdown Output Standard
- Follow `30-规范/Markdown文档输出规范.md`
- Tables: use semicolons for multi-items, NO <br> tags
- Indentation: plain text symbols only
- No HTML tags in Markdown (except when producing .html files)
""",
    "frontmatter": """### Frontmatter Required [IRON RULE]
Every output .md file MUST contain YAML frontmatter with:
- title (string)
- version (X.Y format)
- status (one of: cao-gao/zheng-shi/fei-qi/WIP/gui-dang)
- maturity (one of: cao-gao/zheng-shi/shi-xing/gui-dang)
- type (document type)
- created (YYYY-MM-DD)
- tags (YAML array)
""",
    "path": """### Path Rules
- ONLY write to scope specified above (write_scope)
- NEVER write to: 00-MOC/, 30-规范/, 40-jue-ce/
- No files < 1KB (no stub files)
- Source material NEVER goes into Published documents directly
""",
    "flowchart": """### Flowchart Standard (v3.0)
- Follow `30-规范/流程图绘制规范.md` v3.0
- Step 0-6 generation process
- Brand colors only (see brand-lint.py)
- White background layer (id="bg") required
- Main flow: strokeWidth=2, strokeColor=#333333
""",
}

# === 核心：约束包头部（所有 Subagent 必须注入） ===
CONSTRAINT_HEADER = """## [Constraints] (MUST NOT violate)

### Core Rules (V8 S3.3)
1. Nesting depth = 1: You CANNOT spawn sub-sub-agents
2. CANNOT write 00-MOC/ or 30-规范/ (only parent Agent has permission)
3. CANNOT make decisions (decisions must return to parent)
4. Return value <= 500 chars structured summary
5. Your write paths MUST NOT overlap with other concurrent subagents

### Violation Priority
```
P0 Path permissions > P1 Output format (emoji/frontmatter) > P2 Brand > P3 Content
```
"""

# === 返回格式模板 ===
RETURN_TEMPLATE = """### Return Format
When done, return ONLY a structured summary (<=500 chars):
```
- Task: {one sentence}
- Status: done / partial / failed
- Output path: {file paths}
- Key findings: {3-5 items}
- Questions: {mark any unresolved}
```
"""

# === 可用模块列表（从 vault 结构推断） ===
KNOWN_MODULES = [
    "01-PDI管理", "02-保养管理", "03-保险管理", "04-续保管理",
    "05-延保管理", "06-商务补偿", "07-预约管理", "08-工单管理",
    "09-配件管理", "10-结算管理", "11-报表中心", "12-系统管理",
    "20-取送车", "21-代步车服务", "30-延保销售", "31-服务工单管理",
    "32-服务助手手机端", "33-代步车服务",
]


def generate_spawn_prompt(
    task: str,
    task_type: str,
    module: str = None,
    inject: list = None,
    budget: str = None,
    timeout: int = None,
    parent_agent: str = "claudian",
    extra_context: str = None,
    sub_id: str = None,
    task_id: str = None,
    tool_blacklist: list = None,
) -> str:
    """生成完整的 spawn prompt"""

    type_config = TASK_TYPES.get(task_type)
    if not type_config:
        raise ValueError(f"Unknown task type: {task_type}. Available: {list(TASK_TYPES.keys())}")

    # 确定注入维度
    if inject is None:
        inject = type_config["inject_default"]

    # 确定 budget 和 timeout
    final_budget = budget or type_config["budget_level"]
    final_timeout = timeout or type_config["timeout_sec"]

    # 确定写入范围
    write_scope = type_config["write_scope"]
    if module and "{module}" in write_scope:
        write_scope = write_scope.replace("{module}", module)

    # 生成时间戳
    ts = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S+08:00")

    # === 拼装 prompt ===
    lines = []

    # 元信息头
    lines.append(f"# Subagent Task: {task}")
    lines.append("")
    if sub_id:
        lines.append(f"- Sub ID: {sub_id}")
    if task_id:
        lines.append(f"- Parent Task: {task_id}")
    lines.append(f"- Generated: {ts}")
    lines.append(f"- Parent: {parent_agent}")
    lines.append(f"- Type: {task_type} ({type_config['name']})")
    lines.append(f"- Budget: {final_budget}")
    lines.append(f"- Timeout: {final_timeout}s")
    lines.append(f"- Write scope: `{write_scope}`")
    if tool_blacklist:
        lines.append(f"- Tool blacklist: {', '.join(tool_blacklist)}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # 任务描述
    lines.append("## Task")
    lines.append("")
    lines.append(f"{task}")
    lines.append("")
    if extra_context:
        lines.append(f"Context: {extra_context}")
        lines.append("")

    # 约束包
    lines.append("---")
    lines.append("")
    lines.append(CONSTRAINT_HEADER)

    # 写入范围（具体化）
    lines.append(f"### Write Scope")
    lines.append(f"You may ONLY write to: `{write_scope}`")
    lines.append(f"All other paths are OFF-LIMITS.")
    lines.append("")

    # 注入各约束维度
    for dim in inject:
        if dim in CONSTRAINT_BLOCKS:
            lines.append(CONSTRAINT_BLOCKS[dim])

    # 返回格式
    lines.append(RETURN_TEMPLATE)

    # 成本约束
    lines.append("### Cost Awareness")
    lines.append(f"- Budget level: {final_budget}")
    lines.append(f"- Typical tokens for this task type: ~{type_config['typical_tokens']:,}")
    lines.append(f"- If approaching budget limit, return partial results rather than exceeding")
    lines.append("")

    # 心跳提醒（长任务）
    if final_timeout > 120:
        lines.append("### Heartbeat")
        lines.append(f"- This task timeout: {final_timeout}s")
        lines.append(f"- If running > 120s, emit progress update (heartbeat)")
        lines.append(f"- Heartbeat interval: {min(final_timeout // 3, 120)}s")
        lines.append("")

    # 工具黑名单约束（V8.5）
    if tool_blacklist:
        lines.append("### Tool Blacklist [ENFORCED]")
        lines.append("You MUST NOT use the following tools/commands:")
        for t in tool_blacklist:
            lines.append(f"- {t}")
        lines.append("")

    # 尾部标记
    lines.append("---")
    sub_tag = f" | sub_id={sub_id}" if sub_id else ""
    lines.append(f"*Xi Rang V8.5.0L | spawn by {parent_agent}{sub_tag} | {ts}*")

    return "\n".join(lines)


def auto_sub_id(task_id: str) -> str:
    """自动生成下一个可用的 sub_id"""
    subtasks_dir = VAULT_ROOT / "_temp" / task_id / "subtasks"
    if not subtasks_dir.exists():
        return "sub-01"
    existing = list(subtasks_dir.glob("sub-*.json"))
    return f"sub-{len(existing) + 1:02d}"


def emit_subtask_record(
    task_id: str, sub_id: str, parent: str, model: str,
    task_type: str, name: str, write_scope: str, timeout: int,
    tool_blacklist: list = None,
) -> dict:
    """调用 subtask-record.py create 创建运行时记录"""
    cmd = [
        sys.executable, str(VAULT_ROOT / ".standards" / "subtask-record.py"), "create",
        "--task-id", task_id,
        "--sub-id", sub_id,
        "--parent", parent,
        "--model", model or "sonnet",
        "--type", task_type,
        "--name", name,
        "--write-scope", write_scope,
        "--timeout", str(timeout),
    ]
    if tool_blacklist:
        cmd.extend(["--tool-blacklist", ",".join(tool_blacklist)])

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(VAULT_ROOT))
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"status": "error", "message": result.stderr or result.stdout}


def main():
    # 帮助信息
    if "--help" in sys.argv or "-h" in sys.argv or len(sys.argv) == 1:
        print("""xirang-spawn.py — Subagent Spawn Prompt Generator (V8.5.0L)

Usage:
  python3 .standards/xirang-spawn.py --task "TASK" --type TYPE [options]

Required:
  --task TEXT       Task description for the subagent
  --type TYPE       Task type (see --list-types)

Options:
  --module NAME     Target module (e.g., 08-工单管理)
  --inject DIMS     Comma-separated constraint dimensions (default: auto by type)
  --budget LEVEL    Override budget level (S/M/L/XL)
  --timeout SEC     Override timeout in seconds
  --parent AGENT    Parent agent ID (default: claudian)
  --context TEXT    Extra context for the subagent
  --output FILE     Write prompt to file instead of stdout
  --json            Output as JSON (for programmatic use)
  --list-types      List all task types with configs
  --list-modules    List known modules
  --list-inject     List available constraint dimensions

V8.5 Options:
  --task-id ID      Parent task ID (required for --emit-record)
  --sub-id ID       Sub task ID (auto-generated if omitted)
  --model MODEL     Model for spawn (sonnet/opus/haiku, default: sonnet)
  --emit-record     Create subtask runtime record (default: ON)
  --no-emit-record  Skip record creation
  --tool-blacklist  Comma-separated tool blacklist
  --budget-check    Enable pre-spawn budget check (default: ON)
  --no-budget-check Disable pre-spawn budget check

Examples:
  python3 .standards/xirang-spawn.py --task "重绘取送车流程图" --type diagram --module 20-取送车 --task-id T-20260524-01
  python3 .standards/xirang-spawn.py --task "PDI PRD 章节产出" --type prd --module 01-PDI管理 --budget L --task-id T-20260524-01
  python3 .standards/xirang-spawn.py --task "竞品分析报告" --type research --no-emit-record
""")
        return

    # 列表命令
    if "--list-types" in sys.argv:
        print("Available task types:")
        print(f"{'Type':<12} {'Name':<10} {'Timeout':<10} {'Budget':<8} {'Default Inject'}")
        print("-" * 70)
        for k, v in TASK_TYPES.items():
            inject_str = ",".join(v["inject_default"])
            print(f"{k:<12} {v['name']:<10} {v['timeout_sec']:<10} {v['budget_level']:<8} {inject_str}")
        return

    if "--list-modules" in sys.argv:
        print("Known modules (10-项目/{项目名}/):")
        for m in KNOWN_MODULES:
            print(f"  {m}")
        return

    if "--list-inject" in sys.argv:
        print("Available constraint dimensions:")
        for k in CONSTRAINT_BLOCKS:
            print(f"  {k:<12} — {CONSTRAINT_BLOCKS[k].split(chr(10))[0].strip('#').strip()}")
        return

    # 解析参数
    task = None
    task_type = None
    module = None
    inject = None
    budget = None
    timeout = None
    parent = "claudian"
    context = None
    output_file = None
    output_json = "--json" in sys.argv
    # V8.5 新增
    task_id = None
    sub_id = None
    model = "sonnet"  # V8.5: spawn 使用的模型（用于预算检查和记录）
    emit_record = True  # V8.5 默认开启
    tool_blacklist = None
    budget_check = True  # V8.5 Phase 3: 默认开启预算检查

    i = 1
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg == "--task" and i + 1 < len(sys.argv):
            task = sys.argv[i + 1]; i += 2
        elif arg == "--type" and i + 1 < len(sys.argv):
            task_type = sys.argv[i + 1]; i += 2
        elif arg == "--module" and i + 1 < len(sys.argv):
            module = sys.argv[i + 1]; i += 2
        elif arg == "--inject" and i + 1 < len(sys.argv):
            inject = sys.argv[i + 1].split(","); i += 2
        elif arg == "--budget" and i + 1 < len(sys.argv):
            budget = sys.argv[i + 1]; i += 2
        elif arg == "--timeout" and i + 1 < len(sys.argv):
            timeout = int(sys.argv[i + 1]); i += 2
        elif arg == "--parent" and i + 1 < len(sys.argv):
            parent = sys.argv[i + 1]; i += 2
        elif arg == "--context" and i + 1 < len(sys.argv):
            context = sys.argv[i + 1]; i += 2
        elif arg == "--output" and i + 1 < len(sys.argv):
            output_file = sys.argv[i + 1]; i += 2
        elif arg == "--task-id" and i + 1 < len(sys.argv):
            task_id = sys.argv[i + 1]; i += 2
        elif arg == "--sub-id" and i + 1 < len(sys.argv):
            sub_id = sys.argv[i + 1]; i += 2
        elif arg == "--model" and i + 1 < len(sys.argv):
            model = sys.argv[i + 1]; i += 2
        elif arg == "--emit-record":
            emit_record = True; i += 1
        elif arg == "--no-emit-record":
            emit_record = False; i += 1
        elif arg == "--tool-blacklist" and i + 1 < len(sys.argv):
            tool_blacklist = sys.argv[i + 1].split(","); i += 2
        elif arg == "--budget-check":
            budget_check = True; i += 1
        elif arg == "--no-budget-check":
            budget_check = False; i += 1
        elif arg == "--json":
            i += 1
        else:
            i += 1

    # 验证必填
    if not task:
        print("Error: --task is required", file=sys.stderr)
        sys.exit(2)
    if not task_type:
        print("Error: --type is required", file=sys.stderr)
        sys.exit(2)
    if task_type not in TASK_TYPES:
        print(f"Error: unknown type '{task_type}'. Use --list-types.", file=sys.stderr)
        sys.exit(2)

    # V8.5: model 合法性校验
    valid_models = {"opus", "sonnet", "haiku", "deepseek"}
    if model not in valid_models:
        print(f"Error: unknown model '{model}'. Valid: {sorted(valid_models)}", file=sys.stderr)
        sys.exit(2)

    # V8.5: 自动生成 sub_id（需要 task_id）
    if emit_record and not task_id:
        # 如果没给 task_id，降级为不创建记录
        emit_record = False
        if output_json:
            pass  # 静默降级
        else:
            print("[WARN] --task-id not provided, skipping record creation (use --no-emit-record to suppress)", file=sys.stderr)

    if emit_record and not sub_id:
        sub_id = auto_sub_id(task_id)

    # 默认工具黑名单
    if tool_blacklist is None:
        tool_blacklist = ["v8_handshake", "v8_end", "xirang-spawn.py"]

    # V8.5 Phase 3: Pre-spawn budget check
    if budget_check and task_id:
        try:
            budget_result = subprocess.run(
                [sys.executable, str(VAULT_ROOT / ".standards" / "spawn-budget-check.py"),
                 "check", "--task-id", task_id, "--type", task_type,
                 "--model", model, "--json"],
                capture_output=True, text=True, timeout=10, cwd=str(VAULT_ROOT)
            )
            if budget_result.returncode == 2:
                # 红灯：超预算
                try:
                    budget_data = json.loads(budget_result.stdout)
                except json.JSONDecodeError:
                    budget_data = {"reason": "budget check failed"}
                if output_json:
                    print(json.dumps({"status": "budget_exceeded", "detail": budget_data}, ensure_ascii=False, indent=2))
                else:
                    print(f"[ABORT] 预算超出: {budget_data.get('reason', 'unknown')}", file=sys.stderr)
                sys.exit(2)
            elif budget_result.returncode == 1:
                # 黄灯：建议降级，输出 warning 但继续
                try:
                    budget_data = json.loads(budget_result.stdout)
                    recommended = budget_data.get("model_recommended", "")
                except json.JSONDecodeError:
                    recommended = ""
                if not output_json:
                    print(f"[WARN] 预算紧张，建议模型: {recommended}", file=sys.stderr)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass  # budget check 失败不阻断 spawn

    # 生成 prompt
    type_config = TASK_TYPES[task_type]
    final_timeout = timeout or type_config["timeout_sec"]

    try:
        prompt = generate_spawn_prompt(
            task=task,
            task_type=task_type,
            module=module,
            inject=inject,
            budget=budget,
            timeout=timeout,
            parent_agent=parent,
            extra_context=context,
            sub_id=sub_id,
            task_id=task_id,
            tool_blacklist=tool_blacklist,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)

    # V8.5: 创建子任务运行时记录
    record_result = None
    if emit_record:
        write_scope_str = type_config["write_scope"]
        if module and "{module}" in write_scope_str:
            write_scope_str = write_scope_str.replace("{module}", module)
        record_result = emit_subtask_record(
            task_id=task_id,
            sub_id=sub_id,
            parent=parent,
            model=model,
            task_type=task_type,
            name=task,
            write_scope=write_scope_str,
            timeout=final_timeout,
            tool_blacklist=tool_blacklist,
        )

    # 输出
    if output_json:
        result = {
            "task": task,
            "type": task_type,
            "module": module,
            "budget": budget or type_config["budget_level"],
            "timeout_sec": final_timeout,
            "inject": inject or type_config["inject_default"],
            "parent": parent,
            "sub_id": sub_id,
            "task_id": task_id,
            "prompt": prompt,
            "prompt_length": len(prompt),
            "estimated_tokens": len(prompt) // 4,
            "record_created": record_result if record_result else None,
        }
        output = json.dumps(result, ensure_ascii=False, indent=2)
    else:
        output = prompt

    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"[OK] Spawn prompt written to: {output_file}", file=sys.stderr)
        print(f"     Length: {len(prompt)} chars (~{len(prompt)//4} tokens)", file=sys.stderr)
        if record_result and record_result.get("status") == "ok":
            print(f"     Record: {record_result['path']}", file=sys.stderr)
    else:
        print(output)
        if record_result and record_result.get("status") == "ok" and not output_json:
            print(f"\n[V8.5] Record created: {record_result['path']}", file=sys.stderr)


if __name__ == "__main__":
    main()
