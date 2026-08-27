from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "tools/build_release.py"
INSTALLER_SOURCE = ROOT / "installer/xirang_install.py"
ASSET = "xi-rang-v9.7.2-starter.zip"
ARCHIVE_ROOT = "xi-rang-v9.7.2-starter"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temp = tempfile.TemporaryDirectory(prefix="xirang-v972-tests-")
        cls.base = Path(cls._temp.name)
        cls.dist_a = cls.base / "dist-a"
        cls.dist_b = cls.base / "dist-b"
        subprocess.run([sys.executable, str(BUILD), "--output-dir", str(cls.dist_a)], check=True, capture_output=True, text=True)
        subprocess.run([sys.executable, str(BUILD), "--output-dir", str(cls.dist_b)], check=True, capture_output=True, text=True)
        cls.asset = cls.dist_a / ASSET

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temp.cleanup()

    def package(self, name: str) -> Path:
        destination = self.base / f"package-{name}"
        with zipfile.ZipFile(self.asset) as archive:
            archive.extractall(destination)
        return destination / ARCHIVE_ROOT

    @staticmethod
    def upgrade(package: Path) -> Path:
        return package / ".xirang/distribution/upgrade"

    def run_setup(self, package: Path, install_root: Path, *arguments: str) -> tuple[int, dict]:
        environment = dict(os.environ)
        environment["XIRANG_INSTALL_ROOT"] = str(install_root)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            ["/bin/bash", str(self.upgrade(package) / "setup.sh"), *arguments],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
        return completed.returncode, json.loads(completed.stdout)

    def run_extras(self, package: Path, target: Path, action: str) -> tuple[int, dict]:
        completed = subprocess.run(
            [sys.executable, str(package / ".xirang/distribution/install_extras.py"), action, "--target", str(target)],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        return completed.returncode, json.loads(completed.stdout)

    def test_build_is_reproducible(self) -> None:
        self.assertEqual(
            {path.name for path in self.dist_a.iterdir()},
            {ASSET, "release-manifest.json", "SHA256SUMS"},
        )
        for name in (ASSET, "release-manifest.json", "SHA256SUMS"):
            self.assertEqual(digest(self.dist_a / name), digest(self.dist_b / name), name)
        release = json.loads((self.dist_a / "release-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(release["version"], "9.7.2")
        self.assertEqual(release["tag"], "v9.7.2")
        self.assertEqual(
            release["release_url"],
            "https://github.com/disturbo/xirang-starter-Vualt/releases/tag/v9.7.2",
        )
        self.assertEqual(release["asset"]["name"], ASSET)
        self.assertEqual(
            release["asset"]["download_url"],
            "https://github.com/disturbo/xirang-starter-Vualt/releases/download/v9.7.2/xi-rang-v9.7.2-starter.zip",
        )
        self.assertEqual(release["contents"]["obsidian_plugins"], 16)

    def test_complete_package_is_an_openable_obsidian_knowledge_base(self) -> None:
        with zipfile.ZipFile(self.asset) as archive:
            names = set(archive.namelist())
            root = ARCHIVE_ROOT + "/"
            for required in (
                "🏠-Home.md",
                "息壤.md",
                "AGENT-SETUP.md",
                "00-MOC/知识库导航.md",
                "00-MOC/Skill-Inventory.md",
                "00-MOC/工作台.md",
                "02-项目管理/README.md",
                "10-项目/README.md",
                "20-资料/README.md",
                "30-规范/README.md",
                "30-规范/流程图绘制规范.md",
                "30-规范/SVG架构图设计规范.md",
                "40-决策/README.md",
                "50-经验/教训库.md",
                "60-归档/README.md",
                "70-模板/T-PRD.md",
                ".obsidian/workspace.json",
                ".obsidian/themes/Things/theme.css",
                ".obsidian/snippets/better-tables.css",
                ".skills/RESOLVER.md",
                ".xirang/distribution/verify_complete.py",
                ".xirang/distribution/upgrade/setup.sh",
            ):
                self.assertIn(root + required, names, required)
            for technical in ("baselines/", "installer/", "manifests/", "payload/", "templates/"):
                self.assertFalse(any(name.startswith(root + technical) for name in names), technical)
            self.assertFalse(any(name.endswith("/data.json") for name in names))
            self.assertFalse(any("/__pycache__/" in name or name.endswith(".pyc") for name in names))
            workspace = json.loads(archive.read(root + ".obsidian/workspace.json"))
            self.assertEqual(workspace["main"]["children"][0]["children"][0]["state"]["state"]["file"], "🏠-Home.md")

    def test_plugin_and_skill_closures_match_their_registries(self) -> None:
        package = self.package("registry")
        declared_plugins = json.loads((package / ".obsidian/community-plugins.json").read_text())
        actual_plugins = []
        for manifest in sorted((package / ".obsidian/plugins").glob("*/manifest.json")):
            actual_plugins.append(json.loads(manifest.read_text())["id"])
            self.assertTrue((manifest.parent / "main.js").is_file(), manifest)
            self.assertTrue((manifest.parent / "LICENSE").is_file(), manifest)
            self.assertFalse((manifest.parent / "data.json").exists(), manifest)
        self.assertEqual(set(declared_plugins), set(actual_plugins))
        self.assertEqual(len(actual_plugins), 16)
        self.assertIn("editing-toolbar", actual_plugins)
        editing_toolbar = package / ".obsidian/plugins/editing-toolbar"
        self.assertEqual(json.loads((editing_toolbar / "manifest.json").read_text())["version"], "4.1.1")
        self.assertTrue((editing_toolbar / "styles.css").is_file())
        self.assertIn("floating-toc", actual_plugins)
        self.assertIn("supercharged-links-obsidian", actual_plugins)
        self.assertNotIn("obsidian-quiet-outline", actual_plugins)
        self.assertNotIn("xirang-workbench", actual_plugins)
        skills = sorted(path.parent.name for path in (package / ".skills").glob("*/SKILL.md"))
        self.assertEqual(len(skills), 20)
        self.assertNotIn("yijing-prd-spec", skills)
        self.assertNotIn("flowforge", skills)

    def test_visible_and_upgrade_core_manifests_are_identical(self) -> None:
        package = self.package("core")
        root_core = (package / ".xirang/distribution/core-manifest.json").read_bytes()
        upgrade_core = (self.upgrade(package) / "manifests/core-manifest.json").read_bytes()
        self.assertEqual(root_core, upgrade_core)
        paths = {row["path"] for row in json.loads(root_core)["files"]}
        self.assertIn("30-规范/面向人的五步协作视图.md", paths)
        self.assertIn(".standards/xirang-task.py", paths)
        self.assertNotIn("🏠-Home.md", paths)
        self.assertNotIn("00-MOC/Skill-Inventory.md", paths)
        lifecycle = json.loads((package / ".xirang/distribution/payload-lifecycle.json").read_text())
        lifecycle_paths = {row["path"] for row in lifecycle["files"]}
        self.assertIn("🏠-Home.md", lifecycle_paths)
        self.assertIn("00-MOC/Skill-Inventory.md", lifecycle_paths)
        self.assertNotIn(".obsidian/workspace.json", paths)
        self.assertFalse(any(path.startswith(".skills/") for path in paths))

    def test_complete_manifest_verifier_detects_tampering(self) -> None:
        package = self.package("complete-verify")
        verifier = package / ".xirang/distribution/verify_complete.py"
        good = subprocess.run([sys.executable, str(verifier)], capture_output=True, text=True, check=False)
        self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
        self.assertTrue(json.loads(good.stdout)["ok"])
        (package / "🏠-Home.md").write_text("tampered", encoding="utf-8")
        bad = subprocess.run([sys.executable, str(verifier)], capture_output=True, text=True, check=False)
        self.assertNotEqual(bad.returncode, 0)
        self.assertEqual(json.loads(bad.stdout)["status"], "manifest_invalid")

    def test_extras_install_preserves_existing_preferences_and_fails_on_content_conflict(self) -> None:
        package = self.package("extras")
        target = self.base / "extras-target"
        (target / ".obsidian").mkdir(parents=True)
        (target / ".obsidian/appearance.json").write_text(
            json.dumps({"cssTheme": "My Theme", "enabledCssSnippets": ["mine"]}), encoding="utf-8"
        )
        (target / ".obsidian/community-plugins.json").write_text(json.dumps(["existing-plugin"]), encoding="utf-8")
        code, result = self.run_extras(package, target, "apply")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "extras_installed")
        appearance = json.loads((target / ".obsidian/appearance.json").read_text())
        plugins = json.loads((target / ".obsidian/community-plugins.json").read_text())
        self.assertEqual(appearance["cssTheme"], "My Theme")
        self.assertIn("mine", appearance["enabledCssSnippets"])
        self.assertIn("better-tables", appearance["enabledCssSnippets"])
        self.assertIn("existing-plugin", plugins)
        self.assertIn("dataview", plugins)
        self.assertTrue((target / ".skills/start-task/SKILL.md").is_file())
        self.assertTrue((target / ".obsidian/plugins/dataview/main.js").is_file())
        preset_data = {
            path.relative_to(target).as_posix()
            for path in target.rglob("data.json")
        }
        self.assertEqual(
            preset_data,
            {
                ".obsidian/plugins/floating-toc/data.json",
                ".obsidian/plugins/supercharged-links-obsidian/data.json",
                ".obsidian/plugins/templater-obsidian/data.json",
            },
        )
        self.assertTrue(Path(result["backup"]).is_dir())

        conflict_target = self.base / "extras-conflict"
        conflict = conflict_target / ".skills/start-task/SKILL.md"
        conflict.parent.mkdir(parents=True)
        conflict.write_text("custom", encoding="utf-8")
        code, blocked = self.run_extras(package, conflict_target, "apply")
        self.assertNotEqual(code, 0)
        self.assertEqual(blocked["status"], "assistance_required")
        self.assertEqual(conflict.read_text(), "custom")
        self.assertFalse((conflict_target / ".obsidian/plugins/dataview/main.js").exists())

    def test_bundles_exclude_personal_project_runtime_and_secret_material(self) -> None:
        forbidden_text = (
            "yudongbo",
            "余东波",
            "波波",
            "联友",
            "奕境",
            "BEGIN PRIVATE KEY",
            "github_pat_",
            "ghp_",
        )
        forbidden_parts = (
            "/.git/",
            "/.private/",
            "/.wrangler/",
            "/__pycache__/",
            "/node_modules/",
            "/site-packages/",
            ".sqlite3",
            ".jsonl",
            ".pem",
            ".key",
            "/data.json",
        )
        with zipfile.ZipFile(self.asset) as archive:
            for name in archive.namelist():
                self.assertFalse(any(part in name for part in forbidden_parts), name)
                text = archive.read(name).decode("utf-8", errors="replace")
                self.assertFalse(any(needle in text for needle in forbidden_text), name)

    def test_tampered_upgrade_package_fails_closed(self) -> None:
        package = self.package("tamper")
        (self.upgrade(package) / "README.md").write_text("tampered", encoding="utf-8")
        code, result = self.run_setup(package, self.base / "user-tamper", "plan", "--target", str(self.base / "workspace-tamper"))
        self.assertNotEqual(code, 0)
        self.assertEqual(result["status"], "manifest_invalid")
        self.assertFalse((self.base / "workspace-tamper").exists())

    def test_fresh_install_current_repair_and_contract_lint(self) -> None:
        package = self.package("fresh")
        target = self.base / "workspace-fresh"
        target.mkdir()
        (target / "AGENTS.md").write_text("# Project rules\n\nKeep this sentence.\n", encoding="utf-8")
        user_root = self.base / "user-fresh"
        code, first = self.run_setup(package, user_root, "apply", "--target", str(target), "--no-scheduler")
        self.assertEqual(code, 0, first)
        self.assertEqual(first["status"], "installed")
        agents = (target / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("Keep this sentence.", agents)
        self.assertEqual(agents.count("XIRANG-V97-MANAGED-START"), 1)
        self.assertTrue((target / "50-经验/Agent协作方法论/息壤方法论-V9.md").is_file())

        code, second = self.run_setup(package, user_root, "apply", "--target", str(target), "--no-scheduler")
        self.assertEqual(code, 0, second)
        self.assertEqual(second["status"], "current_verified")
        agents = (target / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual(agents.count("XIRANG-V97-MANAGED-START"), 1)

        environment = dict(os.environ)
        environment["XIRANG_INSTALL_ROOT"] = str(user_root)
        environment["XIRANG_RUNTIME_DIR"] = str(user_root / "workspaces" / second["verification"]["workspace_id"])
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        lint = subprocess.run(
            [sys.executable, str(target / ".standards/xirang-task.py"), "contract-lint", "--root", str(target)],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
        lint_result = json.loads(lint.stdout)
        self.assertEqual(lint.returncode, 0, lint_result)
        self.assertTrue(lint_result["ok"], lint_result)

    def test_extracted_complete_vault_can_activate_without_losing_extras(self) -> None:
        package = self.package("self-activate")
        plugin = package / ".obsidian/plugins/dataview/main.js"
        skill = package / ".skills/start-task/SKILL.md"
        plugin_before = digest(plugin)
        skill_before = digest(skill)

        code, installed = self.run_setup(
            package,
            self.base / "user-self-activate",
            "apply",
            "--target",
            str(package),
            "--no-scheduler",
        )
        self.assertEqual(code, 0, installed)
        self.assertIn(installed["status"], {"installed", "current_verified"})
        extras_code, extras = self.run_extras(package, package, "apply")
        self.assertEqual(extras_code, 0, extras)
        self.assertIn(extras["status"], {"extras_installed", "current_verified"})
        extras_code, extras_again = self.run_extras(package, package, "apply")
        self.assertEqual(extras_code, 0, extras_again)
        self.assertEqual(extras_again["status"], "current_verified")
        self.assertEqual(digest(plugin), plugin_before)
        self.assertEqual(digest(skill), skill_before)
        self.assertEqual(
            {
                path.relative_to(package).as_posix()
                for path in package.rglob("data.json")
            },
            {
                ".obsidian/plugins/floating-toc/data.json",
                ".obsidian/plugins/supercharged-links-obsidian/data.json",
                ".obsidian/plugins/templater-obsidian/data.json",
            },
        )

    def test_unknown_install_requires_assistance(self) -> None:
        package = self.package("unknown")
        target = self.base / "workspace-unknown"
        (target / ".xirang/contract").mkdir(parents=True)
        (target / ".xirang/contract/policy.yaml").write_text("policy_version: 1.2.3\n", encoding="utf-8")
        marker = target / "untouched.txt"
        marker.write_text("preserve", encoding="utf-8")
        code, result = self.run_setup(package, self.base / "user-unknown", "apply", "--target", str(target), "--no-scheduler")
        self.assertNotEqual(code, 0)
        self.assertEqual(result["status"], "assistance_required")
        self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")

    def test_workbuddy_entry_and_unknown_platform_fallback(self) -> None:
        package = self.package("platforms")
        workbuddy_target = self.base / "workspace-workbuddy"
        workbuddy_target.mkdir()
        workbuddy_root = self.base / "user-workbuddy"
        native = workbuddy_root / "platform-entries/workbuddy/CODEBUDDY.md"
        native.parent.mkdir(parents=True)
        native.write_text("# Existing WorkBuddy rule\n", encoding="utf-8")
        code, result = self.run_setup(
            package, workbuddy_root, "apply", "--target", str(workbuddy_target),
            "--platform", "workbuddy", "--no-scheduler",
        )
        self.assertEqual(code, 0, result)
        self.assertEqual(result["platform_entry"]["state"], "native_entry_applied")
        native_text = native.read_text(encoding="utf-8")
        self.assertIn("Existing WorkBuddy rule", native_text)
        self.assertEqual(native_text.count("XIRANG-V97-PLATFORM-START"), 1)

        unknown_target = self.base / "workspace-unknown-platform"
        unknown_target.mkdir()
        code, unknown = self.run_setup(
            package, self.base / "user-unknown-platform", "apply", "--target", str(unknown_target),
            "--platform", "future_agent", "--no-scheduler",
        )
        self.assertEqual(code, 0, unknown)
        self.assertFalse(unknown["platform_entry"]["registered_template"])
        self.assertEqual(unknown["platform_entry"]["state"], "workspace_entry_configured")
        registry = json.loads((unknown_target / ".xirang/adapters/registry.json").read_text(encoding="utf-8"))
        self.assertEqual(registry["platforms"]["future_agent"]["allowed_mode"], "contract_only")

    def test_payload_lifecycle_closes_over_every_payload_file(self) -> None:
        lifecycle = json.loads((ROOT / "starter-vault/.xirang/distribution/payload-lifecycle.json").read_text())
        declared = {row["path"]: row["lifecycle"] for row in lifecycle["files"]}
        actual = {path.relative_to(ROOT / "payload").as_posix() for path in (ROOT / "payload").rglob("*") if path.is_file()}
        self.assertEqual(set(declared), actual)
        self.assertEqual(sum(value == "managed_core" for value in declared.values()), 56)
        self.assertEqual(sum(value == "merge" for value in declared.values()), 1)
        self.assertEqual(sum(value == "seed_if_absent" for value in declared.values()), 34)

    def test_portable_standard_source_map_has_no_omissions(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(ROOT / "tools/check_portable_standards.py")],
            capture_output=True, text=True, check=False,
        )
        result = json.loads(completed.stdout)
        self.assertEqual(completed.returncode, 0, result)
        self.assertEqual(result["mappings"], 17)

    def test_portable_knowledge_base_links_and_semantics_are_closed(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(ROOT / "tools/check_knowledge_base.py"), "--root", str(ROOT / "payload")],
            capture_output=True, text=True, check=False,
        )
        result = json.loads(completed.stdout)
        self.assertEqual(completed.returncode, 0, result)
        self.assertTrue(result["ok"], result)

    def test_obsidian_presets_and_zip_metadata_are_closed(self) -> None:
        package = self.package("obsidian-presets")
        plugins = set(json.loads((package / ".obsidian/community-plugins.json").read_text()))
        presets = {path.stem for path in (package / ".xirang/distribution/obsidian-presets").glob("*.json")}
        self.assertTrue({"floating-toc", "supercharged-links-obsidian", "templater-obsidian"} <= presets)
        self.assertTrue(presets <= plugins)
        workspace = (package / ".obsidian/workspace.json").read_text(encoding="utf-8")
        self.assertNotIn("outline", workspace)
        self.assertNotIn("obsidian-quiet-outline", workspace)
        with zipfile.ZipFile(self.asset) as archive:
            names = archive.namelist()
        self.assertFalse(any(name.startswith("__MACOSX/") or "/._" in name for name in names))

    def test_injected_failure_restores_preimage(self) -> None:
        package = self.package("rollback")
        target = self.base / "workspace-rollback"
        target.mkdir()
        agents = target / "AGENTS.md"
        agents.write_text("# Existing project contract\n", encoding="utf-8")
        unknown = target / "business.txt"
        unknown.write_text("do not change", encoding="utf-8")
        code, result = self.run_setup(
            package,
            self.base / "user-rollback",
            "apply",
            "--target",
            str(target),
            "--no-scheduler",
            "--inject-failure",
            "after_payload",
        )
        self.assertNotEqual(code, 0)
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(agents.read_text(encoding="utf-8"), "# Existing project contract\n")
        self.assertEqual(unknown.read_text(encoding="utf-8"), "do not change")
        self.assertFalse((target / "VERSION").exists())
        self.assertFalse((target / ".xirang/contract/policy.yaml").exists())

    def test_supported_baseline_detection_and_custom_agents_merge(self) -> None:
        spec = importlib.util.spec_from_file_location("xirang_install", INSTALLER_SOURCE)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        target = self.base / "workspace-baseline"
        package = self.base / "baseline-package"
        (package / "baselines").mkdir(parents=True)
        target.mkdir()
        (target / "VERSION").write_text("9.5.0\n", encoding="utf-8")
        (target / "息壤.md").write_text("legacy\n", encoding="utf-8")
        (target / "core.txt").write_text("known core\n", encoding="utf-8")
        baseline = {
            "supported": [{"version": "9.5.0", "required_hashes": {"core.txt": digest(target / "core.txt")}}]
        }
        (package / "baselines/supported.json").write_text(json.dumps(baseline), encoding="utf-8")
        detection = module.detect_install(target, package)
        self.assertEqual(detection["mode"], "upgrade")
        merged = module.merge_agents("# User project\n", f"{module.MANAGED_START}\nmanaged\n{module.MANAGED_END}\n")
        self.assertIn("# User project", merged)
        self.assertIn(module.MANAGED_START, merged)


if __name__ == "__main__":
    unittest.main(verbosity=2)
