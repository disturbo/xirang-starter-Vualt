#!/usr/bin/env python3
"""Multi-project workspace monitor for Xirang CodeRef.

The workspace layer never executes scanned code and never changes project
content. It builds one isolated CodeRef cache per declared project, then
creates a namespaced aggregate graph for advisory queries.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any


SCRIPT_PATH = Path(__file__).with_name("xirang-coderef.py")
SPEC = importlib.util.spec_from_file_location("xirang_coderef_core", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load CodeRef core: {SCRIPT_PATH}")
coderef = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(coderef)

CHECK_NAME = "xirang-coderef-workspace"
SCHEMA_VERSION = "v1"
PROJECT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def load_manifest(path: Path) -> dict[str, Any]:
    manifest_path = path.expanduser().resolve()
    data = read_json(manifest_path)
    if not isinstance(data, dict):
        raise ValueError(f"workspace manifest is missing or invalid: {manifest_path}")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("workspace manifest schema_version must be v1")
    workspace_id = data.get("workspace_id")
    if not isinstance(workspace_id, str) or not PROJECT_ID_PATTERN.fullmatch(workspace_id):
        raise ValueError("workspace_id must use lowercase letters, digits, dot, dash, or underscore")
    workspace_root_raw = data.get("workspace_root")
    if not isinstance(workspace_root_raw, str) or not workspace_root_raw.strip():
        raise ValueError("workspace_root is required")
    projects = data.get("projects")
    if not isinstance(projects, list) or not projects:
        raise ValueError("projects must be a non-empty array")

    seen: set[str] = set()
    normalized_projects: list[dict[str, Any]] = []
    workspace_root = Path(workspace_root_raw).expanduser().resolve()
    for item in projects:
        if not isinstance(item, dict):
            raise ValueError("each project entry must be an object")
        project_id = item.get("id")
        project_path = item.get("path")
        if not isinstance(project_id, str) or not PROJECT_ID_PATTERN.fullmatch(project_id):
            raise ValueError(f"invalid project id: {project_id!r}")
        if project_id in seen:
            raise ValueError(f"duplicate project id: {project_id}")
        if not isinstance(project_path, str) or not project_path.strip():
            raise ValueError(f"project path missing: {project_id}")
        seen.add(project_id)
        resolved_path = Path(project_path).expanduser()
        if not resolved_path.is_absolute():
            resolved_path = workspace_root / resolved_path
        normalized_projects.append(
            {
                **item,
                "id": project_id,
                "path": str(resolved_path.resolve()),
                "enabled": item.get("enabled", True) is not False,
            }
        )
    return {
        **data,
        "manifest_path": str(manifest_path),
        "workspace_root": str(workspace_root),
        "projects": normalized_projects,
    }


def workspace_output_dir(
    manifest: dict[str, Any],
    explicit: str | None = None,
) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    runtime_root = os.environ.get("XIRANG_V9_RUNTIME_DIR")
    root = (
        Path(runtime_root).expanduser()
        if runtime_root
        else Path.home() / ".xirang" / "v9-runtime"
    )
    return (
        root
        / "code-ref"
        / "workspaces"
        / str(manifest["workspace_id"])
    ).resolve()


def enabled_projects(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return [project for project in manifest["projects"] if project.get("enabled", True)]


def project_output_dir(output_dir: Path, project_id: str) -> Path:
    return output_dir / "projects" / project_id


def project_config_path(project: dict[str, Any]) -> Path:
    root = Path(project["path"])
    configured = project.get("config")
    if isinstance(configured, str) and configured.strip():
        value = Path(configured).expanduser()
        return value.resolve() if value.is_absolute() else (root / value).resolve()
    return (root / coderef.DEFAULT_CONFIG_REL).resolve()


def namespaced_key(project_id: str, source_key: str) -> str:
    return f"project://{project_id}/{source_key}"


def aggregate_graph(
    manifest: dict[str, Any],
    project_graphs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for project_id, graph in sorted(project_graphs.items()):
        node_map: dict[str, dict[str, Any]] = {}
        for source_node in graph.get("nodes", []):
            if not isinstance(source_node, dict) or not isinstance(source_node.get("key"), str):
                continue
            source_key = source_node["key"]
            key = namespaced_key(project_id, source_key)
            node = coderef.make_node(
                key,
                source_node.get("sha256"),
                kind=source_node.get("kind"),
                label=f"[{project_id}] {source_node.get('label', source_key)}",
                project_id=project_id,
                source_key=source_key,
                missing=source_node.get("missing"),
            )
            nodes.append(node)
            if isinstance(source_node.get("id"), str):
                node_map[source_node["id"]] = node

        for source_edge in graph.get("edges", []):
            if not isinstance(source_edge, dict):
                continue
            src_node = node_map.get(source_edge.get("src"))
            dst_node = node_map.get(source_edge.get("dst"))
            kind = source_edge.get("kind")
            if not src_node or not dst_node or not isinstance(kind, str):
                continue
            edge_id = coderef.stable_id("edge", src_node["id"], kind, dst_node["id"])
            evidence = []
            for item in source_edge.get("evidence", []):
                if isinstance(item, dict):
                    evidence.append({**item, "project_id": project_id})
            edges.append(
                {
                    "id": edge_id,
                    "src": src_node["id"],
                    "src_key": src_node["key"],
                    "kind": kind,
                    "dst": dst_node["id"],
                    "dst_key": dst_node["key"],
                    "confidence": source_edge.get("confidence", "high"),
                    "evidence": evidence,
                    "project_id": project_id,
                }
            )

    nodes.sort(key=lambda item: (item["kind"], item["key"]))
    edges.sort(key=lambda item: (item["src_key"], item["kind"], item["dst_key"]))
    return {
        "check": CHECK_NAME,
        "schema_version": SCHEMA_VERSION,
        "generated_at": coderef.now_iso(),
        "workspace_id": manifest["workspace_id"],
        "workspace_root": manifest["workspace_root"],
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "projects": len(project_graphs),
            "sources": sum(
                int(graph.get("stats", {}).get("sources", 0))
                for graph in project_graphs.values()
            ),
            "nodes": len(nodes),
            "edges": len(edges),
            "nodes_by_kind": dict(sorted(Counter(node["kind"] for node in nodes).items())),
            "edges_by_kind": dict(sorted(Counter(edge["kind"] for edge in edges).items())),
            "warnings": sum(
                int(graph.get("stats", {}).get("warnings", 0))
                for graph in project_graphs.values()
            ),
        },
        "warnings": [
            {**warning, "project_id": project_id}
            for project_id, graph in sorted(project_graphs.items())
            for warning in graph.get("warnings", [])
            if isinstance(warning, dict)
        ],
    }


def build_workspace(
    manifest_path: Path,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    manifest_path = manifest_path.expanduser().resolve()
    manifest = load_manifest(manifest_path)
    output = (output_dir or workspace_output_dir(manifest)).resolve()
    project_results: list[dict[str, Any]] = []
    project_graphs: dict[str, dict[str, Any]] = {}

    for project in enabled_projects(manifest):
        project_id = project["id"]
        root = Path(project["path"])
        project_output = project_output_dir(output, project_id)
        if not root.is_dir():
            project_results.append(
                {
                    "id": project_id,
                    "path": str(root),
                    "status": "missing",
                    "fresh": False,
                    "message": "project root does not exist",
                }
            )
            continue
        status = coderef.build_index(
            root,
            project_output,
            project_config_path(project),
        )
        graph = coderef.load_graph(project_output)
        if graph is not None:
            project_graphs[project_id] = graph
        project_results.append(
            {
                "id": project_id,
                "label": project.get("label", project_id),
                "path": str(root),
                "status": status.get("status"),
                "fresh": True,
                "summary": status.get("summary", {}),
                "output_dir": str(project_output),
            }
        )

    graph = aggregate_graph(manifest, project_graphs)
    all_present = len(project_graphs) == len(enabled_projects(manifest))
    status = {
        "check": CHECK_NAME,
        "schema_version": SCHEMA_VERSION,
        "generated_at": graph["generated_at"],
        "workspace_id": manifest["workspace_id"],
        "workspace_root": manifest["workspace_root"],
        "manifest_path": str(manifest_path),
        "manifest_sha256": coderef.sha256_file(manifest_path),
        "status": (
            "green"
            if all_present and graph["stats"]["warnings"] == 0
            else "yellow"
        ),
        "fresh": all_present,
        "projects": project_results,
        "excluded_roots": manifest.get("excluded_roots", []),
        "non_code_roots": manifest.get("non_code_roots", []),
        "summary": {
            **graph["stats"],
            "configured_projects": len(enabled_projects(manifest)),
            "built_projects": len(project_graphs),
            "changed_sources": sum(
                int(item.get("summary", {}).get("changed_sources", 0))
                for item in project_results
            ),
            "reused_sources": sum(
                int(item.get("summary", {}).get("reused_sources", 0))
                for item in project_results
            ),
            "deleted_sources": sum(
                int(item.get("summary", {}).get("deleted_sources", 0))
                for item in project_results
            ),
        },
        "paths": {
            "graph": str(output / "workspace-graph.json"),
            "status": str(output / "workspace-status.json"),
            "projects": str(output / "projects"),
        },
    }
    coderef.atomic_write_json(output / "workspace-graph.json", graph)
    coderef.atomic_write_json(output / "workspace-status.json", status)
    return status


def workspace_status(
    manifest_path: Path,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    manifest_path = manifest_path.expanduser().resolve()
    manifest = load_manifest(manifest_path)
    output = (output_dir or workspace_output_dir(manifest)).resolve()
    previous = read_json(output / "workspace-status.json")
    project_results: list[dict[str, Any]] = []
    for project in enabled_projects(manifest):
        project_id = project["id"]
        root = Path(project["path"])
        if not root.is_dir():
            result = {
                "id": project_id,
                "path": str(root),
                "status": "missing",
                "fresh": False,
            }
        else:
            result = {
                "id": project_id,
                "path": str(root),
                **coderef.graph_status(
                    root,
                    project_output_dir(output, project_id),
                ),
            }
        project_results.append(result)

    manifest_fresh = (
        isinstance(previous, dict)
        and previous.get("manifest_sha256") == coderef.sha256_file(manifest_path)
    )
    fresh = manifest_fresh and all(item.get("fresh") is True for item in project_results)
    warning_projects = [
        item["id"]
        for item in project_results
        if item.get("status") != "green"
        and item.get("fresh") is True
    ]
    green = fresh and not warning_projects
    return {
        "check": f"{CHECK_NAME}-status",
        "workspace_id": manifest["workspace_id"],
        "workspace_root": manifest["workspace_root"],
        "manifest_path": str(manifest_path),
        "manifest_fresh": manifest_fresh,
        "fresh": fresh,
        "status": "green" if green else "yellow",
        "projects": project_results,
        "summary": {
            "projects": len(project_results),
            "fresh_projects": sum(item.get("fresh") is True for item in project_results),
            "stale_projects": [
                item["id"] for item in project_results if item.get("fresh") is not True
            ],
            "warning_projects": warning_projects,
        },
    }


def validate_workspace(
    manifest_path: Path,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path.expanduser().resolve())
    output = (output_dir or workspace_output_dir(manifest)).resolve()
    aggregate = coderef.validate_graph(read_json(output / "workspace-graph.json"))
    projects = []
    for project in enabled_projects(manifest):
        result = coderef.validate_graph(
            coderef.load_graph(project_output_dir(output, project["id"]))
        )
        projects.append({"id": project["id"], **result})
    valid = aggregate.get("valid") is True and all(
        project.get("valid") is True for project in projects
    )
    return {
        "check": f"{CHECK_NAME}-validate",
        "workspace_id": manifest["workspace_id"],
        "valid": valid,
        "aggregate": aggregate,
        "projects": projects,
    }


def load_workspace_graph(
    manifest: dict[str, Any],
    output_dir: Path | None = None,
) -> dict[str, Any] | None:
    output = (output_dir or workspace_output_dir(manifest)).resolve()
    graph = read_json(output / "workspace-graph.json")
    return graph if isinstance(graph, dict) else None


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build and query isolated CodeRef graphs for a multi-project workspace."
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--manifest", required=True, help="Workspace manifest JSON")
    common.add_argument("--output-dir", help="Derived workspace cache directory")
    common.add_argument("--json", action="store_true", help="Emit structured JSON")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("build", parents=[common], help="Build every enabled project and aggregate graph")
    subparsers.add_parser("status", parents=[common], help="Check manifest and project cache freshness")
    subparsers.add_parser("validate", parents=[common], help="Validate project and aggregate graphs")
    query = subparsers.add_parser("query", parents=[common], help="Query aggregate graph neighbors")
    query.add_argument("--path", required=True, help="Namespaced key, label, id, or substring")
    query.add_argument("--depth", type=int, default=1, choices=range(1, 6))
    query.add_argument("--direction", choices=("in", "out", "both"), default="both")
    impact = subparsers.add_parser("impact", parents=[common], help="Find aggregate reverse dependents")
    impact.add_argument("--path", required=True, help="Namespaced key, label, id, or substring")
    impact.add_argument("--depth", type=int, default=2, choices=range(1, 6))
    return parser


def main() -> int:
    parser = make_parser()
    args = parser.parse_args()
    manifest_path = Path(args.manifest).expanduser().resolve()
    try:
        manifest = load_manifest(manifest_path)
    except ValueError as exc:
        coderef.print_result({"error": "invalid_manifest", "message": str(exc)}, args.json)
        return 2
    output = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else workspace_output_dir(manifest)
    )

    if args.command == "build":
        result = build_workspace(manifest_path, output)
        coderef.print_result(result, args.json)
        return 0 if result.get("fresh") else 1
    if args.command == "status":
        result = workspace_status(manifest_path, output)
        coderef.print_result(result, args.json)
        return 0 if result.get("fresh") else 1
    if args.command == "validate":
        result = validate_workspace(manifest_path, output)
        coderef.print_result(result, args.json)
        return 0 if result.get("valid") else 1

    graph = load_workspace_graph(manifest, output)
    if graph is None:
        coderef.print_result({"error": "workspace_graph_missing", "output_dir": str(output)}, args.json)
        return 1
    if args.command == "query":
        result = coderef.query_graph(graph, args.path, args.depth, args.direction)
        coderef.print_result(result, args.json)
        return 0 if result.get("found") else 1
    if args.command == "impact":
        result = coderef.impact_graph(graph, args.path, args.depth)
        coderef.print_result(result, args.json)
        return 0 if result.get("found") else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
