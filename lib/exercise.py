"""Command-line driver for a disposable local exercise."""

# Exercise infrastructure: do not modify unless fixing an exercise bug.
# Commit infrastructure fixes separately; normal solution changes belong in pipeline.py.

import argparse
import importlib.util
import json
import math
from pathlib import Path
import sys
import tempfile

from lib.fixture import APPS, add_changes, create_history, git, validate_apps
from lib.workflow import execute_pipeline, validate
from lib.simulation import Simulation, file_lock


PIPELINE_PATH = Path(__file__).resolve().parents[1] / "pipeline.py"


def load_pipeline():
    """Evaluate pipeline.py from source; imported helper modules are not reloaded."""
    path = PIPELINE_PATH.resolve()
    code = compile(path.read_bytes(), str(path), "exec")
    spec = importlib.util.spec_from_file_location("pipeline", path)
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get("pipeline")
    sys.modules["pipeline"] = module
    try:
        exec(code, module.__dict__)
        return module.pipeline
    except BaseException:
        if previous is None:
            sys.modules.pop("pipeline", None)
        else:
            sys.modules["pipeline"] = previous
        raise


def execute(directory, speed, *, observer=None, on_ready=None, changes=None,
            revisions=None, reset_state=False):
    if changes is not None and revisions is not None:
        raise ValueError("changes and revisions are mutually exclusive")
    if changes is not None:
        changes = [validate_apps(apps) for apps in changes]
    if revisions is not None:
        revisions = list(revisions)
    pipeline = load_pipeline()
    validate(pipeline)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "fixture.json"
    with file_lock(directory / "fixture.lock"):
        if manifest.exists():
            history = json.loads(manifest.read_text())
        else:
            history = {"revisions": create_history(directory / "repo")}
            manifest.write_text(json.dumps(history, indent=2) + "\n")
        if changes is not None:
            revisions = []
            for apps in changes:
                revision = add_changes(directory / "repo", apps)
                history["revisions"].append(revision)
                manifest.write_text(json.dumps(history, indent=2) + "\n")
                revisions.append(revision)
        elif revisions is None:
            revisions = list(history["revisions"])
    if on_ready is not None:
        on_ready(revisions)
    if observer is None:
        print(f"Fixture: {directory}", flush=True)
        for revision in revisions:
            print(f"PR tip: {revision}", flush=True)

    def show(event):
        app = event.get("app", event.get("task", ""))
        hit = f" cache_hit={event['cache_hit']}" if "cache_hit" in event else ""
        print(f"{event['revision'][:12]} {event['kind']} {app}{hit}", flush=True)

    state = directory / "state"
    if reset_state:
        runs = directory / "runs"
        runs.mkdir(exist_ok=True)
        state = Path(tempfile.mkdtemp(prefix="run-", dir=runs))
    if revisions:
        with file_lock(state / "locks" / "initialize.lock"):
            if not (state / "events.jsonl").exists():
                parents = git(directory / "repo", "rev-list", "--parents", "-n", "1",
                              revisions[0]).split()
                baseline = parents[1] if len(parents) > 1 else parents[0]
                existing = Simulation(directory / "repo", state, speed=0)
                artifacts = {}
                for app in APPS:
                    artifacts[app] = existing.build(baseline, app, artifacts.get("greeb"))
                    existing.deploy(baseline, app, artifacts[app], image_reference="digest")
                (state / "events.jsonl").unlink()
    sim = Simulation(directory / "repo", state, speed=speed,
                     observer=show if observer is None else observer)
    execute_pipeline(sim, pipeline, revisions)
    if observer is None:
        print(f"Deployments: {sim.state / 'deployments'}", flush=True)
    return sim


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path,
                        help="retain fixture and state here; use a directory outside the checkout")
    parser.add_argument("--speed", type=float, default=5, help="delay multiplier (default: 5)")
    parser.add_argument("--plain", action="store_true", help="run without the terminal UI")
    args = parser.parse_args()
    if not math.isfinite(args.speed) or args.speed < 0:
        parser.error("--speed must be finite and nonnegative")

    if args.plain:
        launch = execute
    else:
        try:
            from lib.tui import BuildApp
        except ModuleNotFoundError as error:
            if error.name != "textual" and not (error.name or "").startswith("textual."):
                raise
            parser.error("Textual is required for the terminal UI. Install locally with "
                         "`python3 -m venv .venv` and "
                         "`.venv/bin/python -m pip install -r requirements.txt`, then run "
                         "`./run`; or use --plain.")

        def launch(directory, speed):
            app = BuildApp(directory, speed)
            app.run()
            if app.failed or app.return_code:
                raise SystemExit(app.return_code or 1)

    if args.directory:
        launch(args.directory.resolve(), args.speed)
    else:
        with tempfile.TemporaryDirectory(prefix="build-exercise-") as directory:
            launch(Path(directory), args.speed)


if __name__ == "__main__":
    main()
