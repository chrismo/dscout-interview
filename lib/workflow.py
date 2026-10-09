"""Small declarative CI executor. Pools belong to pipelines, not tasks."""

# Exercise infrastructure: do not modify unless fixing an exercise bug.
# Commit infrastructure fixes separately; normal solution changes belong in pipeline.py.

from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass
from typing import Callable, Sequence
import threading

from lib.simulation import Simulation


@dataclass(frozen=True)
class Build:
    name: str
    needs: Sequence[str] = ()

    def __post_init__(self):
        object.__setattr__(self, "needs", tuple(self.needs))


@dataclass(frozen=True)
class Deploy:
    name: str
    app: str
    needs: Sequence[str] = ()
    when: Callable[[Simulation, str, dict], bool] | None = None
    image_reference: str = "tag"

    def __post_init__(self):
        object.__setattr__(self, "needs", tuple(self.needs))


@dataclass(frozen=True)
class Call:
    name: str
    pipeline: "Pipeline"
    needs: Sequence[str] = ()

    def __post_init__(self):
        object.__setattr__(self, "needs", tuple(self.needs))


@dataclass(frozen=True)
class Pipeline:
    tasks: Sequence[Build | Deploy | Call] = ()
    pool: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "tasks", tuple(self.tasks))


def validate(pipeline, ancestors=(), pools=(), pool_edges=None):
    root = pool_edges is None
    pool_edges = {} if root else pool_edges
    if not isinstance(pipeline, Pipeline):
        raise ValueError("expected a Pipeline")
    if id(pipeline) in ancestors:
        raise ValueError("recursive pipeline calls are not supported")
    if pipeline.pool and pipeline.pool in pools:
        raise ValueError("a child cannot acquire a pool held by its parent")
    if pipeline.pool:
        if not isinstance(pipeline.pool, str):
            raise ValueError("pool names must be strings")
        for held in pools:
            pool_edges.setdefault(held, set()).add(pipeline.pool)
    if any(not isinstance(task, (Build, Deploy, Call)) for task in pipeline.tasks):
        raise ValueError("unsupported task type")
    names = [task.name for task in pipeline.tasks]
    if len(names) != len(set(names)):
        raise ValueError("task names must be unique within a pipeline")
    remaining = set(names)
    done = set()
    for task in pipeline.tasks:
        if not isinstance(task, (Build, Deploy, Call)):
            raise ValueError("unsupported task type")
        if not task.name or not set(task.needs) <= remaining:
            raise ValueError(f"invalid dependencies for {task.name}")
        if isinstance(task, Build):
            if task.name not in ("zorch", "greeb", "blerg"):
                raise ValueError(f"unknown app: {task.name}")
            if task.name == "blerg" and "greeb" not in task.needs:
                raise ValueError("blerg must depend on greeb")
        if isinstance(task, Deploy):
            if task.app not in ("zorch", "greeb", "blerg"):
                raise ValueError(f"unknown app: {task.app}")
            if task.image_reference not in ("tag", "digest"):
                raise ValueError("image_reference must be tag or digest")
        if isinstance(task, Call):
            validate(task.pipeline, ancestors + (id(pipeline),),
                     pools + ((pipeline.pool,) if pipeline.pool else ()), pool_edges)
    while remaining:
        ready = {task.name for task in pipeline.tasks
                 if task.name in remaining and set(task.needs) <= done}
        if not ready:
            raise ValueError("task dependencies contain a cycle")
        done.update(ready)
        remaining.difference_update(ready)
    if root:
        def visit(name, path):
            if name in path:
                raise ValueError("pipeline pool acquisitions contain a cycle")
            for child in pool_edges.get(name, ()):
                visit(child, path | {name})
        for name in pool_edges:
            visit(name, set())


class Pool:
    def __init__(self):
        self.condition = threading.Condition()
        self.queue = []

    def reserve(self):
        ticket = object()
        with self.condition:
            self.queue.append(ticket)
        return ticket

    def acquire(self, ticket):
        with self.condition:
            self.condition.wait_for(lambda: self.queue[0] is ticket)

    def release(self, ticket):
        with self.condition:
            self.queue.remove(ticket)
            self.condition.notify_all()


class Executor:
    def __init__(self, sim):
        self.sim = sim
        self.pools = {}
        self.guard = threading.Lock()

    def reserve(self, pipeline):
        if not pipeline.pool:
            return None
        with self.guard:
            pool = self.pools.setdefault(pipeline.pool, Pool())
            return pool, pool.reserve()

    def invoke(self, pipeline, commit, inputs=None, lease=None):
        lease = lease if lease is not None else self.reserve(pipeline)
        try:
            if lease:
                self.sim.event("pipeline_waiting", commit, pool=pipeline.pool)
                lease[0].acquire(lease[1])
            self.sim.event("pipeline_started", commit, pool=pipeline.pool)
            result = self.tasks(pipeline, commit, inputs or {})
            self.sim.event("pipeline_finished", commit, pool=pipeline.pool)
            return result
        except BaseException:
            try:
                self.sim.event("pipeline_failed", commit, pool=pipeline.pool)
            except Exception:
                pass
            raise
        finally:
            if lease:
                lease[0].release(lease[1])

    def task(self, task, commit, artifacts):
        if isinstance(task, Build):
            result = self.sim.build(commit, task.name, dependency=artifacts.get("greeb"))
            return {task.name: result}
        if isinstance(task, Deploy):
            if task.when is not None and not task.when(self.sim, commit, artifacts):
                self.sim.event("task_skipped", commit, task=task.name, app=task.app)
                return {}
            self.sim.deploy(commit, task.app, artifacts.get(task.app),
                            image_reference=task.image_reference)
            return {}
        return self.invoke(task.pipeline, commit, artifacts)

    def tasks(self, pipeline, commit, inputs):
        pending = {task.name: task for task in pipeline.tasks}
        results = {}
        failed = set()
        running = {}
        errors = []
        with ThreadPoolExecutor(max_workers=max(1, len(pending))) as workers:
            while pending or running:
                for name, task in list(pending.items()):
                    if set(task.needs) & failed:
                        failed.add(name)
                        del pending[name]
                        self.sim.event("task_blocked", commit, task=name,
                                       **({"app": task.app} if isinstance(task, Deploy) else {}))
                    elif set(task.needs) <= results.keys():
                        artifacts = dict(inputs)
                        for dependency in task.needs:
                            artifacts.update(results[dependency])
                        running[workers.submit(self.task, task, commit, artifacts)] = name
                        del pending[name]
                if not running:
                    if pending and not any(set(task.needs) & failed for task in pending.values()):
                        raise RuntimeError("pipeline has pending tasks but cannot make progress")
                    continue
                completed, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in completed:
                    name = running.pop(future)
                    try:
                        results[name] = future.result()
                    except BaseException as error:
                        failed.add(name)
                        errors.append(error)
        if errors:
            raise RuntimeError(f"pipeline failed for {commit}: {errors[0]}") from errors[0]
        artifacts = dict(inputs)
        for result in results.values():
            artifacts.update(result)
        return artifacts


def execute_pipeline(sim, pipeline, commits):
    """Wait for all invocations, including independent work after a failure.

    Pools are FIFO within this execution, not shared between separate processes
    or execute_pipeline calls. Child requests enqueue when their Call runs.
    """
    validate(pipeline)
    commits = [sim.resolve(commit) for commit in commits]
    executor = Executor(sim)
    leases = [executor.reserve(pipeline) for _ in commits]
    errors = []
    with ThreadPoolExecutor(max_workers=max(1, len(commits))) as workers:
        jobs = [workers.submit(executor.invoke, pipeline, commit, lease=lease)
                for commit, lease in zip(commits, leases)]
        for job in jobs:
            try:
                job.result()
            except BaseException as error:
                errors.append(error)
    if errors:
        raise RuntimeError(f"{len(errors)} commit pipeline(s) failed") from errors[0]
