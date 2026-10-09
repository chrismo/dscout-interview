import json
import os
from pathlib import Path
import py_compile
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from lib import exercise
from lib.fixture import git


DECLARATION = '''from lib.workflow import Build, Pipeline
pipeline = Pipeline(pool="{pool}", tasks=[Build("zorch")])
'''


class ReloadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "pipeline.py"
        self.directory = self.root / "fixture"
        self.write_pipeline("first")
        location = patch("lib.exercise.PIPELINE_PATH", self.path)
        location.start()
        self.addCleanup(location.stop)
        previous = sys.modules.get("pipeline")

        def restore_module():
            if previous is None:
                sys.modules.pop("pipeline", None)
            else:
                sys.modules["pipeline"] = previous

        self.addCleanup(restore_module)

    def write_pipeline(self, pool):
        self.path.write_text(DECLARATION.format(pool=pool))

    def execute(self, **kwargs):
        return exercise.execute(self.directory, 0, observer=lambda event: None, **kwargs)

    def assert_pool(self, sim, pool):
        started = [event for event in sim.events() if event["kind"] == "pipeline_started"]
        self.assertTrue(started)
        self.assertEqual({event["pool"] for event in started}, {pool})

    def snapshot(self):
        return {str(path.relative_to(self.directory)): path.read_bytes()
                for path in self.directory.rglob("*") if path.is_file()}

    def test_same_size_same_timestamp_source_reload_on_initial_rerun_and_changes(self):
        stat = self.path.stat()
        py_compile.compile(str(self.path), doraise=True)
        first = self.execute(reset_state=True)
        self.assert_pool(first, "first")
        revisions = json.loads((self.directory / "fixture.json").read_text())["revisions"]

        self.write_pipeline("other")
        os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(self.path.stat().st_size, stat.st_size)
        self.assertEqual(self.path.stat().st_mtime_ns, stat.st_mtime_ns)
        replay = self.execute(revisions=revisions, reset_state=True)
        self.assert_pool(replay, "other")

        self.write_pipeline("third")
        os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        source = self.path.read_bytes()
        changed = self.execute(changes=[["zorch"]], reset_state=True)
        self.assert_pool(changed, "third")
        self.assertEqual(self.path.read_bytes(), source)
        self.assertNotEqual(first.state, replay.state)
        self.assertNotEqual(replay.state, changed.state)

    def test_module_identity_file_and_dataclasses(self):
        self.path.write_text('''from dataclasses import dataclass
from pathlib import Path
import sys
from lib.workflow import Pipeline
assert __name__ == "pipeline"
assert sys.modules[__name__].__dict__ is globals()
assert Path(__file__).name == "pipeline.py"
@dataclass
class Marker:
    name: str
pipeline = Pipeline(pool=Marker(Path(__file__).parent.name).name)
''')
        pipeline = exercise.load_pipeline()
        self.assertEqual(pipeline.pool, self.root.name)
        module = sys.modules["pipeline"]
        self.assertEqual(Path(module.__file__), self.path.resolve())
        self.assertEqual(module.__spec__.name, "pipeline")
        self.assertIs(module.pipeline, pipeline)
        self.assertIsNot(exercise.load_pipeline(), pipeline)

    def test_invalid_declaration_preserves_history_state_and_recovers(self):
        sim = self.execute(reset_state=True)
        before = self.snapshot()
        head = git(self.directory / "repo", "rev-parse", "HEAD")
        previous_module = sys.modules["pipeline"]
        for source, error in (("pipeline = (\n", SyntaxError),
                              ("raise RuntimeError('broken')\n", RuntimeError),
                              ("from lib.workflow import Pipeline, Build\n"
                               "pipeline = Pipeline(tasks=[Build('unknown')])\n", ValueError)):
            with self.subTest(source=source):
                self.path.write_text(source)
                with self.assertRaises(error):
                    self.execute(changes=[["greeb"]], reset_state=True)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(git(self.directory / "repo", "rev-parse", "HEAD"), head)
                self.assertEqual(self.path.read_text(), source)
                if error is not ValueError:
                    self.assertIs(sys.modules["pipeline"], previous_module)
        self.write_pipeline("fixed")
        recovered = self.execute(changes=[["greeb"]], reset_state=True)
        self.assert_pool(recovered, "fixed")
        self.assertTrue(sim.state.is_dir())
        self.assertNotEqual(git(self.directory / "repo", "rev-parse", "HEAD"), head)

    def test_invalid_initial_declaration_does_not_create_fixture(self):
        self.path.write_text("pipeline = (\n")
        with self.assertRaises(SyntaxError):
            self.execute(reset_state=True)
        self.assertFalse(self.directory.exists())

    def test_reset_state_is_non_destructive_and_cache_is_per_run(self):
        initial = self.execute()
        revisions = json.loads((self.directory / "fixture.json").read_text())["revisions"]
        previous = self.snapshot()
        replay_revisions = [revisions[0], revisions[0]]
        runs = [self.execute(revisions=replay_revisions, reset_state=True) for _ in range(2)]
        self.assertEqual(initial.state, self.directory / "state")
        self.assertNotEqual(runs[0].state, runs[1].state)
        for sim in runs:
            self.assertEqual(sim.state.parent, self.directory / "runs")
            self.assertTrue(sim.state.name.startswith("run-"))
            builds = [event for event in sim.events() if event["kind"] == "build_finished"]
            self.assertEqual([event["cache_hit"] for event in builds], [False, True])
        after = self.snapshot()
        self.assertEqual({path: after[path] for path in previous}, previous)
        default = self.execute(revisions=[revisions[0]])
        self.assertEqual(default.state, initial.state)
        builds = [event for event in default.events() if event["kind"] == "build_finished"]
        self.assertTrue(builds[-1]["cache_hit"])

    def test_import_driver_does_not_evaluate_declaration(self):
        package = self.root / "lib"
        package.mkdir()
        (package / "__init__.py").write_text("")
        for name in ("exercise.py", "fixture.py", "simulation.py", "workflow.py"):
            (package / name).write_bytes(Path(exercise.__file__).with_name(name).read_bytes())
        self.path.write_text("pipeline = (\n")
        result = subprocess.run(
            [sys.executable, "-B", "-c", "from lib import exercise; print('ready')"],
            cwd=self.root, env={**os.environ, "PYTHONPATH": str(self.root)},
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ready")
        self.assertFalse(self.directory.exists())


if __name__ == "__main__":
    unittest.main()
