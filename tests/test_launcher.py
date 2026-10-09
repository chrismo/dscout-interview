import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "checkout with spaces"
        self.root.mkdir()
        self.launcher = self.root / "run"
        shutil.copy2(ROOT / "run", self.launcher)

    def test_uses_local_python_and_preserves_arguments_and_working_directory(self):
        python = self.root / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text('#!/bin/sh\nprintf "%s\\n" "$PWD" "$@"\nexit 7\n')
        python.chmod(0o755)
        result = subprocess.run(
            [str(self.launcher), "--plain", "--directory", "results with spaces"],
            cwd=self.directory.name, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout.splitlines(), [
            str(Path(self.directory.name).resolve()), "-B",
            "-m", "lib.exercise", "--plain", "--directory",
            "results with spaces",
        ])
        self.assertEqual(result.stderr, "")

    def test_missing_virtualenv_has_setup_guidance(self):
        result = subprocess.run(
            [str(self.launcher)], cwd=self.directory.name,
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(f"Run {self.root}/install first.", result.stderr)
        self.assertFalse((self.root / ".venv").exists())


class SetupScriptTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "checkout with spaces"
        self.root.mkdir()
        for name in ("install", "run-tests"):
            shutil.copy2(ROOT / name, self.root / name)
        self.python = self.root / ".venv/bin/python"
        self.python.parent.mkdir(parents=True)
        self.python.write_text('#!/bin/sh\nprintf "%s\\n" "$PWD" "$@"\nexit 7\n')
        self.python.chmod(0o755)

    def test_install_uses_checkout_paths_and_propagates_pip_failure(self):
        python3 = self.root / "python3"
        python3.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        python3.chmod(0o755)
        result = subprocess.run(
            [str(self.root / "install")], cwd=self.directory.name,
            env={**os.environ, "PATH": f"{self.root}:{os.environ['PATH']}"},
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout.splitlines(), [
            "-m", "venv", str(self.root / ".venv"),
            str(Path(self.directory.name).resolve()),
            "-m", "pip", "install", "-r", str(self.root / "requirements.txt"),
        ])

    def test_run_tests_uses_checkout_and_forwards_arguments(self):
        result = subprocess.run(
            [str(self.root / "run-tests"), "-p", "test_launcher.py"],
            cwd=self.directory.name, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 7)
        lines = result.stdout.splitlines()
        self.assertEqual(Path(lines[0]).resolve(), self.root.resolve())
        self.assertEqual(lines[1:], [
            "-B", "-m", "unittest", "discover",
            "-s", "tests", "-v", "-p", "test_launcher.py",
        ])

    def test_run_tests_requires_install(self):
        self.python.unlink()
        result = subprocess.run(
            [str(self.root / "run-tests")], cwd=self.directory.name,
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn(f"Run {self.root}/install first.", result.stderr)


if __name__ == "__main__":
    unittest.main()
