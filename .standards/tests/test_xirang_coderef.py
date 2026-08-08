from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".standards" / "xirang-coderef.py"
SPEC = importlib.util.spec_from_file_location("xirang_coderef", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
coderef = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(coderef)


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class CodeRefTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_tmp = tempfile.TemporaryDirectory()
        self.out_tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.repo_tmp.name)
        self.output = Path(self.out_tmp.name) / "code-ref"

        write(
            self.repo / ".standards" / "gate.py",
            """
from pathlib import Path
import subprocess
import sys

CHECKER = Path(__file__).parent / "checker.py"

def dispatch():
    subprocess.run([sys.executable, str(CHECKER)])

def dispatch_helper():
    return _run_tool("other.py", [])
""".strip()
            + "\n",
        )
        write(
            self.repo / ".standards" / "checker.py",
            """
from pathlib import Path

SCHEMA = Path(__file__).parent / "schemas" / "state.schema.json"

def validate():
    return SCHEMA.read_text(encoding="utf-8")
""".strip()
            + "\n",
        )
        write(
            self.repo / ".standards" / "other.py",
            "def run():\n    return True\n",
        )
        write(
            self.repo / ".standards" / "hooks" / "pre-write.sh",
            """
#!/bin/bash
VAULT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
GATE="$VAULT_ROOT/.standards/gate.py"
python3 "$GATE"
""".strip()
            + "\n",
        )
        write(
            self.repo / ".standards" / "schemas" / "state.schema.json",
            '{"type":"object"}\n',
        )
        write(
            self.repo / ".standards" / "harness-tested-files.txt",
            ".standards/gate.py\n.standards/checker.py\n",
        )
        write(
            self.repo / "02-项目管理" / "脚本" / "v9-harness-eval-runner.py",
            "def main():\n    return 0\n",
        )
        write(
            self.repo / "50-经验" / "Agent协作方法论" / "V9-工具注册表-2026-06-27.md",
            "| 工具 | owner |\n|---|---|\n| `.standards/gate.py` | Codex |\n",
        )
        write(
            self.repo / ".standards" / "coderef-relations.json",
            json.dumps(
                {
                    "schema_version": "v1",
                    "nodes": [
                        {
                            "key": "runtime://status-latest.json",
                            "kind": "data_file",
                            "label": "status",
                        }
                    ],
                    "edges": [
                        {
                            "src": ".standards/gate.py",
                            "kind": "produces",
                            "dst": "runtime://status-latest.json",
                            "note": "fixture producer",
                        },
                        {
                            "src": "runtime://status-latest.json",
                            "kind": "consumed_by",
                            "dst": ".standards/checker.py",
                            "note": "fixture consumer",
                        },
                    ],
                },
                ensure_ascii=False,
            )
            + "\n",
        )
        write(
            self.repo / "node_modules" / "vendor.py",
            "def vendor():\n    return True\n",
        )
        write(
            self.repo / "components" / "Button.tsx",
            "export function Button() { return <button>OK</button>; }\n",
        )
        write(
            self.repo / "components" / "index.ts",
            'export { Button } from "./Button";\n',
        )
        write(
            self.repo / "lib" / "format.ts",
            "export const format = (value: string) => value.trim();\n",
        )
        write(
            self.repo / "app" / "page.tsx",
            """
import { Button } from "@/components";
import { format } from "../lib/format";
import "@/styles/global.css";

export default function Page() {
  return <Button>{format("home")}</Button>;
}
""".strip()
            + "\n",
        )
        write(
            self.repo / "app" / "api" / "route.ts",
            """
export async function GET() {
  const formatter = await import("../../lib/format");
  return Response.json(formatter.format("ok"));
}
""".strip()
            + "\n",
        )
        write(self.repo / "styles" / "global.css", "body { margin: 0; }\n")
        write(
            self.repo / "package.json",
            '{"name":"fixture","private":true,"type":"module"}\n',
        )

    def tearDown(self) -> None:
        self.repo_tmp.cleanup()
        self.out_tmp.cleanup()

    def build(self) -> dict:
        return coderef.build_index(
            self.repo,
            self.output,
            self.repo / ".standards" / "coderef-relations.json",
        )

    def graph(self) -> dict:
        return json.loads((self.output / "graph.json").read_text(encoding="utf-8"))

    def edge_triples(self, graph: dict) -> set[tuple[str, str, str]]:
        return {
            (edge["src_key"], edge["kind"], edge["dst_key"])
            for edge in graph["edges"]
        }

    def test_build_extracts_xirang_orchestration_relations(self) -> None:
        status = self.build()
        graph = self.graph()
        triples = self.edge_triples(graph)

        self.assertEqual(status["schema_version"], "v1")
        self.assertGreater(status["summary"]["nodes"], 0)
        self.assertNotIn("node_modules/vendor.py", {node["key"] for node in graph["nodes"]})
        self.assertIn(
            (".standards/gate.py", "invokes", ".standards/checker.py"),
            triples,
        )
        self.assertIn(
            (".standards/gate.py", "invokes", ".standards/other.py"),
            triples,
        )
        self.assertIn(
            (".standards/hooks/pre-write.sh", "invokes", ".standards/gate.py"),
            triples,
        )
        self.assertIn(
            (".standards/schemas/state.schema.json", "validated_by", ".standards/checker.py"),
            triples,
        )
        self.assertIn(
            (".standards/gate.py", "registered_in", "50-经验/Agent协作方法论/V9-工具注册表-2026-06-27.md"),
            triples,
        )
        self.assertIn(
            (".standards/gate.py", "verified_by", "02-项目管理/脚本/v9-harness-eval-runner.py"),
            triples,
        )
        self.assertIn(
            (".standards/gate.py", "produces", "runtime://status-latest.json"),
            triples,
        )
        self.assertIn(
            ("runtime://status-latest.json", "consumed_by", ".standards/checker.py"),
            triples,
        )
        self.assertIn(("app/page.tsx", "imports", "components/index.ts"), triples)
        self.assertIn(("app/page.tsx", "imports", "lib/format.ts"), triples)
        self.assertIn(("app/page.tsx", "imports", "styles/global.css"), triples)
        self.assertIn(("components/index.ts", "imports", "components/Button.tsx"), triples)
        self.assertIn(("app/api/route.ts", "imports", "lib/format.ts"), triples)

        nodes = {node["key"]: node for node in graph["nodes"]}
        self.assertEqual(nodes["app/page.tsx"]["kind"], "next_route")
        self.assertEqual(nodes["app/api/route.ts"]["kind"], "next_route")
        self.assertEqual(nodes["components/Button.tsx"]["kind"], "component")
        self.assertEqual(nodes["lib/format.ts"]["kind"], "web_module")

    def test_incremental_cache_and_stable_node_id(self) -> None:
        first = self.build()
        first_graph = self.graph()
        first_gate = next(node for node in first_graph["nodes"] if node["key"] == ".standards/gate.py")
        self.assertGreater(first["summary"]["changed_sources"], 0)

        second = self.build()
        self.assertEqual(second["summary"]["changed_sources"], 0)
        self.assertEqual(second["summary"]["reused_sources"], second["summary"]["sources"])

        gate_path = self.repo / ".standards" / "gate.py"
        gate_path.write_text(gate_path.read_text(encoding="utf-8") + "\n# body changed\n", encoding="utf-8")
        third = self.build()
        third_graph = self.graph()
        third_gate = next(node for node in third_graph["nodes"] if node["key"] == ".standards/gate.py")
        self.assertEqual(third["summary"]["changed_sources"], 1)
        self.assertEqual(first_gate["id"], third_gate["id"])
        self.assertNotEqual(first_gate["sha256"], third_gate["sha256"])

    def test_validate_query_and_impact(self) -> None:
        self.build()
        graph = self.graph()
        valid = coderef.validate_graph(graph)
        self.assertTrue(valid["valid"], valid)

        broken = copy.deepcopy(graph)
        broken["edges"][0]["dst"] = "node_missing"
        invalid = coderef.validate_graph(broken)
        self.assertFalse(invalid["valid"])
        self.assertIn("CODEREF_DANGLING_EDGE", {reason["rule_id"] for reason in invalid["reasons"]})

        query = coderef.query_graph(graph, ".standards/gate.py", 2, "both")
        self.assertTrue(query["found"])
        self.assertIn(".standards/checker.py", {node["key"] for node in query["nodes"]})

        impact = coderef.impact_graph(graph, ".standards/checker.py", 2)
        self.assertTrue(impact["found"])
        impacted = {node["key"] for node in impact["impacted"]}
        self.assertIn(".standards/gate.py", impacted)
        self.assertIn(".standards/hooks/pre-write.sh", impacted)

    def test_status_detects_new_uncached_source(self) -> None:
        self.build()
        fresh = coderef.graph_status(self.repo, self.output)
        self.assertTrue(fresh["fresh"], fresh)

        write(self.repo / ".standards" / "new-check.py", "def check():\n    return True\n")
        stale = coderef.graph_status(self.repo, self.output)
        self.assertFalse(stale["fresh"])
        self.assertIn(".standards/new-check.py", stale["uncached_sources"])

    def test_new_web_module_invalidates_inventory_dependent_fragments(self) -> None:
        page = self.repo / "app" / "page.tsx"
        page.write_text(
            page.read_text(encoding="utf-8")
            + '\nimport { feature } from "@/features/new";\n',
            encoding="utf-8",
        )
        self.build()
        first_triples = self.edge_triples(self.graph())
        self.assertNotIn(("app/page.tsx", "imports", "features/new.ts"), first_triples)

        write(self.repo / "features" / "new.ts", "export const feature = true;\n")
        second = self.build()
        second_triples = self.edge_triples(self.graph())
        self.assertGreater(second["summary"]["changed_sources"], 1)
        self.assertIn(("app/page.tsx", "imports", "features/new.ts"), second_triples)

    def test_git_inventory_excludes_tracked_files_deleted_from_worktree(self) -> None:
        deleted = self.repo / ".standards" / "deleted.py"
        write(deleted, "def old():\n    return True\n")
        subprocess.run(
            ["git", "init", "-q"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "add", ".standards/deleted.py"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
        deleted.unlink()

        self.assertNotIn(
            ".standards/deleted.py",
            coderef.git_inventory(self.repo),
        )
        status = self.build()
        self.assertEqual(status["summary"]["warnings"], 0)

    def test_fresh_graph_with_extraction_warning_remains_yellow(self) -> None:
        write(self.repo / ".standards" / "broken.py", "def broken(:\n")
        built = self.build()
        self.assertEqual(built["status"], "yellow")
        self.assertGreater(built["summary"]["warnings"], 0)

        status = coderef.graph_status(self.repo, self.output)
        self.assertTrue(status["fresh"])
        self.assertEqual(status["status"], "yellow")
        self.assertGreater(status["warnings"], 0)


if __name__ == "__main__":
    unittest.main()
