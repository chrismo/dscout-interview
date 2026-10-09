# Build concurrency exercise

## Get started

You'll need **Python 3.10+ and Git** on macOS or Linux (including WSL).
The terminal UI uses Textual, installed in a local virtual environment below.
No accounts, Docker, or cloud services. Installing dependencies needs internet
access; running the exercise does not.

Work from your fork or a copy of this repo on the
`devops-challenge/build-concurrency` branch. From the checkout's root, run:

```sh
./install
./run-tests
./run
```

Dependencies stay inside `.venv/`, not your global Python installation. No
activation is needed; delete `.venv/` to remove them. `./install` can be rerun
to update dependencies. `./run-tests` runs the test suite. `./run` uses the checkout's
virtualenv and forwards arguments to `lib/exercise.py`.

The UI opens with a welcome dialog; nothing runs until you choose.
**Run example (3 PRs)** submits the supplied three-PR scenario, or **Choose PR changes**
lets you prepare your own simulated PRs first. With a retained run directory, the
first choice becomes **Run saved PRs**, submitting its saved PR tips.

During a run, the UI shows each PR's tip SHA, builds, cache hits, and release
alongside a live event log and elapsed time. **Start** and **Finish** show seconds
from the first pipeline event in the run; **Duration** excludes initial queue
waiting and updates while the PR runs. Finish stays blank until all of that PR’s
pipelines complete (successfully or with a failure). Drag within the log to select text
(or focus it and use Shift + arrow keys), then press **Ctrl+C** or **Ctrl+Y** to copy.
These shortcuts work on macOS and Windows; macOS uses the native clipboard, so
Cmd+C is not required. Other platforms use the terminal clipboard (OSC 52), which
must be supported and enabled by your terminal. Terminal-native selection/copy
is also available by holding Shift (or your terminal's mouse-bypass modifier)
while dragging. The log is read-only; selecting text pauses its auto-scroll.
After a run, press **r** to rerun the last batch, **n** to choose new simulated
changes, or **q** to quit. Ctrl+C quits only when no log text is selected.
Rerun, new PRs, and quitting wait until the current run finishes; copying does not.

In **New simulated PRs**, check a box to simulate Git changes for that app in the
monorepo. The checked apps change together in one simulated merged PR. **Queue PR**
queues that PR and resets the checkboxes so you can prepare another.
**Queue example (3 PRs)** adds a change to all three apps, a `greeb` change,
and a `zorch` change to the queue, using the current repo.
**Run pipeline** submits the queued PRs plus any currently checked changes.
With no queued or checked changes, it submits a PR with no source changes.
Every PR still needs all three builds or valid cache hits.

A real PR may contain many commits; this fixture represents each merged PR with
one commit at its resulting tip. That SHA identifies the source snapshot passed
to its build/deploy pipeline. These commits are generated automatically and
deterministically in a disposable local repo; no Git work or actual PR merging
is required from you.

Every UI submission reloads `pipeline.py` and starts from a pre-existing environment:
the snapshot preceding the first submitted PR is already cached and deployed.
Unchanged apps get cache hits and skip rollouts; changing `greeb` also rebuilds `blerg`.
Baseline setup has no simulated delays and is excluded from the event log and PR timings.
**Rerun** replays the same PR tips from the same baseline so timing improvements
reflect your pipeline edits, not cache entries left by an earlier run. Cache reuse
also happens between PRs within a run. Imported helper modules are not automatically reloaded;
restart the UI after editing those.
The default run directory is removed on exit; `--directory` retains it.

For agents, scripts, or a noninteractive terminal, use plain output:

```sh
./run --plain
```

## The task

This little monorepo has three apps: `zorch`, `greeb`, and `blerg`.
`blerg` depends on `greeb`: its build needs that PR's `greeb` artifact, and its
rollout waits for `greeb` to deploy successfully or skip because it is already
live. `zorch` is independent. Each merged PR needs all three built, though unchanged
inputs can reuse cached builds. Each app deploys separately; there is no atomic
all-app release. Apps whose expected image is already deployed skip their rollout.
A cache hit alone does not mean an app is deployed. A successful rollout remains
live if another app fails, so the environment can contain mixed PR versions.

`pipeline.py` declares the build steps and their dependencies. The driver submits
PR tip commit IDs to that pipeline, like merged PRs arriving at CI. Independent
steps can run together, but the starter's capacity-one pool lets only one PR's
pipeline run at a time.

**Let builds for different PRs overlap, while keeping deployments one
at a time.** Make a sequence of merged PRs faster without breaking the build or
release behavior. Keep all required builds (or valid cache hits), and keep
releases local and simulated. Don't speed things up by shortening the delays,
skipping required work, or relaxing artifact checks.

We're interested in how you know your change works, not just whether it works
on the first try. Add tests that challenge your assumptions. The existing tests
check the simulation; passing them isn't enough to verify your new pipeline.

Include a short write-up:

- What did you change, and how much faster is it than the starter?
- What should happen when PRs are released, and why did you choose that behavior?
- How did you test it? What do those tests show, and what haven't you checked?
- What assumptions or tradeoffs did you make?

**AI coding agents are encouraged.** Tell us how you used them. You still own
understanding and checking the result. Ask us if a requirement is unclear.

## Submit your solution

Create your solution branch and pull request **in your own fork**, with both the
PR’s base and head repositories set to your fork. Share that PR link with us.
**Do not open a pull request against this upstream repository**: it would expose
your solution to other candidates. Check the base repository before submitting;
GitHub may default it to the upstream repo. A public fork still makes your code
public, so contact us to arrange private sharing if needed.

## Inspect a run

**Run example (3 PRs)** creates a temporary Git repo with three real commits representing
merged PR tips: changes to all three apps, a `greeb` change, then a `zorch` change.
A preceding baseline commit represents the already-built and deployed environment. Choosing
your own changes first initializes the same source history but submits only your
new simulated PR tips. Plain
mode runs the supplied history immediately. The temporary repo is cleaned up
when you exit. Nothing is pushed or changed in your checkout.

To keep the history and results, use a fresh directory outside your checkout:

```sh
RUN_DIR="$(mktemp -d)"
./run --directory "$RUN_DIR"
git -C "$RUN_DIR/repo" log --oneline --stat
# Use the release path shown in the UI event log:
python3 -m json.tool "$RUN_DIR/runs/<run-id>/deployments/zorch.json"
```

Run again with the same `--directory` to reuse the Git history. Each UI run keeps
its own results under `runs/`, each seeded from its baseline rather than earlier runs' cache entries.
Plain mode uses `state/` and retains its cache across invocations.
Remove the directory when you're done: `rm -rf "$RUN_DIR"`.

At the default delay multiplier (`--speed 5`), uncached builds take 1.5 seconds
for `zorch` and 3 seconds each for `greeb` and `blerg`; each app rollout takes 1 second.
Use `--speed 1` for a quicker run or a larger number to slow things down further.
`--speed 0` removes delays for quick checks, but doesn't prove concurrency correctness.

## Optional type checks

To check the pipeline declaration and executor types:

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/pyright
```

Lists and tuples are accepted for `tasks` and `needs`; use square brackets for
lists, not braces (which create sets). These checks don't verify concurrency.

## Where to work

**Only modify `pipeline.py` for the normal solution**, plus your own tests, write-up,
and any helper modules alongside it. **Do not modify the supplied `lib/` files or
existing tests** unless you genuinely find a bug in the exercise infrastructure
that breaks the spirit of the exercise. Commit any such bug fix separately from
your solution and explain why it is necessary.

- `pipeline.py`: the candidate-editable declaration, exported as `pipeline`.
- `lib/workflow.py`: the supplied DAG executor and pipeline-pool mechanics.
- `lib/simulation.py`: builds, caching, artifacts, events, and simulated releases.
- `lib/fixture.py`: creates the disposable Git history and simulated source changes.
- `lib/exercise.py`: the command-line driver.
- `lib/tui.py`: the live display; it observes the same pipeline, without changing its behavior.
- `lib/flow.py`: pipeline graph layout and rendering.
- `tests/`: the existing tests.

### Declaring a pipeline

Use the Python declarations in `lib/workflow.py`; no YAML or scheduler code is needed:

- `Pipeline(tasks=[...], pool=None)`: runs once per submitted PR tip commit. A named
  pool has capacity one and covers the whole invocation. No pool means no
  pipeline-level concurrency limit.
- `Build("app", needs=[...])`: builds an app after its dependencies succeed.
  `Build("blerg", needs=["greeb"])` receives that commit's `greeb` artifact.
- `Deploy("name", "app", needs=[...])`: deploys one app using its artifact
  from dependencies or inputs supplied by a calling pipeline. An already-deployed
  expected image skips the rollout. Deployment dependencies can order rollouts;
  they do not make them atomic.
- `Call("name", pipeline=another_pipeline, needs=[...])`: calls a pipeline with
  the same commit ID and artifacts from its dependencies. The call waits for
  the child to finish; the child acquires its own pool, if declared.

Names are unique within a pipeline. `needs` refers to task names in that pipeline.
Independent ready tasks run concurrently. Failed tasks block their dependents;
already-running work and independent commits finish before execution returns an
error. Successful cache entries remain available. There are no automatic retries.

Pools are FIFO within one `execute_pipeline(sim, pipeline, commits)` call. Top-level
invocations queue in submission order; called pipelines queue when invoked. Pools
are not shared across separate executions or processes. A child cannot acquire a
pool already held by its parent, and recursive calls and cyclic task dependencies
are rejected. Pool acquisition cycles across child pipelines are also rejected.
Pools belong to pipelines, not individual tasks. Build dependencies must refer to
local task names; inherited artifacts are available to deployment and callbacks,
not a replacement for declaring a local build's dependencies.

For custom release behavior, `Deploy` also accepts `when(sim, commit, artifacts)`,
a Python callback evaluated before deployment while inside the containing pipeline's
pool, if any. Returning false skips that deployment; raising fails the task.
Callbacks and helper modules can live alongside your declaration. The executor does
not supply a release policy for you.

The simulation validates artifacts and atomically writes each app's state, but does
not itself serialize deployments. Your pipeline's concurrency boundaries control
that behavior. Atomic writes aren't the same as one deployment at a time.

### Simulation API

- `sim.build(revision, app, dependency=None)` returns an artifact. When building
  `blerg`, pass that revision's `greeb` artifact as the dependency.
- `sim.deploy(revision, app, artifact, image_reference="tag")` takes one valid
  app artifact. If its expected image is already deployed, it logs `deploy_skipped`
  without rolling out. Otherwise it resolves the published image and updates only
  that app's state on success. `image_reference` accepts `"tag"` or `"digest"`;
  the same option is available on `Deploy` declarations.
- `sim.deployed(app)` returns that app's deployed record, or `None`.
- `sim.image_tag(revision, app)` returns its published image tag;
  `sim.image_digest(revision, app)` returns its expected image content digest.
- `sim.resolve_image(revision, app, reference="tag")` reads a published image.
- `sim.lock(name)` provides a shared filesystem lock across threads or local
  processes using the same state directory. Separate state directories represent
  separate environments.

Build and deploy accept commit IDs from the fixture repo. Cache keys use Git
source content and a build-version identifier. Changing `greeb` also changes
`blerg`'s cache inputs. Only successfully published entries can be reused, and
cache hits still produce artifacts associated with the requested revision.

Builds also publish images to a local file-backed registry, including on cache
hits. Images can be referenced by tag or digest. Each deployed app's record retains
its PR tip, build artifact, and resolved image. Skipped rollouts leave the previous
deployed record unchanged.

Exploring image publication and resolution is optional additional work. The
required take-home goals remain build overlap, deployment exclusion, retained
artifacts, and verification of your stated release policy.

### Output files

In a retained run directory, you'll find the files below. UI results live under
`runs/<run-id>/` instead of `state/`, with a separate directory for each run:

- `fixture.json` and `repo/`: PR tip commit IDs and Git history.
- `state/events.jsonl`: build/release events, cache keys, and cache hits.
- `state/cache/`: completed cached builds.
- `state/artifacts/<revision>/`: artifacts for each PR tip SHA. The underlying
  API and event records call this commit identity `revision`.
- `state/registry/images/`: image records indexed by immutable content digest.
- `state/registry/tags/`: published tag pointers.
- `state/deployments/<app>.json`: each app's current successful deployment,
  expected artifact, and resolved image. There is no single environment-wide revision.

### Test hooks

`Simulation` accepts these optional arguments:

- `speed`: scales the delays.
- `observer(event)`: called after an event is logged. You can use synchronization
  primitives here to control execution in tests.
- `fail_build(revision, app)`: return `True` to fail a cache miss. The build raises
  an exception without publishing its cache entry.
- `fail_deploy(revision, app)`: return `True` to fail a rollout before updating
  deployed state. Already-deployed skips do not invoke this hook.

This is a small simulation, not production CI. It doesn't compile real apps or
model deployment rollback or recovery from machine crashes.
