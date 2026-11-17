"""Run owning test suites in isolated processes, plus portable bootstrap contracts."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def clone_package(target):
    """Copy only versioned export files; never crawl arbitrary owner data."""
    manifest = json.loads((ROOT / "SOURCE_MANIFEST.json").read_text("utf-8"))
    for relative in [*manifest["files"], "SOURCE_MANIFEST.json"]:
        item = Path(relative)
        if item.is_absolute() or ".." in item.parts or ":" in relative or "\\" in relative:
            raise ValueError("Invalid export path")
        source = ROOT / item
        if source.is_symlink() or not source.resolve().is_relative_to(ROOT):
            raise ValueError("Linked source is not used")
        destination = target / item
        destination.parent.mkdir(parents=True, exist_ok=True)
        if relative.startswith("memory/") and relative.endswith(".md"):
            destination.write_text("# Isolated test memory\n", encoding="utf-8")
        elif relative == "memory/experience.json":
            destination.write_text(json.dumps({"schema_version": 1, "scope": "", "records": []}), encoding="utf-8")
        else:
            shutil.copyfile(source, destination)


class PortableTests(unittest.TestCase):
    def run_python(self, args, *, cwd=ROOT, expected=0):
        env = dict(os.environ)
        env.pop("BOBR_WORKBENCH_CONFIG", None)
        process = subprocess.run([sys.executable, "-B", "-X", "utf8", *args], cwd=cwd, env=env,
                                 capture_output=True, text=True, encoding="utf-8", timeout=240)
        self.assertEqual(process.returncode, expected, process.stdout + process.stderr)
        return process

    def test_owning_suites(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "owning-suites"
            clone_package(root)
            self.run_python(["configure.py"], cwd=root)
            for directory in ("tools/core", "tools/memory", "tools/workbench", "tools/knowledge"):
                with self.subTest(directory=directory):
                    self.run_python(["-m", "unittest", "discover", "-s", directory, "-v"], cwd=root)

    def test_independent_bootstrap_memory_and_missing_corpus(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "fresh-workbench"
            clone_package(root)
            for rel in ("core/config.json", "core/CORE_MANIFEST.json", "workbench/config.json", "workbench/research_config.json"):
                (root / rel).unlink(missing_ok=True)
            rejected = self.run_python(["configure.py", "--data-root", str(root / "tools")], cwd=root, expected=2)
            self.assertEqual(json.loads(rejected.stdout)["status"], "error")
            self.assertFalse((root / "core/config.json").exists())
            self.assertFalse((root / ".local").exists())
            data = Path(folder) / "external-data"
            configured = self.run_python(["configure.py", "--data-root", str(data)], cwd=root)
            self.assertEqual(json.loads(configured.stdout)["model"], "disabled")
            config = json.loads((root / "workbench/config.json").read_text("utf-8"))
            self.assertEqual(config["deployment_targets"], [])
            self.assertEqual(config["agent_root"], str(root.resolve()))
            doctor = json.loads(self.run_python(["bobr.py", "doctor"], cwd=root).stdout)
            self.assertFalse(doctor["network_used"])
            self.run_python(["bobr.py", "health"], cwd=root)
            self.run_python(["bobr.py", "verify"], cwd=root)
            value = json.loads(self.run_python(["bobr.py", "route", "--query", "бот восприятие"], cwd=root).stdout)
            self.assertEqual(value["role"], "dayz-ai")
            marker = data / "private-user-data.txt"
            marker.write_text("NOT_SOURCE", encoding="utf-8")
            profile = root / "memory/PROFILE.md"
            profile.write_text("Owner preferences preserved", encoding="utf-8")
            self.run_python(["configure.py"], cwd=root)
            self.assertEqual(profile.read_text("utf-8"), "Owner preferences preserved")
            manifest = json.loads((root / "core/CORE_MANIFEST.json").read_text("utf-8"))
            self.assertFalse(any(key.startswith("custom-data/") for key in manifest["files"]))
            self.assertNotIn("memory/PROFILE.md", manifest["files"])


if __name__ == "__main__":
    unittest.main()
