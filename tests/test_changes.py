import itertools
import json
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from lib.exercise import execute
from lib.fixture import APPS, add_changes, create_history, git
from lib.workflow import Build, Call, Deploy, Pipeline


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


class ChangesTests(unittest.TestCase):
    def setUp(self):
        pipeline_patch = patch("lib.exercise.load_pipeline", return_value=starter_pipeline)
        pipeline_patch.start()
        self.addCleanup(pipeline_patch.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def execute(self, directory=None, **kwargs):
        events, ready = [], []
        sim = execute(directory or self.root, 0, observer=events.append,
                      on_ready=ready.append, **kwargs)
        self.assertEqual(len(ready), 1)
        self.assertEqual({event["revision"] for event in events}, set(ready[0]))
        return sim, ready[0], events

    def history(self, directory=None):
        return json.loads(((directory or self.root) / "fixture.json").read_text())["revisions"]

    def test_all_selections_change_exact_sources_and_reuse_cache(self):
        for flags in itertools.product((False, True), repeat=3):
            selected = tuple(app for app, flag in zip(APPS, flags) if flag)
            with self.subTest(selected=selected):
                directory = self.root / "".join(str(int(flag)) for flag in flags)
                sim, history, _ = self.execute(directory)
                previous = history[-1]
                sim, batch, events = self.execute(directory, changes=[iter(selected)])
                self.assertEqual(len(batch), 1)
                revision = batch[0]
                self.assertEqual(len(revision), 40)
                self.assertNotIn(revision, history)
                self.assertEqual(git(sim.repo, "rev-parse", f"{revision}^"), previous)
                self.assertEqual(self.history(directory), history + batch)
                changed = git(sim.repo, "diff-tree", "--no-commit-id", "--name-only",
                              "-r", revision).splitlines()
                self.assertEqual(set(changed), {f"source/{app}.txt" for app in selected})
                invalidated = set(selected)
                if "greeb" in selected:
                    invalidated.add("blerg")
                for app in APPS:
                    before = git(sim.repo, "show", f"{previous}:source/{app}.txt")
                    version = int(before.removeprefix(f"{app}-v"))
                    expected = f"{app}-v{version + (app in selected)}\n"
                    self.assertEqual((sim.repo / "source" / f"{app}.txt").read_text(), expected)
                    self.assertEqual(sim.cache_key(previous, app) == sim.cache_key(revision, app),
                                     app not in invalidated)
                builds = [event for event in events if event["kind"] == "build_finished"]
                self.assertEqual(len(builds), 3)
                self.assertEqual({event["app"] for event in builds if event["cache_hit"]},
                                 set(APPS) - invalidated)
                for app in APPS:
                    deployed = sim.deployed(app)
                    self.assertEqual(deployed["image"]["digest"], sim.image_digest(revision, app))
                    sim.validate_artifact(deployed["revision"], app, deployed["artifact"])

    def test_batches_append_history_and_replay_without_commits(self):
        sim, initial, _ = self.execute()
        sim, batch, events = self.execute(changes=[("zorch",), ("zorch", "greeb"), ()])
        self.assertEqual(len(batch), 3)
        self.assertEqual(len(set(batch)), 3)
        self.assertEqual(self.history(), initial + batch)
        self.assertEqual(git(sim.repo, "rev-list", "--count", "HEAD"), "7")
        self.assertEqual(git(sim.repo, "show", f"{batch[1]}:source/zorch.txt"), "zorch-v5")
        self.assertEqual([event["revision"] for event in events
                          if event["kind"] == "pipeline_finished"
                          and event["pool"] == "pipeline"], batch)
        manifest = (self.root / "fixture.json").read_bytes()
        sim, replay, events = self.execute(revisions=iter(batch))
        self.assertEqual(replay, batch)
        self.assertEqual((self.root / "fixture.json").read_bytes(), manifest)
        self.assertEqual(git(sim.repo, "rev-parse", "HEAD"), batch[-1])
        self.assertTrue(all(event["cache_hit"] for event in events
                            if event["kind"] == "build_finished"))
        _, default, _ = self.execute()
        self.assertEqual(default, initial + batch)

    def test_first_invocation_changes_submits_only_new_commits(self):
        sim, batch, _ = self.execute(changes=[(), ("blerg",)])
        self.assertEqual(self.history()[3:], batch)
        self.assertEqual(len(self.history()), 5)
        self.assertEqual(git(sim.repo, "show", f"{batch[-1]}:source/blerg.txt"), "blerg-v3")

    def test_empty_batch_and_empty_replay_do_not_commit_or_build(self):
        sim, initial, _ = self.execute()
        release = {app: sim.deployed(app) for app in APPS}
        manifest = (self.root / "fixture.json").read_bytes()
        for kwargs in ({"changes": []}, {"revisions": []}):
            with self.subTest(kwargs=kwargs):
                sim, batch, events = self.execute(**kwargs)
                self.assertEqual(batch, [])
                self.assertEqual(events, [])
                self.assertEqual({app: sim.deployed(app) for app in APPS}, release)
                self.assertEqual((self.root / "fixture.json").read_bytes(), manifest)
                self.assertEqual(git(sim.repo, "rev-parse", "HEAD"), initial[-1])

    def test_invalid_batch_does_not_modify_existing_fixture(self):
        sim, history, _ = self.execute()
        manifest = (self.root / "fixture.json").read_bytes()
        sources = {path: path.read_bytes() for path in (sim.repo / "source").iterdir()}
        event_log = (sim.state / "events.jsonl").read_bytes()
        invalid_batches = [
            [("zorch",), ("unknown",)],
            [("greeb",), ("blerg", "../zorch")],
            [("zorch",), (None,)],
            [("zorch",), None],
            [("zorch",), "greeb"],
        ]
        for changes in invalid_batches:
            with self.subTest(changes=changes):
                with self.assertRaises((ValueError, TypeError)):
                    self.execute(changes=changes)
                self.assertEqual(git(sim.repo, "rev-parse", "HEAD"), history[-1])
                self.assertEqual(git(sim.repo, "status", "--porcelain"), "")
                self.assertEqual((self.root / "fixture.json").read_bytes(), manifest)
                self.assertEqual((sim.state / "events.jsonl").read_bytes(), event_log)
                for path, content in sources.items():
                    self.assertEqual(path.read_bytes(), content)

    def test_invalid_fresh_batch_and_conflicting_options_create_nothing(self):
        directory = self.root / "fresh"
        for kwargs in ({"changes": [("zorch",), ("unknown",)]},
                       {"changes": [], "revisions": []}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.execute(directory, **kwargs)
            self.assertFalse(directory.exists())

    def test_add_changes_validates_and_increments_each_app_once(self):
        repo = self.root / "repo"
        original = create_history(repo)[-1]
        with self.assertRaises(ValueError):
            add_changes(repo, ["zorch", "unknown"])
        self.assertEqual(git(repo, "rev-parse", "HEAD"), original)
        self.assertEqual(git(repo, "status", "--porcelain"), "")
        revision = add_changes(repo, ["zorch", "zorch"])
        self.assertEqual(git(repo, "show", f"{revision}:source/zorch.txt"), "zorch-v4")
        other = add_changes(repo, [])
        self.assertNotEqual(revision, other)
        self.assertEqual(git(repo, "rev-parse", f"{revision}^{{tree}}"),
                         git(repo, "rev-parse", f"{other}^{{tree}}"))

    def test_concurrent_batches_retain_all_commits(self):
        sim, initial, _ = self.execute()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.execute, changes=[("zorch",), ()]) for _ in range(2)]
            batches = [future.result(timeout=20)[1] for future in futures]
        history = self.history()
        self.assertEqual(len(history), 7)
        self.assertEqual(set(history[3:]), set(batches[0] + batches[1]))
        self.assertIn(history[3:], (batches[0] + batches[1], batches[1] + batches[0]))
        self.assertEqual(git(sim.repo, "rev-list", "--count", "HEAD"), "8")
        self.assertEqual((sim.repo / "source/zorch.txt").read_text(), "zorch-v5\n")


if __name__ == "__main__":
    unittest.main()
