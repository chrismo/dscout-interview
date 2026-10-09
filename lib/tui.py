"""Live display for the local build exercise."""

# Exercise infrastructure: do not modify unless fixing an exercise bug.
# Commit infrastructure fixes separately; normal solution changes belong in pipeline.py.

import asyncio
import subprocess
import sys
from pathlib import Path
from threading import Thread
from time import monotonic

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, DataTable, Footer, Static, TextArea

from lib.exercise import execute, load_pipeline
from lib.flow import live_flow
from lib.simulation import APPS


class EventLog(TextArea):
    BINDINGS = [Binding("ctrl+y", "copy", "Copy")]

    async def action_copy(self) -> None:
        text = self.selected_text
        if not text:
            return
        self.app.copy_to_clipboard(text)
        command = (["pbcopy"] if sys.platform == "darwin" else
                   ["clip.exe"] if sys.platform == "win32" else None)
        if command and not self.app.is_headless:
            data = text.encode("utf-16le" if sys.platform == "win32" else "utf-8")
            try:
                await asyncio.to_thread(
                    subprocess.run, command, input=data, check=True,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
                )
            except (OSError, subprocess.SubprocessError):
                self.app.notify("Native clipboard unavailable; tried terminal clipboard instead.",
                                severity="warning")

    def __init__(self, **kwargs):
        super().__init__(read_only=True, **kwargs)

    def write(self, message):
        follow = self.scroll_y >= self.max_scroll_y and not self.selected_text
        self.insert(f"{message}\n", self.document.end)
        if follow:
            self.scroll_end(animate=False)


class Welcome(ModalScreen):
    CSS = """
    Welcome { align: center middle; }
    #welcome-dialog {
        width: 90%; min-width: 40; max-width: 76; height: auto;
        max-height: 100%; border: thick $accent; padding: 1 2;
    }
    #welcome-title { text-style: bold; margin-bottom: 1; }
    #welcome-content { height: auto; max-height: 14; }
    #welcome-actions { height: 3; margin-top: 1; }
    #welcome-actions Button { min-width: 10; margin-right: 1; }
    """
    BINDINGS = [Binding("escape,q", "quit", "Quit")]

    def __init__(self, existing_history: bool):
        super().__init__()
        self.existing_history = existing_history

    def compose(self) -> ComposeResult:
        with Vertical(id="welcome-dialog"):
            yield Static("Build concurrency exercise", id="welcome-title")
            with VerticalScroll(id="welcome-content"):
                yield Static(
                    "A monorepo with three apps: zorch, greeb, and blerg.\n"
                    "Merged PRs trigger builds and deployments, like CI.\n\n"
                    "Your task: edit the declaration in pipeline.py so builds across\n"
                    "PRs overlap, while keeping deployments one at a time.\n\n"
                    "Each simulated PR is represented by one tip commit;\n"
                    "no real Git work is required. No PRs are actually merged.\n"
                    "Releases are local and simulated: no actual deployment,\n"
                    "and nothing changes in your checkout.\n\n"
                    + ("Run saved PRs submits the saved simulated PR history."
                       if self.existing_history else
                       "The example submits 3 PRs: changes to all apps, a greeb change,\n"
                       "then a zorch change.")
                )
            with Horizontal(id="welcome-actions"):
                yield Button("Run saved PRs" if self.existing_history else "Run example (3 PRs)",
                             variant="primary", id="run-example")
                yield Button("Choose PR changes", id="choose-changes")
                yield Button("Quit", id="welcome-quit")

    def on_mount(self) -> None:
        self.query_one("#run-example", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "welcome-quit":
            self.action_quit()
        else:
            self.dismiss(event.button.id)

    def action_quit(self) -> None:
        self.app.action_safe_quit()


class SourceCheckbox(Checkbox):
    @property
    def BUTTON_INNER(self):
        return "X" if self.value else " "


class NewChanges(ModalScreen):
    CSS = """
    NewChanges { align: center middle; }
    #changes-dialog {
        width: 72; height: auto; max-height: 100%; overflow-y: auto;
        border: thick $accent; padding: 1 2;
    }
    #changes-title { text-style: bold; margin-bottom: 1; }
    #selection-summary { height: 1; }
    #batch-summary { height: auto; max-height: 6; overflow-y: auto; }
    #changes-actions { height: 3; }
    #changes-actions Button { min-width: 14; margin-right: 1; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self):
        super().__init__()
        self.batch = []

    def compose(self) -> ComposeResult:
        with Vertical(id="changes-dialog"):
            yield Static("New simulated PRs", id="changes-title")
            yield Static("Check a box to include changes for that app in a simulated PR.\n"
                         "The selected apps will change together in one PR.\n"
                         "Each simulated PR is represented by one tip commit;\n"
                         "no real Git work is required. No PRs are actually merged.\n\n"
                         "Queue PR lets you prepare another PR before running.\n"
                         "Run pipeline simulates merged PRs triggering builds and deployments\n"
                         "for queued PRs and any checked changes.")
            for app in APPS:
                yield SourceCheckbox(app, id=f"change-{app}")
            yield Static(id="selection-summary", markup=False)
            yield Button("Queue example (3 PRs)", id="queue-example")
            yield Static("No PRs queued", id="batch-summary", markup=False)
            with Horizontal(id="changes-actions"):
                yield Button("Queue PR", id="queue-commit")
                yield Button("Run pipeline", variant="primary", id="run-pipeline")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self._update_selection()

    def _selected(self):
        return [app for app in APPS if self.query_one(f"#change-{app}", Checkbox).value]

    def _update_selection(self) -> None:
        selected = self._selected()
        self.query_one("#selection-summary", Static).update(
            f"Next PR: {', '.join(selected) or 'no source changes'}"
        )

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        self._update_selection()

    def _add_selection(self) -> None:
        self.batch.append(self._selected())
        for checkbox in self.query(Checkbox):
            checkbox.value = False
        self._update_selection()
        self._update_queue()

    def _update_queue(self) -> None:
        self.query_one("#batch-summary", Static).update(
            "Queued PRs:\n" + "\n".join(
                f"{index}. {', '.join(change) or 'no source changes'}"
                for index, change in enumerate(self.batch, 1)
            )
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "queue-commit":
            self._add_selection()
        elif event.button.id == "queue-example":
            self.batch.extend([list(APPS), ["greeb"], ["zorch"]])
            self._update_queue()
        elif event.button.id == "run-pipeline":
            if self._selected() or not self.batch:
                self._add_selection()
            self.dismiss(self.batch)
        elif event.button.id == "cancel":
            self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)


class BuildApp(App):
    CSS = """
    #status { height: 2; }
    #path { height: 2; }
    #revisions { height: 8; }
    #details { height: 1fr; }
    #events { width: 1fr; height: 100%; border: round $accent; }
    #flow-pane { width: 1fr; height: 100%; border: round $accent; }
    #flow { height: auto; padding: 1; }
    """
    BINDINGS = [
        Binding("r", "rerun", "Rerun"),
        Binding("n", "new_changes", "New PRs"),
        Binding("q", "safe_quit", "Quit", priority=True),
        Binding("ctrl+c", "copy_or_quit", "Copy or quit", show=False, priority=True),
    ]

    def __init__(self, directory: Path, speed: float):
        super().__init__()
        self.directory = Path(directory)
        self.speed = speed
        self.failed = False
        self.running = False
        self.simulation = None
        self.states = {}
        self._revision_times = {}
        self._execution_starts = {}
        self._execution_finishes = {}
        self._active_pipelines = {}
        self._run_event_start = None
        self._last_revisions = None
        self._thread = None
        self._error = None
        self._started = None
        self._ended = None
        self._selected_commit = None
        self._flow_pipeline = None

    def compose(self) -> ComposeResult:
        self._status = Static("Ready to run", id="status", markup=False)
        yield self._status
        yield Static(str(self.directory), id="path", markup=False)
        yield DataTable(id="revisions")
        with Horizontal(id="details"):
            yield EventLog(id="events")
            with VerticalScroll(id="flow-pane"):
                yield Static("Run a pipeline to see its flow", id="flow", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        for column in ("Revision", *APPS, *(f"deploy-{app}" for app in APPS),
                       "release", "Start", "Finish", "Duration"):
            table.add_column("PR (tip SHA)" if column == "Revision" else column, key=column)
        table.cursor_type = "row"
        self.query_one("#events", EventLog).border_title = "Event log"
        self.query_one("#flow-pane").border_title = "Flow · select a PR above"
        self._status_timer = self.set_interval(0.1, self._update_status)
        self._show_welcome()

    def _show_welcome(self) -> None:
        self.push_screen(Welcome((self.directory / "fixture.json").exists()), self._welcome_chosen)

    def _welcome_chosen(self, choice) -> None:
        if choice == "run-example":
            self._start()
        elif choice == "choose-changes":
            self.action_new_changes()

    def _start(self, changes=None) -> None:
        self.running = True
        self.failed = False
        self.simulation = None
        self._error = None
        self._started = monotonic()
        self._ended = None
        self.states.clear()
        self._selected_commit = None
        try:
            self._flow_pipeline = load_pipeline()
        except Exception:
            self._flow_pipeline = None
        self._revision_times.clear()
        self._execution_starts.clear()
        self._execution_finishes.clear()
        self._active_pipelines.clear()
        self._run_event_start = None
        self.query_one(DataTable).clear()
        self.query_one(EventLog).clear()
        self.query_one("#path", Static).update(f"Run directory: {self.directory}")
        self._update_status()
        options = {}
        if changes is not None:
            options["changes"] = changes
        elif self._last_revisions is not None:
            options["revisions"] = self._last_revisions
        self._thread = Thread(target=self._run, kwargs=options)
        self._thread.start()

    def _run(self, **options) -> None:
        try:
            simulation = execute(
                self.directory,
                self.speed,
                reset_state=True,
                observer=lambda event: self.call_later(self._observe, event),
                on_ready=lambda revisions: self.call_later(self._revisions_ready, revisions),
                **options,
            )
            self.call_later(self._result, simulation)
        except Exception as error:
            self.call_later(self._record_error, error)
        finally:
            self.call_later(self._finish)

    def _revisions_ready(self, revisions) -> None:
        self._last_revisions = list(revisions)
        table = self.query_one(DataTable)
        for revision in self._last_revisions:
            self.states[revision] = dict.fromkeys((*APPS, *(f"deploy-{app}" for app in APPS), "release"), "pending")
            table.add_row(revision[:12], *(Text("pending", style="dim") for _ in range(7)),
                          "—", "—", "—", key=revision)
        self._selected_commit = self._last_revisions[0] if self._last_revisions else None
        self._refresh_flow()
        self.query_one(EventLog).write(f"PRs ready: {len(self._last_revisions)}")

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._selected_commit = str(event.row_key.value)
        self._refresh_flow()

    def _refresh_flow(self) -> None:
        if self._flow_pipeline is not None:
            try:
                content = live_flow(self._flow_pipeline,
                                    self.states.get(self._selected_commit, {}),
                                    self._selected_commit)
            except ValueError as error:
                content = Text(str(error), style="red")
            self.query_one("#flow", Static).update(content)

    def _observe(self, event) -> None:
        revision, kind = event["revision"], event["kind"]
        if "time" in event:
            started, ended = self._revision_times.get(revision, (event["time"], event["time"]))
            started, ended = min(started, event["time"]), max(ended, event["time"])
            self._revision_times[revision] = started, ended
            self._run_event_start = (started if self._run_event_start is None
                                     else min(self._run_event_start, started))
            if kind == "pipeline_waiting" or (kind == "pipeline_started" and not event.get("pool")):
                self._active_pipelines[revision] = self._active_pipelines.get(revision, 0) + 1
            if kind == "pipeline_started":
                previous = self._execution_starts.get(revision, event["time"])
                self._execution_starts[revision] = min(previous, event["time"])
            if kind in ("pipeline_finished", "pipeline_failed"):
                active = max(0, self._active_pipelines.get(revision, 0) - 1)
                self._active_pipelines[revision] = active
                if active == 0:
                    self._execution_finishes[revision] = ended
            execution_start = self._execution_starts.get(revision)
            table = self.query_one(DataTable)
            if execution_start is not None:
                table.update_cell(revision, "Duration", f"{ended - execution_start:.2f}s",
                                  update_width=True)
            for commit, start in self._execution_starts.items():
                table.update_cell(commit, "Start", f"{start - self._run_event_start:.2f}s",
                                  update_width=True)
            for commit, finish in self._execution_finishes.items():
                table.update_cell(commit, "Finish", f"{finish - self._run_event_start:.2f}s",
                                  update_width=True)
        column = event.get("app", "release")
        if kind.startswith("deploy_") and "app" in event:
            column = f"deploy-{event['app']}"
        if kind in ("task_blocked", "task_skipped"):
            task = event["task"]
            column = (f"deploy-{event['app']}" if "app" in event
                      else "release" if task == "deploy" else task)
        status = {
            "build_requested": "waiting (cache lock)",
            "build_started": "building",
            "cache_hit": "cache hit",
            "build_finished": "finished (cache hit)" if event.get("cache_hit") else "finished",
            "build_failed": "failed",
            "deploy_requested": "ready",
            "deploy_started": "deploying",
            "deploy_finished": "finished",
            "deploy_failed": "failed",
            "deploy_skipped": "skipped",
            "task_blocked": "blocked",
            "task_skipped": "skipped",
        }.get(kind)
        if status is not None and column in self.states[revision]:
            self.states[revision][column] = status
            style = ("bold red" if status in ("failed", "blocked") else "cyan" if "cache hit" in status
                     else "green" if status == "finished" else "yellow")
            label = {"waiting (cache lock)": "waiting",
                     "finished (cache hit)": "cached"}.get(status, status)
            self.query_one(DataTable).update_cell(revision, column, Text(label, style=style),
                                                  update_width=True)
        deployment_states = [self.states[revision][f"deploy-{app}"] for app in APPS]
        if "app" in event and (kind.startswith("deploy_")
                               or kind in ("task_skipped", "task_blocked")):
            summary = ("failed" if any(value in ("failed", "blocked")
                                      for value in deployment_states) else
                       "finished" if all(value in ("finished", "skipped")
                                         for value in deployment_states) else "deploying")
            self.states[revision]["release"] = summary
            self.query_one(DataTable).update_cell(revision, "release", summary)
        if revision == self._selected_commit:
            self._refresh_flow()
            terminal = {"finished", "finished (cache hit)", "failed", "blocked", "skipped"}
            if all(state in terminal for state in self.states[revision].values()):
                for next_commit in self._last_revisions or []:
                    if next_commit != revision and not all(
                            state in terminal for state in self.states[next_commit].values()):
                        self._selected_commit = next_commit
                        row = self._last_revisions.index(next_commit)
                        self.query_one(DataTable).move_cursor(row=row)
                        self._refresh_flow()
                        break
        if kind.endswith("_failed"):
            self.failed = True
        hit = f" cache_hit={event['cache_hit']}" if "cache_hit" in event else ""
        task = event.get("app", event.get("task", ""))
        self.query_one(EventLog).write(f"{revision[:12]} {kind} {task}{hit}")

    def _result(self, simulation) -> None:
        self.simulation = simulation

    def _record_error(self, error) -> None:
        self.failed = True
        self._error = f"{type(error).__name__}: {error}"
        self.query_one(EventLog).write(f"FAILED: {self._error}")

    async def _finish(self) -> None:
        await asyncio.to_thread(self._thread.join)
        self.running = False
        self._ended = monotonic()
        if not self.failed:
            self.query_one(EventLog).write(f"Finished. Deployments: {self.simulation.state / 'deployments'}")
        self._update_status()

    def _update_status(self) -> None:
        if self._started is None:
            return
        elapsed = (self._ended or monotonic()) - self._started
        state = "Running" if self.running else "FAILED" if self.failed else "Finished"
        detail = self._error or ("Wait for completion before rerunning or quitting." if self.running
                                 else "r: rerun · n: new PRs · q: quit")
        self._status.update(f"{state} · {elapsed:.2f}s\n{detail}")

    def _busy(self) -> bool:
        if not self.running:
            return False
        self.query_one(EventLog).write("Still running: must finish before rerunning or quitting.")
        return True

    def action_rerun(self) -> None:
        if self._started is not None and not isinstance(self.screen, ModalScreen) and not self._busy():
            self._start()

    def action_new_changes(self) -> None:
        if isinstance(self.screen, ModalScreen) or self._busy():
            return
        self.push_screen(NewChanges(), self._changes_chosen)

    def _changes_chosen(self, changes) -> None:
        if changes is not None:
            self._start(changes=changes)
        elif self._started is None:
            self._show_welcome()

    async def action_copy_or_quit(self) -> None:
        if isinstance(self.focused, EventLog) and self.focused.selected_text:
            await self.focused.action_copy()
        else:
            self.action_safe_quit()

    def action_safe_quit(self) -> None:
        if not self._busy():
            self.exit()

    def action_quit(self) -> None:
        self.action_safe_quit()

    async def on_unmount(self) -> None:
        self._status_timer.stop()
        if self._thread is not None:
            await asyncio.to_thread(self._thread.join)
