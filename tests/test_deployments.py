from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest

from lib.fixture import create_history
from lib.simulation import APPS, Simulation
from lib.workflow import Build, Call, Deploy, Pipeline, execute_pipeline


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


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.revisions = create_history(self.root / "repo")
        self.sim = Simulation(self.root / "repo", self.root / "state", speed=0)

    def build_all(self, revision):
        artifacts = {}
        for app in APPS:
            artifacts[app] = self.sim.build(revision, app, artifacts.get("greeb"))
        return artifacts

    def deploy_all(self, revision, artifacts):
        for app in APPS:
            self.sim.deploy(revision, app, artifacts[app], image_reference="digest")

    def test_cache_hit_does_not_mean_already_deployed(self):
        revision = self.revisions[0]
        self.build_all(revision)
        artifacts = self.build_all(revision)
        builds = [event for event in self.sim.events() if event["kind"] == "build_finished"]
        self.assertEqual(len(builds), 6)
        self.assertTrue(all(event["cache_hit"] for event in builds[-3:]))
        self.assertIsNone(self.sim.deployed("zorch"))
        self.deploy_all(revision, artifacts)
        self.assertEqual({event["app"] for event in self.sim.events()
                          if event["kind"] == "deploy_finished"}, set(APPS))

    def test_only_changed_images_roll_out_and_dependency_inputs_count(self):
        base, changed, last = self.revisions
        self.deploy_all(base, self.build_all(base))
        before = self.sim.deployed("zorch")
        self.deploy_all(changed, self.build_all(changed))
        self.assertEqual(self.sim.deployed("zorch"), before)
        self.assertEqual(self.sim.deployed("greeb")["revision"], changed)
        self.assertEqual(self.sim.deployed("blerg")["revision"], changed)
        self.deploy_all(last, self.build_all(last))
        events = self.sim.events()
        for revision, expected in ((base, set(APPS)), (changed, {"greeb", "blerg"}),
                                   (last, {"zorch"})):
            self.assertEqual({event["app"] for event in events
                              if event["revision"] == revision
                              and event["kind"] == "deploy_finished"}, expected)
            self.assertEqual({event["app"] for event in events
                              if event["revision"] == revision
                              and event["kind"] == "build_finished"}, set(APPS))

    def test_partial_failure_preserves_other_apps_and_cache_hit_retry_deploys(self):
        base, changed, _ = self.revisions
        self.deploy_all(base, self.build_all(base))
        old_blerg = self.sim.deployed("blerg")
        artifacts = self.build_all(changed)
        self.sim.fail_deploy = lambda revision, app: app == "blerg"
        with self.assertRaises(RuntimeError):
            self.deploy_all(changed, artifacts)
        self.assertEqual(self.sim.deployed("greeb")["revision"], changed)
        self.assertEqual(self.sim.deployed("blerg"), old_blerg)
        self.sim.fail_deploy = None
        self.deploy_all(changed, self.build_all(changed))
        self.assertEqual(self.sim.deployed("blerg")["revision"], changed)
        self.assertTrue(any(event["kind"] == "deploy_failed" and event["app"] == "blerg"
                            for event in self.sim.events()))
        retries = [event for event in self.sim.events()
                   if event["revision"] == changed and event["kind"] == "deploy_finished"]
        self.assertEqual([event["app"] for event in retries], ["greeb", "blerg"])

    def test_independent_deployments_do_not_lose_other_app_state(self):
        revision = self.revisions[0]
        artifacts = self.build_all(revision)
        gate = threading.Barrier(3, timeout=5)
        self.sim.observer = lambda event: gate.wait() if event["kind"] == "deploy_started" else None
        with ThreadPoolExecutor(max_workers=3) as workers:
            jobs = [workers.submit(self.sim.deploy, revision, app, artifacts[app]) for app in APPS]
            for job in jobs:
                job.result(timeout=10)
        self.assertTrue(all(self.sim.deployed(app)["revision"] == revision for app in APPS))

    def test_failed_app_does_not_block_independent_app(self):
        revision = self.revisions[0]
        self.sim.fail_deploy = lambda revision, app: app == "greeb"
        declaration = Pipeline(tasks=[
            Build("zorch"), Build("greeb"),
            Deploy("zorch-rollout", "zorch", needs=["zorch"]),
            Deploy("greeb-rollout", "greeb", needs=["greeb"]),
        ])
        with self.assertRaises(RuntimeError):
            execute_pipeline(self.sim, declaration, [revision])
        self.assertIsNotNone(self.sim.deployed("zorch"))
        self.assertIsNone(self.sim.deployed("greeb"))

    def test_starter_serializes_rollouts_without_cross_app_dependencies(self):
        declaration = starter_pipeline
        calls = {task.name: task for task in declaration.tasks
                 if task.name.startswith("deploy-")}
        for app in APPS:
            expected = (app, "deploy-greeb") if app == "blerg" else (app,)
            self.assertEqual(calls[f"deploy-{app}"].needs, expected)
            self.assertEqual(calls[f"deploy-{app}"].pipeline.pool, "deployment")
        execute_pipeline(self.sim, declaration, self.revisions)
        active = set()
        for event in self.sim.events():
            if event["kind"] == "deploy_started":
                self.assertFalse(active)
                active.add((event["revision"], event["app"]))
            elif event["kind"] == "deploy_finished":
                active.remove((event["revision"], event["app"]))
        self.assertFalse(active)

    def test_starter_failure_releases_pool_for_unrelated_apps(self):
        revision = self.revisions[0]
        self.sim.fail_deploy = lambda revision, app: app == "zorch"
        with self.assertRaises(RuntimeError):
            execute_pipeline(self.sim, starter_pipeline, [revision])
        self.assertIsNone(self.sim.deployed("zorch"))
        self.assertEqual(self.sim.deployed("greeb")["revision"], revision)
        self.assertEqual(self.sim.deployed("blerg")["revision"], revision)

    def test_greeb_rollout_failure_blocks_blerg_but_not_zorch(self):
        revision = self.revisions[0]
        self.sim.fail_deploy = lambda revision, app: app == "greeb"
        with self.assertRaises(RuntimeError):
            execute_pipeline(self.sim, starter_pipeline, [revision])
        self.assertIsNotNone(self.sim.deployed("zorch"))
        self.assertIsNone(self.sim.deployed("greeb"))
        self.assertIsNone(self.sim.deployed("blerg"))
        self.assertTrue(any(event["kind"] == "task_blocked"
                            and event["task"] == "deploy-blerg" for event in self.sim.events()))

    def test_greeb_rollout_finishes_or_skips_before_blerg(self):
        execute_pipeline(self.sim, starter_pipeline, self.revisions)
        events = self.sim.events()
        self.assertEqual({event["kind"] for event in events if event.get("app") == "greeb"
                          and event["kind"] in ("deploy_finished", "deploy_skipped")},
                         {"deploy_finished", "deploy_skipped"})
        for revision in self.revisions:
            with self.subTest(revision=revision):
                greeb_ends = [i for i, event in enumerate(events)
                              if event["revision"] == revision and event.get("app") == "greeb"
                              and event["kind"] in ("deploy_finished", "deploy_skipped")]
                blerg_requests = [i for i, event in enumerate(events)
                                  if event["revision"] == revision and event.get("app") == "blerg"
                                  and event["kind"] == "deploy_requested"]
                self.assertEqual(len(greeb_ends), 1)
                self.assertEqual(len(blerg_requests), 1)
                self.assertLess(greeb_ends[0], blerg_requests[0])

    def test_example_newest_submitted_policy_skips_both_dependent_rollouts(self):
        newest = self.revisions[-1]
        def newest_only(sim, revision, artifacts):
            return revision == newest
        declaration = Pipeline(tasks=[
            Build("greeb"), Build("blerg", needs=["greeb"]),
            Call("release", needs=["greeb", "blerg"], pipeline=Pipeline(
                pool="deployment", tasks=[
                    Deploy("deploy-greeb", "greeb", when=newest_only, image_reference="digest"),
                    Deploy("deploy-blerg", "blerg", needs=["deploy-greeb"],
                           when=newest_only, image_reference="digest"),
                ],
            )),
        ])
        execute_pipeline(self.sim, declaration, self.revisions)
        events = self.sim.events()
        for revision in self.revisions:
            with self.subTest(revision=revision):
                revision_events = [event for event in events if event["revision"] == revision]
                self.assertEqual({event["app"] for event in revision_events
                                  if event["kind"] == "build_finished"}, {"greeb", "blerg"})
                if revision != newest:
                    self.assertEqual({event["app"] for event in revision_events
                                      if event["kind"] == "task_skipped"}, {"greeb", "blerg"})
                    self.assertFalse(any(event["kind"].startswith("deploy_")
                                         for event in revision_events))
                    skips = [event["app"] for event in revision_events
                             if event["kind"] == "task_skipped"]
                    self.assertEqual(skips, ["greeb", "blerg"])
        for app in ("greeb", "blerg"):
            self.assertEqual(self.sim.deployed(app)["revision"], newest)
        greeb_ends = [i for i, event in enumerate(events)
                      if event["kind"] == "deploy_finished" and event["app"] == "greeb"]
        blerg_requests = [i for i, event in enumerate(events)
                          if event["kind"] == "deploy_requested" and event["app"] == "blerg"]
        self.assertEqual(len(greeb_ends), 1)
        self.assertEqual(len(blerg_requests), 1)
        self.assertLess(greeb_ends[0], blerg_requests[0])

    def test_simulator_does_not_impose_a_no_rollback_policy(self):
        base, changed, _ = self.revisions
        base_artifacts = self.build_all(base)
        changed_artifacts = self.build_all(changed)
        self.sim.deploy(changed, "greeb", changed_artifacts["greeb"], image_reference="digest")
        self.sim.deploy(base, "greeb", base_artifacts["greeb"], image_reference="digest")
        self.assertEqual(self.sim.deployed("greeb")["revision"], base)


if __name__ == "__main__":
    unittest.main()
