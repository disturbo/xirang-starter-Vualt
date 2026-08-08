#!/usr/bin/env python3
"""Xirang CodeRef — derived code/orchestration relationship index.

CodeRef is intentionally not a source of truth. It reads the repository,
extracts advisory relationships, and writes a disposable graph/cache outside
the Vault. It uses only Python's standard library and never executes scanned
project files.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import posixpath
import re
import subprocess
import sys
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


CHECK_NAME = "xirang-coderef"
SCHEMA_VERSION = "v1"
EXTRACTOR_VERSION = "v2"
DEFAULT_CONFIG_REL = Path(".standards/coderef-relations.json")
TOOL_REGISTRY_REL = Path("50-经验/Agent协作方法论/V9-工具注册表-2026-06-27.md")
HARNESS_MANIFEST_REL = Path(".standards/harness-tested-files.txt")
HARNESS_RUNNER_REL = Path("02-项目管理/脚本/v9-harness-eval-runner.py")

PYTHON_COMMAND_CALLS = {
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
    "os.system",
}
HELPER_COMMAND_CALLS = {
    "_run_tool",
    "_run_pyscript",
    "run_iteration_ops",
}
READ_METHODS = {"read_text", "read_bytes", "open"}
WRITE_METHODS = {"write_text", "write_bytes", "replace", "rename"}
DEPENDENCY_REVERSE_KINDS = {
    "imports",
    "invokes",
    "sources",
    "consumes",
    "registered_in",
    "verified_by",
}
DEPENDENCY_FORWARD_KINDS = {
    "validated_by",
    "produces",
    "consumed_by",
    "generated_by",
}
PATH_SUFFIXES = (
    ".py",
    ".sh",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".mts",
    ".cjs",
    ".cts",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".md",
    ".txt",
)
WEB_SOURCE_SUFFIXES = {
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".mts",
    ".cjs",
    ".cts",
}
WEB_RESOLVABLE_SUFFIXES = (
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mts",
    ".mjs",
    ".cts",
    ".cjs",
    ".json",
    ".css",
    ".scss",
    ".sass",
)
IGNORED_DIRS = {
    ".git",
    ".cache",
    ".next",
    ".vinext",
    ".wrangler",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "coverage",
    "dist",
    "build",
    "out",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def inventory_sha256(paths: set[str] | list[str]) -> str:
    return sha256_bytes("\0".join(sorted(paths)).encode("utf-8"))


def stable_id(prefix: str, *parts: str) -> str:
    raw = "\0".join((prefix, *parts)).encode("utf-8")
    return f"{prefix}_{sha256_bytes(raw)[:24]}"


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return default


def runtime_output_dir(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    runtime_root = os.environ.get("XIRANG_V9_RUNTIME_DIR")
    root = Path(runtime_root).expanduser() if runtime_root else Path.home() / ".xirang" / "v9-runtime"
    return (root / "code-ref").resolve()


def normalize_rel(value: str) -> str:
    value = value.replace("\\", "/").strip()
    while value.startswith("./"):
        value = value[2:]
    normalized = posixpath.normpath(value)
    return "" if normalized == "." else normalized


def is_ignored_path(rel: str) -> bool:
    return any(part in IGNORED_DIRS for part in PurePosixPath(rel).parts)


def git_inventory(repo_root: Path) -> list[str]:
    """Return tracked plus non-ignored untracked files, without ignored trees."""
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-co", "--exclude-standard", "-z"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode == 0:
        return sorted(
            rel
            for item in proc.stdout.split(b"\0")
            if item
            for rel in [normalize_rel(item.decode("utf-8", errors="surrogateescape"))]
            if not is_ignored_path(rel) and (repo_root / rel).is_file()
        )

    paths: list[str] = []
    for current, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = sorted(name for name in dirnames if name not in IGNORED_DIRS)
        base = Path(current)
        for filename in sorted(filenames):
            paths.append((base / filename).relative_to(repo_root).as_posix())
    return sorted(paths)


def is_relevant_source(rel: str) -> bool:
    if is_ignored_path(rel):
        return False
    path = PurePosixPath(rel)
    suffix = path.suffix.lower()
    if suffix in {".py", ".sh"}:
        return True
    if suffix in WEB_SOURCE_SUFFIXES:
        return True
    if path.name in {"package.json", "tsconfig.json", "jsconfig.json"}:
        return True
    if rel.endswith(".schema.json") or rel.endswith("module-registry.json"):
        return True
    if rel in {
        DEFAULT_CONFIG_REL.as_posix(),
        TOOL_REGISTRY_REL.as_posix(),
        HARNESS_MANIFEST_REL.as_posix(),
        ".standards/agent-contract.yaml",
        ".codex/hooks.json",
    }:
        return True
    return False


def classify_node(key: str) -> str:
    if key.startswith("runtime://"):
        return "data_file"
    if key.startswith("external://"):
        return "external"
    path = PurePosixPath(key)
    name = path.name.lower()
    suffix = path.suffix.lower()
    if key.endswith(".schema.json"):
        return "schema"
    if name == "module-registry.json":
        return "module_registry"
    if name == "harness-tested-files.txt":
        return "test_manifest"
    if "工具注册表" in key:
        return "tool_registry"
    if key.endswith((".yaml", ".yml", ".toml", ".json")):
        return "config"
    if key.endswith(".sh"):
        return "hook" if "/hooks/" in f"/{key}" else "shell_script"
    if key.endswith(".py"):
        if any(token in name for token in ("check", "lint", "verify", "gate", "accept")):
            return "checker"
        return "python_script"
    if suffix in WEB_SOURCE_SUFFIXES:
        if path.stem.lower() in {"page", "layout", "route", "loading", "error", "not-found"}:
            return "next_route"
        if suffix in {".tsx", ".jsx"}:
            return "component"
        return "web_module"
    return "file"


def make_node(key: str, content_hash: str | None = None, **extra: Any) -> dict[str, Any]:
    key = normalize_rel(key) if "://" not in key else key
    kind = extra.pop("kind", None) or classify_node(key)
    node = {
        "id": stable_id("node", kind, key),
        "key": key,
        "kind": kind,
        "label": extra.pop("label", None) or PurePosixPath(key).name or key,
    }
    if content_hash:
        node["sha256"] = content_hash
    node.update({k: v for k, v in extra.items() if v is not None})
    return node


def edge_spec(
    src: str,
    kind: str,
    dst: str,
    source_path: str,
    line: int = 1,
    extractor: str = "declared",
    detail: str | None = None,
    confidence: str = "high",
) -> dict[str, Any]:
    return {
        "src": src,
        "kind": kind,
        "dst": dst,
        "confidence": confidence,
        "evidence": {
            "source_path": source_path,
            "line": max(1, int(line)),
            "extractor": extractor,
            "detail": detail,
        },
    }


def callable_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        left = callable_name(node.value)
        return f"{left}.{node.attr}" if left else node.attr
    return ""


def string_values(node: ast.AST | None, constants: dict[str, list[str]]) -> list[str]:
    if node is None:
        return []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.Name):
        return list(constants.get(node.id, []))
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        values: list[str] = []
        for item in node.elts:
            values.extend(string_values(item, constants))
        return values
    if isinstance(node, ast.JoinedStr):
        literal = "".join(
            part.value for part in node.values if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
        return [literal] if literal else []
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Div, ast.Add)):
        left = string_values(node.left, constants)
        right = string_values(node.right, constants)
        if left and right:
            separator = "/" if isinstance(node.op, ast.Div) else ""
            return [f"{a.rstrip('/')}{separator}{b.lstrip('/')}" for a in left for b in right]
        if right and any(value.lower().endswith(PATH_SUFFIXES) for value in right):
            # Keep the basename resolvable against the repository inventory.
            # If it is not a repository file, resolve_target may later classify
            # a *latest.json value as a virtual runtime data node.
            return [PurePosixPath(value).name for value in right]
        return []
    if isinstance(node, ast.Call):
        name = callable_name(node.func)
        if name in {"Path", "str"} and node.args:
            return string_values(node.args[0], constants)
        if name.endswith((".resolve", ".expanduser", ".absolute")):
            return string_values(node.func.value, constants) if isinstance(node.func, ast.Attribute) else []
    if isinstance(node, ast.Attribute):
        return string_values(node.value, constants)
    return []


def collect_python_constants(tree: ast.AST) -> dict[str, list[str]]:
    constants: dict[str, list[str]] = {}
    assignments = [
        node
        for node in getattr(tree, "body", [])
        if isinstance(node, (ast.Assign, ast.AnnAssign))
    ]
    for _ in range(4):
        changed = False
        for assignment in assignments:
            value_node = assignment.value
            names: list[str] = []
            if isinstance(assignment, ast.Assign):
                names = [target.id for target in assignment.targets if isinstance(target, ast.Name)]
            elif isinstance(assignment.target, ast.Name):
                names = [assignment.target.id]
            values = string_values(value_node, constants)
            for name in names:
                if values and constants.get(name) != values:
                    constants[name] = values
                    changed = True
        if not changed:
            break
    return constants


def candidate_variants(candidate: str, source_rel: str, repo_root: Path) -> list[str]:
    text = candidate.strip().strip("'\"")
    text = text.replace("${VAULT_ROOT}", "").replace("$VAULT_ROOT", "")
    text = text.replace("${REPO_ROOT}", "").replace("$REPO_ROOT", "")
    text = text.replace("${ROOT}", "").replace("$ROOT", "")
    repo_prefix = repo_root.as_posix().rstrip("/") + "/"
    if text.startswith(repo_prefix):
        text = text[len(repo_prefix):]
    if text.startswith("runtime://") or text.startswith("external://"):
        return [text]
    text = normalize_rel(text.lstrip("/"))
    if not text:
        return []
    variants = [text]
    parent = PurePosixPath(source_rel).parent.as_posix()
    if parent not in {"", "."}:
        variants.append(normalize_rel(f"{parent}/{text}"))
    basename = PurePosixPath(text).name
    variants.extend(
        [
            f".standards/{basename}",
            f".standards/hooks/{basename}",
            f"02-项目管理/脚本/{basename}",
        ]
    )
    return list(dict.fromkeys(variants))


def resolve_target(
    candidate: str,
    source_rel: str,
    repo_root: Path,
    inventory: set[str],
    basename_index: dict[str, list[str]],
) -> str | None:
    if not candidate:
        return None
    if candidate.startswith(("runtime://", "external://")):
        return candidate
    for variant in candidate_variants(candidate, source_rel, repo_root):
        if variant in inventory:
            return variant
    basename = PurePosixPath(candidate.strip("'\"")).name
    matches = basename_index.get(basename, [])
    if len(matches) == 1:
        return matches[0]
    if basename.lower().endswith((".json", ".jsonl")) and "latest" in basename.lower():
        return f"runtime://{basename}"
    return None


def resolve_python_import(
    module: str,
    level: int,
    source_rel: str,
    inventory: set[str],
) -> str | None:
    module_path = module.replace(".", "/") if module else ""
    source_parent = PurePosixPath(source_rel).parent
    bases: list[PurePosixPath] = []
    if level:
        base = source_parent
        for _ in range(max(0, level - 1)):
            base = base.parent
        bases.append(base)
    else:
        bases.extend([source_parent, PurePosixPath("."), PurePosixPath(".standards")])
    for base in bases:
        stem = normalize_rel((base / module_path).as_posix())
        for candidate in (f"{stem}.py", f"{stem}/__init__.py"):
            if candidate in inventory:
                return candidate
    return None


def extract_python(
    path: Path,
    rel: str,
    repo_root: Path,
    inventory: set[str],
    basename_index: dict[str, list[str]],
) -> dict[str, Any]:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=rel)
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        return {"nodes": [], "edges": [], "warnings": [f"{type(exc).__name__}: {exc}"]}

    constants = collect_python_constants(tree)
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                target = resolve_python_import(alias.name, 0, rel, inventory)
                if target:
                    edges.append(edge_spec(rel, "imports", target, rel, node.lineno, "python_ast", alias.name))
        elif isinstance(node, ast.ImportFrom):
            target = resolve_python_import(node.module or "", node.level, rel, inventory)
            if target:
                edges.append(
                    edge_spec(rel, "imports", target, rel, node.lineno, "python_ast", node.module or "")
                )

        if not isinstance(node, ast.Call):
            continue
        name = callable_name(node.func)
        command_like = name in PYTHON_COMMAND_CALLS or name.split(".")[-1] in HELPER_COMMAND_CALLS
        if command_like:
            candidates: list[str] = []
            for arg in node.args:
                candidates.extend(string_values(arg, constants))
            for candidate in candidates:
                if not candidate.lower().endswith((".py", ".sh")):
                    continue
                target = resolve_target(candidate, rel, repo_root, inventory, basename_index)
                if target and target != rel:
                    edges.append(
                        edge_spec(
                            rel,
                            "invokes",
                            target,
                            rel,
                            node.lineno,
                            "python_command",
                            f"{name}: {candidate}",
                            "high",
                        )
                    )

        if isinstance(node.func, ast.Attribute) and node.func.attr in READ_METHODS | WRITE_METHODS:
            values = string_values(node.func.value, constants)
            if node.func.attr == "open" and node.args:
                values.extend(string_values(node.args[0], constants))
            for candidate in values:
                target = resolve_target(candidate, rel, repo_root, inventory, basename_index)
                if not target or target == rel:
                    continue
                nodes.append(make_node(target))
                if node.func.attr in WRITE_METHODS:
                    edges.append(edge_spec(rel, "produces", target, rel, node.lineno, "python_io"))
                else:
                    edges.append(edge_spec(rel, "consumes", target, rel, node.lineno, "python_io"))
                    if target.endswith(".schema.json"):
                        edges.append(edge_spec(target, "validated_by", rel, rel, node.lineno, "python_io"))

        if name == "open" and node.args:
            candidates = string_values(node.args[0], constants)
            mode_values = string_values(node.args[1], constants) if len(node.args) > 1 else ["r"]
            write_mode = any(any(flag in mode for flag in "wax+") for mode in mode_values)
            for candidate in candidates:
                target = resolve_target(candidate, rel, repo_root, inventory, basename_index)
                if target and target != rel:
                    nodes.append(make_node(target))
                    kind = "produces" if write_mode else "consumes"
                    edges.append(edge_spec(rel, kind, target, rel, node.lineno, "python_io"))

    return {"nodes": nodes, "edges": edges, "warnings": []}


_JS_FROM_IMPORT = re.compile(r"""\bfrom\s*["'](?P<module>[^"']+)["']""")
_JS_SIDE_EFFECT_IMPORT = re.compile(
    r"""(?m)^\s*import\s*["'](?P<module>[^"']+)["']"""
)
_JS_DYNAMIC_IMPORT = re.compile(
    r"""\b(?:require|import)\s*\(\s*["'](?P<module>[^"']+)["']\s*\)"""
)


def resolve_web_import(
    module: str,
    source_rel: str,
    inventory: set[str],
) -> str | None:
    """Resolve deterministic repository-local JS/TS imports.

    Bare package imports deliberately remain unresolved: CodeRef describes
    project relationships, not the complete third-party dependency graph.
    """
    specifier = module.strip().split("?", 1)[0].split("#", 1)[0]
    if specifier.startswith("@/"):
        stem = normalize_rel(specifier[2:])
    elif specifier.startswith("."):
        parent = PurePosixPath(source_rel).parent
        stem = normalize_rel((parent / specifier).as_posix())
    else:
        return None
    if not stem:
        return None

    candidates = [stem]
    if PurePosixPath(stem).suffix.lower() not in WEB_RESOLVABLE_SUFFIXES:
        candidates.extend(f"{stem}{suffix}" for suffix in WEB_RESOLVABLE_SUFFIXES)
        candidates.extend(f"{stem}/index{suffix}" for suffix in WEB_RESOLVABLE_SUFFIXES)
    for candidate in candidates:
        if candidate in inventory:
            return candidate
    return None


def extract_web_module(
    path: Path,
    rel: str,
    inventory: set[str],
) -> dict[str, Any]:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {"nodes": [], "edges": [], "warnings": [f"{type(exc).__name__}: {exc}"]}

    matches: set[tuple[int, str, str]] = set()
    patterns = (
        (_JS_FROM_IMPORT, "js_static_import"),
        (_JS_SIDE_EFFECT_IMPORT, "js_side_effect_import"),
        (_JS_DYNAMIC_IMPORT, "js_dynamic_import"),
    )
    for pattern, extractor in patterns:
        for match in pattern.finditer(source):
            module = match.group("module")
            line = source.count("\n", 0, match.start("module")) + 1
            matches.add((line, module, extractor))

    edges: list[dict[str, Any]] = []
    for line, module, extractor in sorted(matches):
        target = resolve_web_import(module, rel, inventory)
        if target and target != rel:
            edges.append(
                edge_spec(
                    rel,
                    "imports",
                    target,
                    rel,
                    line,
                    extractor,
                    module,
                    "medium",
                )
            )
    return {"nodes": [], "edges": edges, "warnings": []}


_SHELL_ASSIGNMENT = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.+?)\s*$")
_SHELL_PATH = re.compile(r"(?P<path>(?:[A-Za-z0-9_./${}\-\u4e00-\u9fff]+)\.(?:py|sh))")


def shell_variables(lines: list[str]) -> dict[str, str]:
    variables: dict[str, str] = {}
    for line in lines:
        match = _SHELL_ASSIGNMENT.match(line)
        if not match:
            continue
        value = match.group(2).strip().strip("'\"")
        for _ in range(3):
            for key, replacement in variables.items():
                value = value.replace(f"${key}", replacement).replace(f"${{{key}}}", replacement)
        variables[match.group(1)] = value
    return variables


def expand_shell_token(token: str, variables: dict[str, str]) -> str:
    value = token.strip().strip("'\"")
    for _ in range(4):
        previous = value
        for key, replacement in variables.items():
            value = value.replace(f"${key}", replacement).replace(f"${{{key}}}", replacement)
        if value == previous:
            break
    return value


def extract_shell(
    path: Path,
    rel: str,
    repo_root: Path,
    inventory: set[str],
    basename_index: dict[str, list[str]],
) -> dict[str, Any]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return {"nodes": [], "edges": [], "warnings": [f"{type(exc).__name__}: {exc}"]}

    variables = shell_variables(lines)
    edges: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        relation = "sources" if re.search(r"(^|[;&|]\s*)source\s+", line) else "invokes"
        if not (
            relation == "sources"
            or re.search(r"\b(?:python3|bash|sh)\b", line)
            or any(token in line for token in ("$PYTHON", "${PYTHON}", "$V8_PYTHON", "${V8_PYTHON}"))
        ):
            continue

        candidates: list[str] = []
        for match in _SHELL_PATH.finditer(line):
            candidates.append(match.group("path"))
        for variable_name in re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", line):
            value = variables.get(variable_name)
            if value and value.lower().endswith((".py", ".sh")):
                candidates.append(value)

        for candidate in candidates:
            expanded = expand_shell_token(candidate, variables)
            target = resolve_target(expanded, rel, repo_root, inventory, basename_index)
            if target and target != rel:
                edges.append(
                    edge_spec(
                        rel,
                        relation,
                        target,
                        rel,
                        line_number,
                        "shell_command",
                        raw_line.strip()[:240],
                    )
                )
    return {"nodes": [], "edges": edges, "warnings": []}


def extract_tool_registry(
    path: Path,
    rel: str,
    inventory: set[str],
) -> dict[str, Any]:
    edges: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return {"nodes": [], "edges": [], "warnings": [f"{type(exc).__name__}: {exc}"]}
    for line_number, line in enumerate(lines, start=1):
        for value in re.findall(r"`([^`]+)`", line):
            # A registry cell may list two tools as "`a.py` / `b.py`".
            # Split only a slash surrounded by whitespace; ordinary path
            # separators must remain intact.
            for candidate in re.split(r"\s+/\s+", value):
                target = normalize_rel(candidate)
                if target in inventory and target != rel:
                    edges.append(
                        edge_spec(target, "registered_in", rel, rel, line_number, "tool_registry")
                    )
    return {"nodes": [], "edges": edges, "warnings": []}


def extract_harness_manifest(
    path: Path,
    rel: str,
    inventory: set[str],
) -> dict[str, Any]:
    edges: list[dict[str, Any]] = []
    if HARNESS_RUNNER_REL.as_posix() not in inventory:
        return {"nodes": [], "edges": [], "warnings": ["harness runner missing"]}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return {"nodes": [], "edges": [], "warnings": [f"{type(exc).__name__}: {exc}"]}
    for line_number, line in enumerate(lines, start=1):
        target = line.strip()
        if not target or target.startswith("#") or target not in inventory:
            continue
        edges.append(
            edge_spec(
                target,
                "verified_by",
                HARNESS_RUNNER_REL.as_posix(),
                rel,
                line_number,
                "harness_manifest",
            )
        )
    return {"nodes": [], "edges": edges, "warnings": []}


def extract_declared_config(path: Path, rel: str) -> dict[str, Any]:
    data = read_json(path, {})
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        return {"nodes": [], "edges": [], "warnings": ["invalid declared relation config"]}
    nodes = [
        make_node(str(node["key"]), kind=node.get("kind"), label=node.get("label"))
        for node in data.get("nodes", [])
        if isinstance(node, dict) and node.get("key")
    ]
    edges = []
    for item in data.get("edges", []):
        if not isinstance(item, dict) or not all(item.get(key) for key in ("src", "kind", "dst")):
            continue
        edges.append(
            edge_spec(
                str(item["src"]),
                str(item["kind"]),
                str(item["dst"]),
                rel,
                int(item.get("line", 1)),
                "declared_config",
                item.get("note"),
                "declared",
            )
        )
    return {"nodes": nodes, "edges": edges, "warnings": []}


def extract_fragment(
    repo_root: Path,
    rel: str,
    inventory: set[str],
    basename_index: dict[str, list[str]],
    declared_config_rel: str,
) -> dict[str, Any]:
    path = repo_root / rel
    if rel == declared_config_rel:
        return extract_declared_config(path, rel)
    if rel == TOOL_REGISTRY_REL.as_posix():
        return extract_tool_registry(path, rel, inventory)
    if rel == HARNESS_MANIFEST_REL.as_posix():
        return extract_harness_manifest(path, rel, inventory)
    if rel.endswith(".py"):
        return extract_python(path, rel, repo_root, inventory, basename_index)
    if rel.endswith(".sh"):
        return extract_shell(path, rel, repo_root, inventory, basename_index)
    if PurePosixPath(rel).suffix.lower() in WEB_SOURCE_SUFFIXES:
        return extract_web_module(path, rel, inventory)
    return {"nodes": [], "edges": [], "warnings": []}


def merge_graph(
    repo_root: Path,
    source_hashes: dict[str, str],
    fragments: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    node_by_key: dict[str, dict[str, Any]] = {
        rel: make_node(rel, content_hash)
        for rel, content_hash in sorted(source_hashes.items())
    }
    raw_edges: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []
    for source_rel, fragment in sorted(fragments.items()):
        for node in fragment.get("nodes", []):
            if isinstance(node, dict) and node.get("key"):
                existing = node_by_key.get(node["key"], {})
                node_by_key[node["key"]] = {**existing, **node}
        raw_edges.extend(edge for edge in fragment.get("edges", []) if isinstance(edge, dict))
        warnings.extend(
            {"source_path": source_rel, "message": str(message)}
            for message in fragment.get("warnings", [])
        )

    for edge in raw_edges:
        for key in ("src", "dst"):
            node_key = edge.get(key)
            if isinstance(node_key, str) and node_key not in node_by_key:
                is_virtual = node_key.startswith(("runtime://", "external://"))
                exists = is_virtual or (repo_root / node_key).is_file()
                node_by_key[node_key] = make_node(node_key, missing=not exists)

    merged_edges: dict[tuple[str, str, str], dict[str, Any]] = {}
    for edge in raw_edges:
        src_key, kind, dst_key = edge.get("src"), edge.get("kind"), edge.get("dst")
        if not all(isinstance(value, str) and value for value in (src_key, kind, dst_key)):
            continue
        triple = (src_key, kind, dst_key)
        evidence = edge.get("evidence", {})
        if triple not in merged_edges:
            src_id = node_by_key[src_key]["id"]
            dst_id = node_by_key[dst_key]["id"]
            merged_edges[triple] = {
                "id": stable_id("edge", src_id, kind, dst_id),
                "src": src_id,
                "src_key": src_key,
                "kind": kind,
                "dst": dst_id,
                "dst_key": dst_key,
                "confidence": edge.get("confidence", "high"),
                "evidence": [],
            }
        if evidence and evidence not in merged_edges[triple]["evidence"]:
            merged_edges[triple]["evidence"].append(evidence)
        if edge.get("confidence") == "declared":
            merged_edges[triple]["confidence"] = "declared"

    nodes = sorted(node_by_key.values(), key=lambda item: (item["kind"], item["key"]))
    edges = sorted(
        merged_edges.values(),
        key=lambda item: (item["src_key"], item["kind"], item["dst_key"]),
    )
    return {
        "check": CHECK_NAME,
        "schema_version": SCHEMA_VERSION,
        "generated_at": now_iso(),
        "repo_root": str(repo_root),
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "sources": len(source_hashes),
            "nodes": len(nodes),
            "edges": len(edges),
            "nodes_by_kind": dict(sorted(Counter(node["kind"] for node in nodes).items())),
            "edges_by_kind": dict(sorted(Counter(edge["kind"] for edge in edges).items())),
            "warnings": len(warnings),
        },
        "warnings": warnings,
    }


def build_index(repo_root: Path, output_dir: Path, config_path: Path) -> dict[str, Any]:
    # macOS temporary directories may be reached through /var and
    # /private/var aliases. Canonicalize before enforcing config scope.
    repo_root = repo_root.resolve()
    output_dir = output_dir.resolve()
    config_path = config_path.resolve()
    inventory_list = git_inventory(repo_root)
    inventory = set(inventory_list)
    try:
        config_rel = config_path.resolve().relative_to(repo_root).as_posix()
    except ValueError as exc:
        raise ValueError("declared relation config must be inside repo_root") from exc
    if config_path.is_file():
        inventory.add(config_rel)
    sources = sorted(rel for rel in inventory if is_relevant_source(rel) or rel == config_rel)
    resolvable_inventory = {
        rel
        for rel in inventory
        if is_relevant_source(rel)
        or PurePosixPath(rel).suffix.lower() in WEB_RESOLVABLE_SUFFIXES
    }
    current_inventory_sha256 = inventory_sha256(resolvable_inventory)
    basename_index: dict[str, list[str]] = {}
    for rel in inventory:
        basename_index.setdefault(PurePosixPath(rel).name, []).append(rel)

    old_cache = read_json(output_dir / "cache.json", {})
    old_files = old_cache.get("files", {}) if isinstance(old_cache, dict) else {}
    cache_compatible = (
        isinstance(old_cache, dict)
        and old_cache.get("extractor_version") == EXTRACTOR_VERSION
        and old_cache.get("inventory_sha256") == current_inventory_sha256
    )
    new_files: dict[str, dict[str, Any]] = {}
    source_hashes: dict[str, str] = {}
    changed: list[str] = []
    reused: list[str] = []
    warnings: list[dict[str, str]] = []

    for rel in sources:
        path = repo_root / rel
        try:
            content_hash = sha256_file(path)
        except OSError as exc:
            warnings.append({"source_path": rel, "message": f"{type(exc).__name__}: {exc}"})
            continue
        source_hashes[rel] = content_hash
        cached = old_files.get(rel) if cache_compatible and isinstance(old_files, dict) else None
        if isinstance(cached, dict) and cached.get("sha256") == content_hash:
            fragment = cached.get("fragment", {"nodes": [], "edges": [], "warnings": []})
            reused.append(rel)
        else:
            fragment = extract_fragment(
                repo_root,
                rel,
                inventory,
                basename_index,
                config_rel,
            )
            changed.append(rel)
        new_files[rel] = {"sha256": content_hash, "fragment": fragment}

    deleted = sorted(set(old_files) - set(new_files)) if isinstance(old_files, dict) else []
    graph = merge_graph(
        repo_root,
        source_hashes,
        {rel: item["fragment"] for rel, item in new_files.items()},
    )
    graph["warnings"].extend(warnings)
    graph["stats"]["warnings"] = len(graph["warnings"])
    cache = {
        "check": CHECK_NAME,
        "schema_version": SCHEMA_VERSION,
        "extractor_version": EXTRACTOR_VERSION,
        "generated_at": graph["generated_at"],
        "repo_root": str(repo_root),
        "inventory_sha256": current_inventory_sha256,
        "files": new_files,
    }
    status = {
        "check": CHECK_NAME,
        "schema_version": SCHEMA_VERSION,
        "generated_at": graph["generated_at"],
        "status": "green" if not graph["warnings"] else "yellow",
        "repo_root": str(repo_root),
        "output_dir": str(output_dir),
        "changed_sources": changed,
        "reused_sources": reused,
        "deleted_sources": deleted,
        "summary": {
            **graph["stats"],
            "changed_sources": len(changed),
            "reused_sources": len(reused),
            "deleted_sources": len(deleted),
        },
        "paths": {
            "graph": str(output_dir / "graph.json"),
            "cache": str(output_dir / "cache.json"),
            "status": str(output_dir / "status.json"),
        },
    }
    atomic_write_json(output_dir / "graph.json", graph)
    atomic_write_json(output_dir / "cache.json", cache)
    atomic_write_json(output_dir / "status.json", status)
    return status


def load_graph(output_dir: Path) -> dict[str, Any] | None:
    graph = read_json(output_dir / "graph.json")
    return graph if isinstance(graph, dict) else None


def validate_graph(graph: dict[str, Any] | None) -> dict[str, Any]:
    reasons: list[dict[str, Any]] = []
    if not graph:
        return {
            "check": f"{CHECK_NAME}-validate",
            "valid": False,
            "reasons": [{"rule_id": "CODEREF_GRAPH_MISSING", "message": "graph.json 不存在或无效"}],
        }
    if graph.get("schema_version") != SCHEMA_VERSION:
        reasons.append({"rule_id": "CODEREF_SCHEMA", "message": "schema_version 不受支持"})
    nodes = graph.get("nodes")
    edges = graph.get("edges")
    if not isinstance(nodes, list):
        nodes = []
        reasons.append({"rule_id": "CODEREF_NODES", "message": "nodes 必须是数组"})
    if not isinstance(edges, list):
        edges = []
        reasons.append({"rule_id": "CODEREF_EDGES", "message": "edges 必须是数组"})

    node_ids = [node.get("id") for node in nodes if isinstance(node, dict)]
    if len(node_ids) != len(set(node_ids)):
        reasons.append({"rule_id": "CODEREF_NODE_DUPLICATE", "message": "节点 ID 重复"})
    node_id_set = {node_id for node_id in node_ids if isinstance(node_id, str)}
    dangling = [
        edge.get("id")
        for edge in edges
        if isinstance(edge, dict)
        and (edge.get("src") not in node_id_set or edge.get("dst") not in node_id_set)
    ]
    if dangling:
        reasons.append(
            {"rule_id": "CODEREF_DANGLING_EDGE", "message": "存在悬空边", "edge_ids": dangling}
        )
    invalid_node_ids = [
        node.get("key")
        for node in nodes
        if isinstance(node, dict)
        and node.get("id") != stable_id("node", str(node.get("kind")), str(node.get("key")))
    ]
    if invalid_node_ids:
        reasons.append(
            {
                "rule_id": "CODEREF_NODE_ID_UNSTABLE",
                "message": "节点 ID 与稳定 ID 规则不一致",
                "keys": invalid_node_ids,
            }
        )
    return {
        "check": f"{CHECK_NAME}-validate",
        "validated_at": now_iso(),
        "valid": not reasons,
        "nodes": len(nodes),
        "edges": len(edges),
        "reasons": reasons,
    }


def graph_status(repo_root: Path, output_dir: Path) -> dict[str, Any]:
    cache = read_json(output_dir / "cache.json", {})
    persisted_status = read_json(output_dir / "status.json", {})
    files = cache.get("files", {}) if isinstance(cache, dict) else {}
    if not files:
        return {
            "check": f"{CHECK_NAME}-status",
            "status": "missing",
            "fresh": False,
            "message": "尚未构建 CodeRef 缓存",
            "output_dir": str(output_dir),
        }
    stale: list[str] = []
    missing: list[str] = []
    for rel, item in files.items():
        path = repo_root / rel
        if not path.is_file():
            missing.append(rel)
        elif sha256_file(path) != item.get("sha256"):
            stale.append(rel)
    current_inventory = set(git_inventory(repo_root))
    current_sources = {rel for rel in current_inventory if is_relevant_source(rel)}
    resolvable_inventory = {
        rel
        for rel in current_inventory
        if is_relevant_source(rel)
        or PurePosixPath(rel).suffix.lower() in WEB_RESOLVABLE_SUFFIXES
    }
    cache_compatible = (
        isinstance(cache, dict)
        and cache.get("extractor_version") == EXTRACTOR_VERSION
        and cache.get("inventory_sha256") == inventory_sha256(resolvable_inventory)
    )
    uncached = sorted(current_sources - set(files))
    fresh = cache_compatible and not stale and not missing and not uncached
    warning_count = (
        int(persisted_status.get("summary", {}).get("warnings", 0))
        if isinstance(persisted_status, dict)
        and isinstance(persisted_status.get("summary"), dict)
        else 0
    )
    return {
        "check": f"{CHECK_NAME}-status",
        "status": "green" if fresh and warning_count == 0 else "yellow",
        "fresh": fresh,
        "repo_root": str(repo_root),
        "output_dir": str(output_dir),
        "cached_sources": len(files),
        "cache_compatible": cache_compatible,
        "extractor_version": EXTRACTOR_VERSION,
        "warnings": warning_count,
        "stale_sources": stale,
        "missing_sources": missing,
        "uncached_sources": uncached,
    }


def match_nodes(graph: dict[str, Any], pattern: str) -> list[dict[str, Any]]:
    nodes = [node for node in graph.get("nodes", []) if isinstance(node, dict)]
    exact = [node for node in nodes if node.get("key") == pattern or node.get("id") == pattern]
    if exact:
        return exact
    lowered = pattern.lower()
    return [
        node
        for node in nodes
        if lowered in str(node.get("key", "")).lower() or lowered in str(node.get("label", "")).lower()
    ]


def query_graph(
    graph: dict[str, Any],
    pattern: str,
    depth: int,
    direction: str,
) -> dict[str, Any]:
    matched = match_nodes(graph, pattern)
    if not matched:
        return {"check": f"{CHECK_NAME}-query", "found": False, "pattern": pattern, "nodes": [], "edges": []}
    node_by_id = {node["id"]: node for node in graph.get("nodes", [])}
    edges = [edge for edge in graph.get("edges", []) if isinstance(edge, dict)]
    visited = {node["id"] for node in matched}
    frontier = deque((node["id"], 0) for node in matched)
    selected_edges: dict[str, dict[str, Any]] = {}
    while frontier:
        node_id, current_depth = frontier.popleft()
        if current_depth >= depth:
            continue
        for edge in edges:
            neighbor: str | None = None
            if direction in {"out", "both"} and edge.get("src") == node_id:
                neighbor = edge.get("dst")
            elif direction in {"in", "both"} and edge.get("dst") == node_id:
                neighbor = edge.get("src")
            if not isinstance(neighbor, str):
                continue
            selected_edges[edge["id"]] = edge
            if neighbor not in visited:
                visited.add(neighbor)
                frontier.append((neighbor, current_depth + 1))
    return {
        "check": f"{CHECK_NAME}-query",
        "found": True,
        "pattern": pattern,
        "depth": depth,
        "direction": direction,
        "nodes": sorted((node_by_id[node_id] for node_id in visited), key=lambda item: item["key"]),
        "edges": sorted(selected_edges.values(), key=lambda item: (item["src_key"], item["kind"], item["dst_key"])),
    }


def impact_graph(graph: dict[str, Any], pattern: str, depth: int) -> dict[str, Any]:
    matched = match_nodes(graph, pattern)
    if not matched:
        return {"check": f"{CHECK_NAME}-impact", "found": False, "pattern": pattern, "impacted": [], "edges": []}
    node_by_id = {node["id"]: node for node in graph.get("nodes", [])}
    edges = [edge for edge in graph.get("edges", []) if isinstance(edge, dict)]
    start_ids = {node["id"] for node in matched}
    visited = set(start_ids)
    frontier = deque((node_id, 0) for node_id in start_ids)
    selected_edges: dict[str, dict[str, Any]] = {}
    levels: dict[str, int] = {node_id: 0 for node_id in start_ids}
    while frontier:
        node_id, current_depth = frontier.popleft()
        if current_depth >= depth:
            continue
        for edge in edges:
            neighbor: str | None = None
            kind = edge.get("kind")
            if kind in DEPENDENCY_REVERSE_KINDS and edge.get("dst") == node_id:
                neighbor = edge.get("src")
            elif kind in DEPENDENCY_FORWARD_KINDS and edge.get("src") == node_id:
                neighbor = edge.get("dst")
            if not isinstance(neighbor, str):
                continue
            selected_edges[edge["id"]] = edge
            if neighbor not in visited:
                visited.add(neighbor)
                levels[neighbor] = current_depth + 1
                frontier.append((neighbor, current_depth + 1))
    impacted = [
        {**node_by_id[node_id], "impact_depth": levels[node_id]}
        for node_id in visited - start_ids
    ]
    impacted.sort(key=lambda item: (item["impact_depth"], item["key"]))
    return {
        "check": f"{CHECK_NAME}-impact",
        "found": True,
        "pattern": pattern,
        "depth": depth,
        "roots": matched,
        "impacted": impacted,
        "edges": sorted(selected_edges.values(), key=lambda item: (item["src_key"], item["kind"], item["dst_key"])),
    }


def print_result(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return
    if "valid" in result:
        print("PASS" if result["valid"] else "FAIL")
        for reason in result.get("reasons", []):
            print(f"[{reason.get('rule_id')}] {reason.get('message')}")
        return
    if "summary" in result:
        summary = result["summary"]
        print(
            f"{result.get('status', 'unknown')}: "
            f"{summary.get('nodes', 0)} nodes / {summary.get('edges', 0)} edges / "
            f"{summary.get('changed_sources', 0)} changed"
        )
        return
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build and query Xirang's disposable code/orchestration relationship graph."
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo-root", default=".", help="Repository root (default: current directory)")
    common.add_argument("--output-dir", help="Derived cache directory")
    common.add_argument("--config", help="Declared relationship config")
    common.add_argument("--json", action="store_true", help="Emit structured JSON")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("build", parents=[common], help="Incrementally build graph/cache/status")
    subparsers.add_parser("status", parents=[common], help="Check cache freshness without writing")
    subparsers.add_parser("validate", parents=[common], help="Validate persisted graph")
    query = subparsers.add_parser("query", parents=[common], help="Query graph neighbors")
    query.add_argument("--path", required=True, help="Node key, id, label, or substring")
    query.add_argument("--depth", type=int, default=1, choices=range(1, 6))
    query.add_argument("--direction", choices=("in", "out", "both"), default="both")
    impact = subparsers.add_parser("impact", parents=[common], help="Find reverse dependents/impact radius")
    impact.add_argument("--path", required=True, help="Changed node key, id, label, or substring")
    impact.add_argument("--depth", type=int, default=2, choices=range(1, 6))
    return parser


def main() -> int:
    parser = make_parser()
    args = parser.parse_args()
    repo_root = Path(args.repo_root).expanduser().resolve()
    output_dir = runtime_output_dir(args.output_dir)
    config_path = (
        Path(args.config).expanduser().resolve()
        if args.config
        else repo_root / DEFAULT_CONFIG_REL
    )
    if not repo_root.is_dir():
        parser.error(f"repo root does not exist: {repo_root}")

    if args.command == "build":
        try:
            result = build_index(repo_root, output_dir, config_path)
        except ValueError as exc:
            print_result({"error": "invalid_config", "message": str(exc)}, args.json)
            return 2
        print_result(result, args.json)
        return 0
    if args.command == "status":
        result = graph_status(repo_root, output_dir)
        print_result(result, args.json)
        return 0 if result.get("fresh") else 1

    graph = load_graph(output_dir)
    if args.command == "validate":
        result = validate_graph(graph)
        print_result(result, args.json)
        return 0 if result["valid"] else 1
    if graph is None:
        print_result({"error": "graph_missing", "output_dir": str(output_dir)}, args.json)
        return 1
    if args.command == "query":
        result = query_graph(graph, args.path, args.depth, args.direction)
        print_result(result, args.json)
        return 0 if result.get("found") else 1
    if args.command == "impact":
        result = impact_graph(graph, args.path, args.depth)
        print_result(result, args.json)
        return 0 if result.get("found") else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
