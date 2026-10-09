from pathlib import Path
import tempfile
import threading
import unittest

from lib.fixture import create_history
from lib.simulation import Simulation
from lib.workflow import Build, Call, Deploy, Pipeline, execute_pipeline, validate


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


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.commits = create_history(root / "repo")
        self.sim = Simulation(root / "repo", root / "state", speed=0)

    def test_starter_parallel_apps_serial_commits(self):
        gate = threading.Barrier(2, timeout=3)
        def observer(event):
            if event["revision"] == self.commits[0] and event["kind"] == "build_started" \
                    and event["app"] in ("zorch", "greeb"):
                gate.wait()
        self.sim.observer = observer
        execute_pipeline(self.sim, starter_pipeline, self.commits)
        releases = [e["revision"] for e in self.sim.events() if e["kind"] == "pipeline_finished"
                    and e["pool"] == "pipeline"]
        self.assertEqual(releases, self.commits)

    def test_calls_pass_commit_artifacts_and_wait_for_child(self):
        child = Pipeline(pool="child", tasks=[Build("greeb")])
        parent = Pipeline(pool="parent", tasks=[Call("component", pipeline=child),
            Deploy("inspect-inputs", "greeb", needs=["component"],
                   when=lambda sim, commit, inputs: self.check_inputs(commit, inputs))])
        execute_pipeline(self.sim, parent, self.commits[:1])
        events = self.sim.events()
        self.assertTrue(any(e["kind"] == "build_finished" and e["app"] == "greeb"
                            and e["revision"] == self.commits[0] for e in events))
        self.assertEqual(events[-1]["kind"], "pipeline_finished")
        self.assertEqual(events[-1]["pool"], "parent")
        child_end = next(i for i, e in enumerate(events)
                         if e["kind"] == "pipeline_finished" and e["pool"] == "child")
        self.assertLess(child_end, len(events) - 1)

    def check_inputs(self, commit, inputs):
        self.assertEqual(set(inputs), {"greeb"})
        self.assertEqual(inputs["greeb"]["revision"], commit)
        return False

    def test_failure_blocks_dependents_but_later_commits_complete(self):
        self.sim.fail_build = lambda commit, app: commit == self.commits[0] and app == "greeb"
        with self.assertRaisesRegex(RuntimeError, "commit pipeline"):
            execute_pipeline(self.sim, starter_pipeline, self.commits)
        blocked = {e["task"] for e in self.sim.events() if e["kind"] == "task_blocked"}
        self.assertEqual(blocked, {"blerg", "deploy-greeb", "deploy-blerg"})
        self.assertFalse(any(e["kind"] == "deploy_started" and e.get("app") == "greeb"
                             and e["revision"] == self.commits[0]
                             for e in self.sim.events()))
        self.assertTrue(any(e["kind"] == "deploy_finished" and e["revision"] == self.commits[1]
                            for e in self.sim.events()))

    def test_child_failure_propagates_and_pool_is_released(self):
        child = Pipeline(pool="child", tasks=[Build("greeb")])
        parent = Pipeline(pool="parent", tasks=[Call("child", pipeline=child)])
        self.sim.fail_build = lambda commit, app: commit == self.commits[0]
        with self.assertRaises(RuntimeError):
            execute_pipeline(self.sim, parent, self.commits[:2])
        self.assertTrue(any(e["kind"] == "build_finished" and e["revision"] == self.commits[1]
                            for e in self.sim.events()))

    def test_deploy_condition_receives_inputs_and_can_skip(self):
        observed = []
        def condition(sim, commit, artifacts):
            observed.append((commit, set(artifacts)))
            return False
        declaration = Pipeline(tasks=[Build("zorch"), Build("greeb"),
            Build("blerg", needs=["greeb"]),
            Deploy("release", "zorch", needs=["zorch", "greeb", "blerg"], when=condition)])
        execute_pipeline(self.sim, declaration, self.commits[:1])
        self.assertEqual(observed, [(self.commits[0], {"zorch", "greeb", "blerg"})])
        self.assertTrue(any(e["kind"] == "task_skipped" for e in self.sim.events()))
        self.assertFalse((self.sim.state / "deployments").exists())

    def test_deploy_mechanics_do_not_secretly_serialize(self):
        commit = self.commits[0]
        artifact = self.sim.build(commit, "zorch")
        gate = threading.Barrier(2, timeout=3)
        self.sim.observer = lambda e: gate.wait() if e["kind"] == "deploy_started" else None
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as workers:
            jobs = [workers.submit(self.sim.deploy, commit, "zorch", artifact) for _ in range(2)]
            for job in jobs:
                job.result()

    def test_invalid_graphs_fail_before_execution(self):
        invalid = [
            Pipeline(tasks=[Build("zorch"), Build("zorch")]),
            Pipeline(tasks=[Build("zorch", needs=["missing"])]),
            Pipeline(tasks=[Build("zorch", needs=["greeb"]), Build("greeb", needs=["zorch"])]),
            Pipeline(tasks=[Build("blerg")]),
            Pipeline(pool="same", tasks=[Call("child", Pipeline(pool="same"))]),
        ]
        for declaration in invalid:
            with self.subTest(declaration=declaration), self.assertRaises(ValueError):
                execute_pipeline(self.sim, declaration, self.commits)
        self.assertEqual(self.sim.events(), [])

    def test_cross_branch_pool_cycle_is_rejected(self):
        left = Pipeline(pool="a", tasks=[Call("b", Pipeline(pool="b"))])
        right = Pipeline(pool="b", tasks=[Call("a", Pipeline(pool="a"))])
        with self.assertRaisesRegex(ValueError, "cycle"):
            validate(Pipeline(tasks=[Call("left", left), Call("right", right)]))

    def test_declarations_copy_mutable_collections(self):
        dependencies = ["greeb"]
        task = Build("blerg", needs=dependencies)
        tasks = [task]
        declaration = Pipeline(tasks=tasks)
        dependencies.append("missing")
        tasks.clear()
        self.assertEqual(task.needs, ("greeb",))
        self.assertEqual(declaration.tasks, (task,))

    def test_empty_commits_and_empty_pipeline(self):
        execute_pipeline(self.sim, starter_pipeline, [])
        self.assertEqual(self.sim.events(), [])
        execute_pipeline(self.sim, Pipeline(), self.commits[:1])


if __name__ == "__main__":
    unittest.main()
