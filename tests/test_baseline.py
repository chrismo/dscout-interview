import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lib.exercise import execute
from lib.fixture import APPS, create_history, git


class BaselineTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def run_batch(self, **options):
        events, ready = [], []
        with patch("lib.simulation.time.sleep") as sleep:
            sim = execute(self.root, 5, observer=events.append, on_ready=ready.append,
                          reset_state=True, **options)
        self.assertEqual(events, sim.events())
        self.assertEqual({e["revision"] for e in events}, set(ready[0]))
        self.assertTrue(all(call.args[0] == 0 or call.args[0] >= 1 for call in sleep.call_args_list))
        return sim, ready[0], events

    def test_example_first_pr_changes_all_apps_after_baseline(self):
        revisions = create_history(self.root / "repo")
        self.assertEqual(len(revisions), 3)
        baseline = git(self.root / "repo", "rev-parse", f"{revisions[0]}^")
        for app in APPS:
            self.assertNotEqual(git(self.root / "repo", "show", f"{baseline}:source/{app}.txt"),
                                git(self.root / "repo", "show", f"{revisions[0]}:source/{app}.txt"))

    def test_default_example_builds_all_then_dependency_then_independent(self):
        sim, revisions, events = self.run_batch()
        for revision, changed in zip(revisions, (set(APPS), {"greeb", "blerg"}, {"zorch"})):
            self.assertEqual({e["app"] for e in events if e["revision"] == revision
                              and e["kind"] == "build_started"}, changed)
            self.assertEqual({e["app"] for e in events if e["revision"] == revision
                              and e["kind"] == "deploy_finished"}, changed)
        self.assertTrue(all(sim.deployed(app) for app in APPS))

    def test_custom_changes_only_rebuild_invalidated_apps_and_rerun_identically(self):
        for changes, changed in (([], set()), (["zorch"], {"zorch"}),
                                 (["greeb"], {"greeb", "blerg"}), (["blerg"], {"blerg"})):
            with self.subTest(changes=changes):
                sim, revisions, events = self.run_batch(changes=[changes])
                baseline = git(sim.repo, "rev-parse", f"{revisions[0]}^")
                replay, repeated, replay_events = self.run_batch(revisions=revisions)
                self.assertEqual(repeated, revisions)
                self.assertNotEqual(sim.state, replay.state)
                for result, trace in ((sim, events), (replay, replay_events)):
                    self.assertEqual({e["app"] for e in trace if e["kind"] == "build_started"}, changed)
                    self.assertEqual({e["app"] for e in trace if e["kind"] == "cache_hit"}, set(APPS) - changed)
                    self.assertEqual({e["app"] for e in trace if e["kind"] == "deploy_skipped"}, set(APPS) - changed)
                    self.assertEqual({e["app"] for e in trace if e["kind"] == "deploy_finished"}, changed)
                    for app in APPS:
                        result.validate_artifact(revisions[0], app,
                                                json.loads(result.artifact_path(revisions[0], app).read_text()))
                        self.assertEqual(result.deployed(app)["revision"],
                                         revisions[0] if app in changed else baseline)
