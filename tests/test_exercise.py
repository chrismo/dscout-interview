import builtins
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

from lib import exercise
from lib.fixture import create_history, git
from lib.workflow import Build, Call, Deploy, Pipeline, execute_pipeline
from lib.simulation import APPS, Simulation

ROOT = Path(__file__).resolve().parents[1]

starter_pipeline = Pipeline(
    pool="pipeline",
    tasks=[
        Build("zorch"),
        Build("greeb"),
        Build("blerg", needs=["greeb"]),
        Call("deploy-zorch", needs=["zorch"], pipeline=Pipeline(
            pool="deployment", tasks=[Deploy("deploy-zorch", "zorch")],
        )),
        Call("deploy-greeb", needs=["greeb"], pipeline=Pipeline(
            pool="deployment", tasks=[Deploy("deploy-greeb", "greeb")],
        )),
        Call("deploy-blerg", needs=["blerg", "deploy-greeb"], pipeline=Pipeline(
            pool="deployment", tasks=[Deploy("deploy-blerg", "blerg")],
        )),
    ],
)


class ExerciseTests(unittest.TestCase):
    def setUp(self):
        pipeline_patch = patch("lib.exercise.load_pipeline", return_value=starter_pipeline)
        pipeline_patch.start()
        self.addCleanup(pipeline_patch.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.repo = self.root / "repo"
        self.revisions = create_history(self.repo)
        self.sim = Simulation(self.repo, self.root / "state", speed=0)

    def test_git_history_and_sources(self):
        base, greeb, zorch = self.revisions
        self.assertEqual(git(self.repo, "rev-parse", f"{greeb}^"), base)
        self.assertEqual(git(self.repo, "rev-parse", f"{zorch}^"), greeb)
        for revision, app in ((greeb, "greeb"), (zorch, "zorch")):
            self.assertEqual(git(self.repo, "diff-tree", "--no-commit-id", "--name-only",
                                 "-r", revision), f"source/{app}.txt")
        self.assertEqual(git(self.repo, "show", f"{base}:source/greeb.txt"), "greeb-v2")
        self.assertEqual(git(self.repo, "show", f"{greeb}:source/greeb.txt"), "greeb-v3")
        self.assertEqual(git(self.repo, "show", f"{zorch}:source/zorch.txt"), "zorch-v3")

    def test_pipeline_cache_and_artifacts(self):
        # Finish each run before asserting that its published entries are reusable.
        expected_hits = (set(), {"zorch"}, {"greeb", "blerg"})
        for revision, hits in zip(self.revisions, expected_hits):
            execute_pipeline(self.sim, starter_pipeline, [revision])
            events = [e for e in self.sim.events()
                      if e["revision"] == revision and e["kind"] == "build_finished"]
            self.assertEqual({e["app"] for e in events}, set(APPS))
            self.assertEqual({e["app"] for e in events if e["cache_hit"]}, hits)
            for app in APPS:
                deployed = self.sim.deployed(app)
                self.assertEqual(deployed["image"]["digest"], self.sim.image_digest(revision, app))
                self.sim.validate_artifact(deployed["revision"], app, deployed["artifact"])
        base, greeb, zorch = self.revisions
        self.assertNotEqual(self.sim.cache_key(base, "blerg"),
                            self.sim.cache_key(greeb, "blerg"))
        self.assertEqual(self.sim.cache_key(greeb, "blerg"),
                         self.sim.cache_key(zorch, "blerg"))
        execute_pipeline(self.sim, starter_pipeline, self.revisions)
        builds = [e for e in self.sim.events() if e["kind"] == "build_finished"]
        self.assertTrue(all(e["cache_hit"] for e in builds[-9:]))

    def test_cache_is_content_based(self):
        base = self.revisions[0]
        git(self.repo, "checkout", "--quiet", "--detach", base)
        git(self.repo, "commit", "--quiet", "--allow-empty", "-m", "same source")
        other = git(self.repo, "rev-parse", "HEAD")
        self.assertNotEqual(base, other)
        execute_pipeline(self.sim, starter_pipeline, [base])
        execute_pipeline(self.sim, starter_pipeline, [other])
        hits = [e for e in self.sim.events()
                if e["kind"] == "cache_hit" and e["revision"] == other]
        self.assertEqual({e["app"] for e in hits}, set(APPS))

    def test_exact_source_bytes_affect_cache(self):
        previous = self.revisions[-1]
        (self.repo / "source/zorch.txt").write_text("zorch-v2\n\n")
        git(self.repo, "add", "source")
        git(self.repo, "commit", "--quiet", "-m", "change whitespace")
        revision = git(self.repo, "rev-parse", "HEAD")
        self.assertNotEqual(self.sim.cache_key(previous, "zorch"),
                            self.sim.cache_key(revision, "zorch"))

    def test_failed_build_does_not_publish_or_release(self):
        base, revision, _ = self.revisions
        execute_pipeline(self.sim, starter_pipeline, [base])
        release = {app: self.sim.deployed(app) for app in APPS}
        failing = Simulation(self.repo, self.sim.state, speed=0,
                             fail_build=lambda rev, app: app == "greeb")
        with self.assertRaises(RuntimeError):
            execute_pipeline(failing, starter_pipeline, [revision])
        key = self.sim.cache_key(revision, "greeb")
        self.assertFalse((self.sim.state / "cache" / f"{key}.json").exists())
        self.assertFalse(self.sim.artifact_path(revision, "greeb").exists())
        events = failing.events()
        blocked = {event["task"] for event in events
                   if event["revision"] == revision and event["kind"] == "task_blocked"}
        self.assertEqual(blocked, {"blerg", "deploy-greeb", "deploy-blerg"})
        self.assertTrue(any(event["revision"] == revision and event["kind"] == "pipeline_failed"
                            for event in events))
        self.assertEqual({app: self.sim.deployed(app) for app in APPS}, release)
        execute_pipeline(self.sim, starter_pipeline, [revision])
        self.assertEqual(self.sim.deployed("greeb")["revision"], revision)

    def test_dependency_and_release_validation(self):
        base, other, _ = self.revisions
        with self.assertRaises(ValueError):
            self.sim.build(base, "blerg")
        artifacts = {}
        for app in APPS:
            artifacts[app] = self.sim.build(base, app, artifacts.get("greeb"))
        with self.assertRaises(ValueError):
            self.sim.deploy(other, "zorch", artifacts["zorch"])
        with self.assertRaises(ValueError):
            self.sim.deploy(base, "greeb", artifacts["zorch"])
        self.assertFalse((self.sim.state / "deployments").exists())

    def test_concurrent_cache_publication(self):
        revision = self.revisions[0]
        started, release, requested = (threading.Event() for _ in range(3))

        def hold(event):
            if event["kind"] == "build_started":
                started.set()
                if not release.wait(5):
                    raise TimeoutError("build gate")

        def observe(event):
            if event["kind"] == "build_requested":
                requested.set()

        first = Simulation(self.repo, self.sim.state, speed=0, observer=hold)
        second = Simulation(self.repo, self.sim.state, speed=0, observer=observe)
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(first.build, revision, "zorch")
            try:
                self.assertTrue(started.wait(5))
                b = pool.submit(second.build, revision, "zorch")
                self.assertTrue(requested.wait(5))
                key = self.sim.cache_key(revision, "zorch")
                self.assertFalse((self.sim.state / "cache" / f"{key}.json").exists())
            finally:
                release.set()
            self.assertEqual(a.result(timeout=5), b.result(timeout=5))
        kinds = [e["kind"] for e in self.sim.events()]
        self.assertEqual(kinds.count("build_started"), 1)
        self.assertEqual(kinds.count("cache_hit"), 1)
        self.assertEqual(kinds.count("build_finished"), 2)

    def test_execute_callbacks_suppress_output(self):
        (self.root / "fixture.json").write_text(json.dumps({"revisions": self.revisions}))
        events = []
        ready = []

        def on_ready(revisions):
            self.assertEqual(revisions, self.revisions)
            self.assertEqual(events, [])
            ready.append(revisions)

        def observe(event):
            self.assertEqual(ready, [self.revisions])
            events.append(event)

        output = io.StringIO()
        with redirect_stdout(output), patch("lib.exercise.execute_pipeline",
                                            wraps=execute_pipeline) as execute:
            sim = exercise.execute(self.root, 0, observer=observe, on_ready=on_ready)
        execute.assert_called_once_with(sim, starter_pipeline, self.revisions)
        self.assertIsInstance(sim, Simulation)
        self.assertEqual(sim.observer, observe)
        self.assertCountEqual(events, sim.events())
        self.assertTrue(events)
        self.assertEqual(output.getvalue(), "")

    def test_cli_repeated_invocations(self):
        directory = self.root / "cli"
        command = [sys.executable, "-B", "-m", "lib.exercise",
                   "--plain", "--directory", str(directory), "--speed", "0"]
        for _ in range(2):
            result = subprocess.run(command, check=True, capture_output=True,
                                    text=True, cwd=ROOT)
            self.assertIn(f"Fixture: {directory.resolve()}", result.stdout)
            history = json.loads((directory / "fixture.json").read_text())["revisions"]
            tips = [line.removeprefix("PR tip: ") for line in result.stdout.splitlines()
                    if line.startswith("PR tip: ")]
            self.assertEqual(tips, history)
            self.assertNotIn("Revision: ", result.stdout)
            self.assertIn("build_finished", result.stdout)
            self.assertIn("pipeline_started", result.stdout)
            self.assertIn("pipeline_finished", result.stdout)
            self.assertIn(f"Deployments: {directory.resolve() / 'state/deployments'}", result.stdout)
        events = [json.loads(line) for line in
                  (directory / "state/events.jsonl").read_text().splitlines()]
        builds = [e for e in events if e["kind"] == "build_finished"]
        self.assertEqual(len(builds), 18)
        self.assertTrue(all(e["cache_hit"] for e in builds[-9:]))
        sim = Simulation(directory / "repo", directory / "state", speed=0)
        for app in APPS:
            deployed = sim.deployed(app)
            sim.validate_artifact(deployed["revision"], app, deployed["artifact"])


class DriverTests(unittest.TestCase):
    def test_plain_does_not_import_ui(self):
        original_import = builtins.__import__

        def import_without_ui(name, *args, **kwargs):
            if name == "lib.tui" or name == "textual" or name.startswith("textual."):
                self.fail(f"plain mode imported {name}")
            return original_import(name, *args, **kwargs)

        with patch("sys.argv", ["exercise.py", "--plain", "--speed", "0"]), \
                patch("builtins.__import__", side_effect=import_without_ui), \
                patch("lib.exercise.execute") as execute:
            exercise.main()
        directory, speed = execute.call_args.args
        self.assertEqual(speed, 0)
        self.assertFalse(directory.exists())

    def test_ui_temp_directory_lasts_until_app_closes(self):
        directories = []

        def run_app():
            directory, speed = app_class.call_args.args
            self.assertTrue(directory.is_dir())
            self.assertEqual(speed, 0)
            directories.append(directory)

        app = Mock(failed=False, return_code=0)
        app.run.side_effect = run_app
        app_class = Mock(return_value=app)
        with patch.dict(sys.modules, {"lib.tui": SimpleNamespace(BuildApp=app_class)}), \
                patch("sys.argv", ["exercise.py", "--speed", "0"]):
            exercise.main()
        app.run.assert_called_once_with()
        self.assertFalse(directories[0].exists())

    def test_ui_failure_exits_nonzero_after_run(self):
        for failed, return_code in ((True, 0), (False, 1)):
            with self.subTest(failed=failed, return_code=return_code):
                app = Mock(failed=failed, return_code=return_code)
                with patch.dict(sys.modules, {"lib.tui": SimpleNamespace(BuildApp=Mock(return_value=app))}), \
                        patch("sys.argv", ["exercise.py"]), \
                        self.assertRaises(SystemExit) as raised:
                    exercise.main()
                app.run.assert_called_once_with()
                self.assertEqual(raised.exception.code, 1)

    def test_missing_textual_has_install_guidance(self):
        original_import = builtins.__import__

        def missing_textual(name, *args, **kwargs):
            if name == "lib.tui":
                raise ModuleNotFoundError("No module named 'textual'", name="textual")
            return original_import(name, *args, **kwargs)

        error = io.StringIO()
        with patch("sys.argv", ["exercise.py"]), \
                patch("builtins.__import__", side_effect=missing_textual), \
                redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            exercise.main()
        self.assertEqual(raised.exception.code, 2)
        self.assertIn(".venv/bin/python -m pip install -r requirements.txt", error.getvalue())
        self.assertIn("--plain", error.getvalue())

    def test_unrelated_missing_import_is_not_swallowed(self):
        original_import = builtins.__import__

        def missing_dependency(name, *args, **kwargs):
            if name == "lib.tui":
                raise ModuleNotFoundError("No module named 'other'", name="other")
            return original_import(name, *args, **kwargs)

        with patch("sys.argv", ["exercise.py"]), \
                patch("builtins.__import__", side_effect=missing_dependency), \
                self.assertRaises(ModuleNotFoundError):
            exercise.main()

    def test_invalid_speed(self):
        for speed in ("-1", "nan", "inf", "-inf"):
            with self.subTest(speed=speed), \
                    patch("sys.argv", ["exercise.py", "--plain", f"--speed={speed}"]), \
                    redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                exercise.main()
            self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
