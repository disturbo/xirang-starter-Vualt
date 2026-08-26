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


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temp = tempfile.TemporaryDirectory(prefix="xirang-v97-tests-")
        cls.base = Path(cls._temp.name)
        cls.dist_a = cls.base / "dist-a"
        cls.dist_b = cls.base / "dist-b"
        subprocess.run([sys.executable, str(BUILD), "--output-dir", str(cls.dist_a)], check=True, capture_output=True, text=True)
        subprocess.run([sys.executable, str(BUILD), "--output-dir", str(cls.dist_b)], check=True, capture_output=True, text=True)
        cls.asset = cls.dist_a / "xi-rang-v9.7.0-universal.zip"

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temp.cleanup()

    def package(self, name: str) -> Path:
        destination = self.base / f"package-{name}"
        with zipfile.ZipFile(self.asset) as archive:
            archive.extractall(destination)
        return destination / "xi-rang-v9.7.0"

    def run_setup(self, package: Path, install_root: Path, *arguments: str) -> tuple[int, dict]:
        environment = dict(os.environ)
        environment["XIRANG_INSTALL_ROOT"] = str(install_root)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            ["/bin/bash", str(package / "setup.sh"), *arguments],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
        return completed.returncode, json.loads(completed.stdout)

    def test_build_is_reproducible(self) -> None:
        for name in ("xi-rang-v9.7.0-universal.zip", "release-manifest.json", "SHA256SUMS"):
            self.assertEqual(digest(self.dist_a / name), digest(self.dist_b / name), name)

    def test_tampered_package_fails_closed(self) -> None:
        package = self.package("tamper")
        (package / "README.md").write_text("tampered", encoding="utf-8")
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
