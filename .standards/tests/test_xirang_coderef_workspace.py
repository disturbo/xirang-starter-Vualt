from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".standards" / "xirang-coderef-workspace.py"
SPEC = importlib.util.spec_from_file_location("xirang_coderef_workspace", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
workspace = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(workspace)


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class WorkspaceCodeRefTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sandbox = self.root / "sandbox"
        self.external = self.root / "external-app"
        self.output = self.root / "runtime"
        self.manifest = self.sandbox / ".xirang-coderef-workspace.json"

        write(
            self.sandbox / "web" / "app" / "page.tsx",
            'import { Card } from "../components/Card";\nexport default function Page() { return <Card />; }\n',
        )
        write(
            self.sandbox / "web" / "components" / "Card.tsx",
            "export function Card() { return <article />; }\n",
        )
        write(
            self.sandbox / "web" / "node_modules" / "vendor.ts",
            "export const vendor = true;\n",
        )
        write(
            self.sandbox / "worker" / "main.py",
            "from helper import run\n\nrun()\n",
        )
        write(self.sandbox / "worker" / "helper.py", "def run():\n    return True\n")
        write(
            self.external / "src" / "index.ts",
            'export { value } from "./value";\n',
        )
        write(self.external / "src" / "value.ts", "export const value = 1;\n")
        write(
            self.manifest,
            json.dumps(
                {
                    "schema_version": "v1",
                    "workspace_id": "fixture",
                    "workspace_root": str(self.sandbox),
                    "projects": [
                        {"id": "web", "path": "web"},
                        {"id": "worker", "path": "worker"},
                        {"id": "external", "path": str(self.external)},
                    ],
                    "excluded_roots": ["archive"],
                },
                ensure_ascii=False,
            )
            + "\n",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def graph(self) -> dict:
        return json.loads(
            (self.output / "workspace-graph.json").read_text(encoding="utf-8")
        )

    def test_build_isolates_projects_and_validates_aggregate(self) -> None:
        result = workspace.build_workspace(self.manifest, self.output)
        self.assertTrue(result["fresh"], result)
        self.assertEqual(result["summary"]["configured_projects"], 3)
        self.assertEqual(result["summary"]["built_projects"], 3)
        for project_id in ("web", "worker", "external"):
            self.assertTrue(
                (self.output / "projects" / project_id / "graph.json").is_file()
            )

        graph = self.graph()
        keys = {node["key"] for node in graph["nodes"]}
        self.assertIn("project://web/app/page.tsx", keys)
        self.assertIn("project://worker/main.py", keys)
        self.assertIn("project://external/src/index.ts", keys)
        self.assertFalse(any("node_modules" in key for key in keys))

        triples = {
            (edge["src_key"], edge["kind"], edge["dst_key"])
            for edge in graph["edges"]
        }
        self.assertIn(
            (
                "project://web/app/page.tsx",
                "imports",
                "project://web/components/Card.tsx",
            ),
            triples,
        )
        self.assertIn(
            (
                "project://external/src/index.ts",
                "imports",
                "project://external/src/value.ts",
            ),
            triples,
        )
        validation = workspace.validate_workspace(self.manifest, self.output)
        self.assertTrue(validation["valid"], validation)

    def test_unchanged_build_reuses_all_sources(self) -> None:
        first = workspace.build_workspace(self.manifest, self.output)
        self.assertGreater(first["summary"]["changed_sources"], 0)
        second = workspace.build_workspace(self.manifest, self.output)
        self.assertEqual(second["summary"]["changed_sources"], 0)
        self.assertEqual(
            second["summary"]["reused_sources"],
            second["summary"]["sources"],
        )

    def test_status_identifies_only_changed_project(self) -> None:
        workspace.build_workspace(self.manifest, self.output)
        card = self.sandbox / "web" / "components" / "Card.tsx"
        card.write_text(
            card.read_text(encoding="utf-8") + "\n// changed\n",
            encoding="utf-8",
        )
        status = workspace.workspace_status(self.manifest, self.output)
        self.assertFalse(status["fresh"])
        self.assertEqual(status["summary"]["stale_projects"], ["web"])

        rebuilt = workspace.build_workspace(self.manifest, self.output)
        changed_by_project = {
            item["id"]: item["summary"]["changed_sources"]
            for item in rebuilt["projects"]
        }
        self.assertEqual(changed_by_project["web"], 1)
        self.assertEqual(changed_by_project["worker"], 0)
        self.assertEqual(changed_by_project["external"], 0)

    def test_aggregate_query_and_impact_remain_project_scoped(self) -> None:
        workspace.build_workspace(self.manifest, self.output)
        graph = self.graph()
        query = workspace.coderef.query_graph(
            graph,
            "project://web/app/page.tsx",
            1,
            "out",
        )
        self.assertTrue(query["found"])
        self.assertIn(
            "project://web/components/Card.tsx",
            {node["key"] for node in query["nodes"]},
        )
        self.assertFalse(
            any(
                node["key"].startswith("project://worker/")
                for node in query["nodes"]
            )
        )

        impact = workspace.coderef.impact_graph(
            graph,
            "project://web/components/Card.tsx",
            1,
        )
        self.assertIn(
            "project://web/app/page.tsx",
            {node["key"] for node in impact["impacted"]},
        )

    def test_fresh_warning_project_keeps_workspace_yellow(self) -> None:
        write(self.sandbox / "worker" / "broken.py", "def broken(:\n")
        built = workspace.build_workspace(self.manifest, self.output)
        self.assertTrue(built["fresh"])
        self.assertEqual(built["status"], "yellow")

        status = workspace.workspace_status(self.manifest, self.output)
        self.assertTrue(status["fresh"])
        self.assertEqual(status["status"], "yellow")
        self.assertEqual(status["summary"]["warning_projects"], ["worker"])


if __name__ == "__main__":
    unittest.main()
