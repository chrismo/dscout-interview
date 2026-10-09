import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch

from textual.widgets import Button, Checkbox, DataTable, Static

from lib.exercise import execute
from lib.fixture import git
from lib.simulation import APPS
from lib.tui import BuildApp, EventLog, NewChanges, SourceCheckbox, Welcome
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


class TuiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        pipeline_patch = patch("lib.exercise.load_pipeline", return_value=starter_pipeline)
        self.pipeline_patch = pipeline_patch
        pipeline_patch.start()
        self.addCleanup(pipeline_patch.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "fixture"

    async def finished(self, app, pilot):
        for _ in range(200):
            if not app.running:
                return
            await pilot.pause(0.025)
        self.fail("UI worker did not finish")

    async def test_welcome_waits_for_choice_and_primary_is_visible(self):
        app = BuildApp(self.directory, 0)
        with patch("lib.tui.execute") as mocked:
            async with app.run_test(size=(80, 24)) as pilot:
                self.assertIsInstance(app.screen, Welcome)
                button = app.screen.query_one("#run-example", Button)
                self.assertEqual(button.label.plain, "Run example (3 PRs)")
                self.assertIs(app.focused, button)
                self.assertTrue(button.visible)
                self.assertGreater(button.region.width, 0)
                self.assertLessEqual(button.region.bottom, 24)
                instructions = "\n".join(str(widget.render()) for widget in app.screen.query(Static))
                for text in ("zorch, greeb, and blerg", "like CI", "pipeline.py",
                             "Merged PRs trigger builds and deployments", "PRs overlap",
                             "deployments one at a time", "one tip commit",
                             "no real Git work is required", "No PRs are actually merged",
                             "local and simulated", "no actual deployment",
                             "nothing changes in your checkout", "changes to all apps",
                             "greeb change", "zorch change"):
                    self.assertIn(text, instructions)
                self.assertNotIn("depends", instructions)
                self.assertNotIn("invalidat", instructions)
                await pilot.press("r", "n")
                app.action_rerun()
                app.action_new_changes()
                self.assertIsInstance(app.screen, Welcome)
                self.assertIn("Ready to run", str(app._status.render()))
                self.assertFalse(app.running)
                self.assertIsNone(app._thread)
                mocked.assert_not_called()
                self.assertFalse(self.directory.exists())

    async def test_choose_changes_first_submits_only_selected_revisions(self):
        app = BuildApp(self.directory, 0)
        with patch("lib.tui.execute", wraps=execute) as mocked:
            async with app.run_test(size=(120, 35)) as pilot:
                await pilot.click("#choose-changes")
                self.assertIsInstance(app.screen, NewChanges)
                mocked.assert_not_called()
                self.assertFalse(self.directory.exists())
                await pilot.click("#change-blerg")
                await pilot.click("#queue-commit")
                await pilot.click("#change-zorch")
                await pilot.click("#run-pipeline")
                await self.finished(app, pilot)
                self.assertFalse(app.failed)
                self.assertNotIsInstance(app.screen, Welcome)
                self.assertEqual(mocked.call_count, 1)
                self.assertEqual(mocked.call_args.kwargs["changes"], [["blerg"], ["zorch"]])
                self.assertEqual(len(app._last_revisions), 2)
                executed = {event["revision"] for event in app.simulation.events()}
                self.assertEqual(executed, set(app._last_revisions))

    async def test_cancel_first_changes_returns_welcome_without_work(self):
        app = BuildApp(self.directory, 0)
        with patch("lib.tui.execute") as mocked:
            async with app.run_test(size=(120, 35)) as pilot:
                for cancel in ("button", "escape"):
                    await pilot.click("#choose-changes")
                    await pilot.click("#change-greeb")
                    await pilot.click("#queue-commit")
                    await pilot.click("#change-zorch")
                    await pilot.click("#queue-example")
                    if cancel == "button":
                        await pilot.click("#cancel")
                    else:
                        await pilot.press("escape")
                    self.assertIsInstance(app.screen, Welcome)
                    self.assertEqual(app.focused.id, "run-example")
                    self.assertFalse(self.directory.exists())
                    mocked.assert_not_called()
                    self.assertIsNone(app._thread)

    async def test_welcome_quit_does_no_work(self):
        for quit_choice in ("button", "q", "escape", "ctrl+c"):
            with self.subTest(quit_choice=quit_choice), patch("lib.tui.execute") as mocked:
                app = BuildApp(self.directory, 0)
                async with app.run_test() as pilot:
                    if quit_choice == "button":
                        await pilot.click("#welcome-quit")
                    else:
                        await pilot.press(quit_choice)
                    self.assertFalse(app.is_running)
                mocked.assert_not_called()
                self.assertFalse(self.directory.exists())
                self.assertIsNone(app._thread)

    async def test_retained_history_label_and_reuse(self):
        await asyncio.to_thread(execute, self.directory, 0, changes=[["zorch"]],
                                observer=lambda event: None, on_ready=lambda revisions: None)
        manifest = (self.directory / "fixture.json").read_bytes()
        app = BuildApp(self.directory, 0)
        with patch("lib.tui.execute", wraps=execute) as mocked:
            async with app.run_test() as pilot:
                self.assertIsInstance(app.screen, Welcome)
                button = app.screen.query_one("#run-example", Button)
                self.assertEqual(button.label.plain, "Run saved PRs")
                instructions = "\n".join(str(widget.render()) for widget in app.screen.query(Static))
                self.assertIn("saved simulated PR history", instructions)
                self.assertNotIn("completed cache", instructions)
                self.assertNotIn("changes to all apps", instructions)
                mocked.assert_not_called()
                await pilot.click("#run-example")
                await self.finished(app, pilot)
                self.assertFalse(app.failed)
                self.assertEqual(len(app._last_revisions), 4)
                self.assertEqual((self.directory / "fixture.json").read_bytes(), manifest)

    async def test_completion_and_fresh_rerun(self):
        app = BuildApp(self.directory, 0)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.press("enter")
            await self.finished(app, pilot)
            self.assertFalse(app.failed)
            self.assertIsNotNone(app.simulation)
            table = app.query_one(DataTable)
            self.assertEqual(table.row_count, 3)
            self.assertEqual(table.columns[next(iter(table.columns))].label.plain, "PR (tip SHA)")
            self.assertEqual(app.query_one("#flow-pane").border_title, "Flow · select a PR above")
            for revision in app.states:
                self.assertEqual(table.get_cell(revision, "Revision"), revision[:12])
                events = [event for event in app.simulation.events()
                          if event["revision"] == revision]
                finish = max(event["time"] for event in events)
                start = min(event["time"] for event in events
                            if event["kind"] == "pipeline_started")
                duration = finish - start
                self.assertEqual(app.query_one(DataTable).get_cell(revision, "Duration"),
                                 f"{duration:.2f}s")
                run_start = min(event["time"] for event in app.simulation.events())
                self.assertEqual(table.get_cell(revision, "Start"), f"{start - run_start:.2f}s")
                self.assertEqual(table.get_cell(revision, "Finish"), f"{finish - run_start:.2f}s")
            self.assertTrue(all(row["release"] == "finished" for row in app.states.values()))
            status = str(app.query_one("#status", Static).render())
            self.assertIn("Finished", status)
            self.assertRegex(status, r"Finished · \d+\.\d{2}s")
            first = app.simulation
            first_events = first.events()
            first_hits = {(event["revision"], event["app"]): event["cache_hit"]
                          for event in first_events if event["kind"] == "build_finished"}
            self.assertEqual([[first_hits[revision, name] for name in APPS]
                              for revision in app._last_revisions],
                             [[False, False, False], [True, False, False], [False, True, True]])
            log = app.query_one(EventLog).text
            self.assertIn(str(first.state / "deployments"), log)
            self.assertIn("PRs ready: 3", log)
            self.assertNotIn("Revisions ready", log)
            table.move_cursor(row=0)
            await pilot.pause()
            selected = app._last_revisions[0]
            self.assertEqual(app._selected_commit, selected)
            self.assertIn(f"PR (tip SHA): {selected[:12]}",
                          str(app.query_one("#flow", Static).render()))
            await pilot.press("r")
            await self.finished(app, pilot)
            builds = [event for event in app.simulation.events()
                      if event["kind"] == "build_finished"]
            self.assertEqual(len(builds), 9)
            self.assertEqual({(event["revision"], event["app"]): event["cache_hit"]
                              for event in builds}, first_hits)
            self.assertNotEqual(app.simulation.state, first.state)
            self.assertEqual(app.simulation.state.parent, self.directory / "runs")
            self.assertTrue(first.state.exists())
            self.assertEqual(first.events(), first_events)

    async def test_ctrl_c_copies_selection_otherwise_quits_or_waits_for_active_run(self):
        app = BuildApp(self.directory, 0)
        async with app.run_test(size=(120, 30)) as pilot:
            app.pop_screen()
            log = app.query_one(EventLog)
            log.write("Selected event")
            log.focus()
            log.move_cursor((0, 0))
            await pilot.press("shift+end")
            self.assertTrue(log.selected_text)
            app.running = True
            await pilot.press("ctrl+c")
            self.assertTrue(app.is_running)
            self.assertEqual(app.clipboard, "Selected event")
            self.assertNotIn("Still running", log.text)
            await pilot.press("right", "ctrl+c")
            self.assertTrue(app.is_running)
            self.assertIn("Still running", log.text)
            app.running = False
            await pilot.press("ctrl+c")
            self.assertFalse(app.is_running)

    async def test_event_log_copy_read_only_selection_and_scroll_follow(self):
        app = BuildApp(self.directory, 0)
        async with app.run_test(size=(120, 30)) as pilot:
            app.pop_screen()
            log = app.query_one(EventLog)
            self.assertTrue(log.read_only)
            log.focus()
            for index in range(40):
                log.write(f"Event {index}")
            await pilot.pause()
            self.assertGreater(log.max_scroll_y, 0)
            self.assertEqual(log.scroll_y, log.max_scroll_y)

            log.write("Selected event")
            await pilot.pause()
            self.assertEqual(log.scroll_y, log.max_scroll_y)
            log.move_cursor((40, 0))
            await pilot.press("shift+end")
            self.assertEqual(log.selected_text, "Selected event")
            await pilot.press("ctrl+y")
            self.assertTrue(app.is_running)
            self.assertEqual(app.clipboard, "Selected event")

            scroll_y = log.scroll_y
            log.write("Another event")
            await pilot.pause()
            self.assertEqual(log.selected_text, "Selected event")
            self.assertEqual(log.scroll_y, scroll_y)
            self.assertLess(log.scroll_y, log.max_scroll_y)
            self.assertTrue(log.text.endswith("Selected event\nAnother event\n"))
            text = log.text
            await pilot.press("x", "backspace", "delete", "ctrl+v")
            self.assertEqual(log.text, text)

            await pilot.press("right")
            self.assertFalse(log.selected_text)
            log.move_cursor((0, 0))
            log.scroll_home(animate=False)
            await pilot.pause()
            log.write("While scrolled up")
            await pilot.pause()
            self.assertEqual(log.scroll_y, 0)
            log.scroll_end(animate=False)
            await pilot.pause()
            log.write("Following again")
            await pilot.pause()
            self.assertEqual(log.scroll_y, log.max_scroll_y)

    async def test_native_clipboard_commands_and_terminal_fallback(self):
        app = BuildApp(self.directory, 0)
        async with app.run_test() as pilot:
            app.pop_screen()
            log = app.query_one(EventLog)
            log.write("Selected café")
            log.focus()
            log.move_cursor((0, 0))
            await pilot.press("shift+end")
            with patch("lib.tui.subprocess.run") as run:
                await pilot.press("ctrl+y")
                run.assert_not_called()
                with patch.object(BuildApp, "is_headless", False):
                    for platform, command, encoding in (
                        ("darwin", ["pbcopy"], "utf-8"),
                        ("win32", ["clip.exe"], "utf-16le"),
                    ):
                        with self.subTest(platform=platform), patch("lib.tui.sys.platform", platform):
                            await pilot.press("ctrl+y")
                            self.assertEqual(run.call_args.args, (command,))
                            self.assertEqual(run.call_args.kwargs["input"],
                                             "Selected café".encode(encoding))
                            self.assertTrue(run.call_args.kwargs["check"])
                            self.assertEqual(run.call_args.kwargs["timeout"], 2)
                    run.reset_mock()
                    with patch("lib.tui.sys.platform", "linux"):
                        await pilot.press("ctrl+y")
                        run.assert_not_called()
                    with patch("lib.tui.sys.platform", "darwin"), patch.object(
                        app, "notify"
                    ) as notify:
                        run.side_effect = OSError("clipboard unavailable")
                        await pilot.press("ctrl+y")
                        notify.assert_called_once()
                        self.assertEqual(app.clipboard, "Selected café")
            await pilot.press("right")
            with patch.object(app, "copy_to_clipboard") as copy:
                await pilot.press("ctrl+y")
                copy.assert_not_called()
            self.assertTrue(app.is_running)

    async def test_new_changes_cancel_and_escape_preserve_run(self):
        app = BuildApp(self.directory, 0)
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.press("enter")
            await self.finished(app, pilot)
            original_revisions = app._last_revisions.copy()
            original_manifest = (self.directory / "fixture.json").read_bytes()
            original_events = app.simulation.events()
            repo = self.directory / "repo"
            original_head = git(repo, "rev-parse", "HEAD")
            cache = app.simulation.state / "cache"
            original_cache = {path: path.read_bytes() for path in cache.glob("*.json")}
            for cancel in ("button", "escape"):
                await pilot.press("n")
                self.assertIsInstance(app.screen, NewChanges)
                await pilot.click("#change-greeb")
                await pilot.click("#queue-commit")
                await pilot.click("#change-zorch")
                await pilot.click("#queue-example")
                self.assertEqual(app.screen.batch, [["greeb"], list(APPS), ["greeb"], ["zorch"]])
                self.assertTrue(app.screen.query_one("#change-zorch", Checkbox).value)
                await pilot.press("r", "n")
                self.assertFalse(app.running)
                if cancel == "button":
                    await pilot.click("#cancel")
                else:
                    await pilot.press("escape")
                self.assertNotIsInstance(app.screen, NewChanges)
                self.assertEqual(app._last_revisions, original_revisions)
                self.assertEqual((self.directory / "fixture.json").read_bytes(), original_manifest)
                self.assertEqual(app.simulation.events(), original_events)
                self.assertEqual(git(repo, "rev-parse", "HEAD"), original_head)
                self.assertEqual({path: path.read_bytes() for path in cache.glob("*.json")},
                                 original_cache)
                self.assertEqual(app.query_one(DataTable).row_count, 3)
            self.assertEqual(app.directory, self.directory)
        self.assertTrue(self.directory.exists())

    async def test_new_changes_checkbox_rendering_and_instructions(self):
        app = BuildApp(self.directory, 0)
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.press("enter")
            await self.finished(app, pilot)
            await pilot.press("n")
            modal = app.screen
            self.assertIsInstance(modal, NewChanges)
            self.assertEqual(modal.query_one("#queue-commit", Button).label.plain,
                             "Queue PR")
            self.assertEqual(modal.query_one("#run-pipeline", Button).label.plain,
                             "Run pipeline")
            instructions = "\n".join(str(widget.render()) for widget in modal.query(Static))
            for text in ("Check a box to include changes for that app in a simulated PR.",
                         "The selected apps will change together in one PR.",
                         "one tip commit", "no real Git work is required",
                         "No PRs are actually merged",
                         "Queue PR lets you prepare another PR before running.",
                         "Run pipeline simulates merged PRs triggering builds and deployments",
                         "for queued PRs and any checked changes.", "No PRs queued",
                         "Next PR: no source changes"):
                self.assertIn(text, instructions)
            self.assertNotIn("Queue commit", instructions)
            self.assertNotIn("invalidat", instructions.lower())
            checkbox = modal.query_one("#change-greeb", SourceCheckbox)
            self.assertFalse(checkbox.value)
            self.assertEqual(checkbox._button.plain,
                             f"{checkbox.BUTTON_LEFT} {checkbox.BUTTON_RIGHT}")
            await pilot.click("#change-greeb")
            self.assertTrue(checkbox.value)
            self.assertEqual(checkbox._button.plain,
                             f"{checkbox.BUTTON_LEFT}X{checkbox.BUTTON_RIGHT}")
            await pilot.click("#change-greeb")
            self.assertFalse(checkbox.value)
            self.assertEqual(checkbox._button.plain,
                             f"{checkbox.BUTTON_LEFT} {checkbox.BUTTON_RIGHT}")
            await pilot.press("escape")

    async def test_new_changes_batch_and_fresh_rerun(self):
        app = BuildApp(self.directory, 0)
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.press("enter")
            await self.finished(app, pilot)
            history = app._last_revisions.copy()
            await pilot.press("n")
            await pilot.click("#change-zorch")
            await pilot.click("#queue-commit")
            self.assertEqual(app.screen.batch, [["zorch"]])
            self.assertTrue(all(not checkbox.value for checkbox in app.screen.query(Checkbox)))
            self.assertIn("1. zorch", str(app.screen.query_one("#batch-summary", Static).render()))
            await pilot.click("#change-greeb")
            summary = str(app.screen.query_one("#selection-summary", Static).render())
            self.assertIn("Next PR: greeb", summary)
            self.assertNotIn("invalidat", summary.lower())
            self.assertFalse(app.screen.query_one("#change-blerg", Checkbox).value)
            await pilot.click("#run-pipeline")
            await self.finished(app, pilot)
            self.assertFalse(app.failed)
            batch = app._last_revisions.copy()
            self.assertEqual(len(batch), 2)
            self.assertTrue(set(batch).isdisjoint(history))
            self.assertEqual(app.query_one(DataTable).row_count, 2)
            self.assertEqual(json.loads((self.directory / "fixture.json").read_text())["revisions"],
                             history + batch)
            builds = [event for event in app.simulation.events()
                      if event["kind"] == "build_finished" and event["revision"] in batch]
            self.assertEqual(len(builds), 6)
            hits = {(event["revision"], event["app"]): event["cache_hit"] for event in builds}
            self.assertEqual([hits[batch[0], name] for name in APPS], [False, True, True])
            self.assertEqual([hits[batch[1], name] for name in APPS], [True, False, False])
            await pilot.press("r")
            await self.finished(app, pilot)
            self.assertFalse(app.failed)
            self.assertEqual(app._last_revisions, batch)
            self.assertEqual(app.query_one(DataTable).row_count, 2)
            rerun_hits = {(event["revision"], event["app"]): event["cache_hit"]
                          for event in app.simulation.events()
                          if event["kind"] == "build_finished"}
            self.assertEqual(rerun_hits, hits)
            self.assertEqual(json.loads((self.directory / "fixture.json").read_text())["revisions"],
                             history + batch)
            self.assertEqual(app.directory, self.directory)

    async def test_queue_example_appends_history_and_fresh_rerun(self):
        app = BuildApp(self.directory, 0)
        with patch("lib.tui.execute", wraps=execute) as mocked:
            async with app.run_test(size=(120, 35)) as pilot:
                await pilot.press("enter")
                await self.finished(app, pilot)
                history = app._last_revisions.copy()
                original_simulation = app.simulation
                original_events = app.simulation.events()
                repo = self.directory / "repo"
                cache = app.simulation.state / "cache"
                original_cache = {path: path.read_bytes() for path in cache.glob("*.json")}
                self.assertTrue(original_cache)
                await pilot.press("n")
                button = app.screen.query_one("#queue-example", Button)
                self.assertEqual(button.label.plain, "Queue example (3 PRs)")
                await pilot.click("#queue-example")
                self.assertEqual(app.screen.batch, [list(APPS), ["greeb"], ["zorch"]])
                summary = str(app.screen.query_one("#batch-summary", Static).render())
                for text in ("Queued PRs:", "1. zorch, greeb, blerg", "2. greeb", "3. zorch"):
                    self.assertIn(text, summary)
                self.assertEqual(mocked.call_count, 1)
                await pilot.click("#run-pipeline")
                await self.finished(app, pilot)
                self.assertFalse(app.failed)
                self.assertEqual(mocked.call_args.kwargs["changes"], [list(APPS), ["greeb"], ["zorch"]])
                batch = app._last_revisions.copy()
                self.assertEqual(len(set(batch)), 3)
                self.assertTrue(set(batch).isdisjoint(history))
                self.assertEqual(app.query_one(DataTable).row_count, 3)
                manifest = (self.directory / "fixture.json").read_bytes()
                self.assertEqual(json.loads(manifest)["revisions"], history + batch)
                for previous, revision, changed in zip(history[-1:] + batch, batch,
                                                       ("source/blerg.txt\nsource/greeb.txt\nsource/zorch.txt", "source/greeb.txt", "source/zorch.txt")):
                    self.assertEqual(git(repo, "rev-parse", f"{revision}^"), previous)
                    self.assertEqual(git(repo, "diff-tree", "--no-commit-id", "--name-only",
                                         "-r", revision), changed)
                self.assertEqual(git(repo, "show", f"{batch[1]}:source/greeb.txt"), "greeb-v5")
                self.assertEqual(git(repo, "show", f"{batch[2]}:source/zorch.txt"), "zorch-v5")
                for path, content in original_cache.items():
                    self.assertEqual(path.read_bytes(), content)
                events = app.simulation.events()
                self.assertEqual(original_simulation.events(), original_events)
                self.assertNotEqual(app.simulation.state, original_simulation.state)
                builds = [event for event in events
                          if event["kind"] == "build_finished" and event["revision"] in batch]
                self.assertEqual(len(builds), 9)
                hits = {(event["revision"], event["app"]): event["cache_hit"] for event in builds}
                self.assertEqual([[hits[revision, name] for name in APPS] for revision in batch],
                                 [[False, False, False], [True, False, False], [False, True, True]])
                await pilot.press("r")
                await self.finished(app, pilot)
                self.assertFalse(app.failed)
                self.assertEqual(mocked.call_args.kwargs["revisions"], batch)
                self.assertNotIn("changes", mocked.call_args.kwargs)
                self.assertEqual(app._last_revisions, batch)
                self.assertEqual((self.directory / "fixture.json").read_bytes(), manifest)
                self.assertEqual(git(repo, "rev-parse", "HEAD"), batch[-1])
                self.assertEqual(app.directory, self.directory)
                builds = [event for event in app.simulation.events()
                          if event["kind"] == "build_finished"]
                self.assertEqual(len(builds), 9)
                self.assertEqual({(event["revision"], event["app"]): event["cache_hit"]
                                  for event in builds}, hits)
                self.assertTrue(all(call.kwargs["reset_state"] for call in mocked.call_args_list))

    async def test_queue_example_preserves_queued_and_pending_choices(self):
        app = BuildApp(self.directory, 0)
        with patch("lib.tui.execute", wraps=execute) as mocked:
            async with app.run_test(size=(120, 35)) as pilot:
                await pilot.click("#choose-changes")
                await pilot.click("#change-blerg")
                await pilot.click("#queue-commit")
                await pilot.click("#change-zorch")
                await pilot.click("#change-greeb")
                selection = str(app.screen.query_one("#selection-summary", Static).render())
                await pilot.click("#queue-example")
                expected = [["blerg"], list(APPS), ["greeb"], ["zorch"]]
                self.assertEqual(app.screen.batch, expected)
                self.assertTrue(app.screen.query_one("#change-zorch", Checkbox).value)
                self.assertTrue(app.screen.query_one("#change-greeb", Checkbox).value)
                self.assertFalse(app.screen.query_one("#change-blerg", Checkbox).value)
                self.assertEqual(str(app.screen.query_one("#selection-summary", Static).render()),
                                 selection)
                mocked.assert_not_called()
                self.assertFalse(self.directory.exists())
                await pilot.click("#run-pipeline")
                await self.finished(app, pilot)
                self.assertFalse(app.failed)
                self.assertEqual(mocked.call_args.kwargs["changes"], expected + [["zorch", "greeb"]])
                self.assertEqual(len(set(app._last_revisions)), 5)
                self.assertEqual(app.query_one(DataTable).row_count, 5)

    async def test_run_queued_changes_does_not_add_empty_selection(self):
        app = BuildApp(self.directory, 0)
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.press("enter")
            await self.finished(app, pilot)
            with patch("lib.tui.execute", wraps=execute) as mocked:
                await pilot.press("n")
                await pilot.click("#change-blerg")
                await pilot.click("#queue-commit")
                await pilot.click("#run-pipeline")
                await self.finished(app, pilot)
                self.assertEqual(mocked.call_args.kwargs["changes"], [["blerg"]])
                self.assertEqual(len(app._last_revisions), 1)
                self.assertFalse(app.failed)

    async def test_run_empty_selection_creates_empty_commit(self):
        app = BuildApp(self.directory, 0)
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.press("enter")
            await self.finished(app, pilot)
            history = app._last_revisions.copy()
            with patch("lib.tui.execute", wraps=execute) as mocked:
                await pilot.press("n")
                await pilot.click("#run-pipeline")
                await self.finished(app, pilot)
                self.assertEqual(mocked.call_args.kwargs["changes"], [[]])
            self.assertFalse(app.failed)
            self.assertEqual(len(app._last_revisions), 1)
            self.assertNotIn(app._last_revisions[0], history)
            self.assertTrue(all(row[name] == "finished (cache hit)"
                                for row in app.states.values() for name in APPS))
            builds = [event for event in app.simulation.events()
                      if event["kind"] == "build_finished"]
            self.assertEqual(len(builds), 3)
            self.assertTrue(all(event["cache_hit"] for event in builds))
            self.assertTrue(all(row[f"deploy-{app}"] == "skipped"
                                for row in app.states.values() for app in APPS))

    async def test_pr_start_finish_offsets_wait_for_nested_pipelines(self):
        app = BuildApp(self.directory, 0)
        async with app.run_test(size=(120, 30)) as pilot:
            app.pop_screen()
            app._revisions_ready(["first", "second"])
            table = app.query_one(DataTable)
            self.assertEqual(table.get_cell("first", "Start"), "—")
            self.assertEqual(table.get_cell("first", "Finish"), "—")

            def observe(revision, kind, time, pool):
                app._observe(dict(revision=revision, kind=kind, time=time, pool=pool))

            observe("first", "pipeline_waiting", 10, "root")
            observe("second", "pipeline_waiting", 10.1, "root")
            observe("first", "pipeline_started", 11, "root")
            self.assertEqual(table.get_cell("first", "Start"), "1.00s")
            self.assertEqual(table.get_cell("second", "Start"), "—")
            observe("first", "pipeline_waiting", 12, "child")
            observe("first", "pipeline_started", 13, "child")
            observe("first", "pipeline_finished", 14, "child")
            self.assertEqual(table.get_cell("first", "Finish"), "—")
            observe("first", "pipeline_finished", 15, "root")
            self.assertEqual(table.get_cell("first", "Finish"), "5.00s")
            self.assertEqual(table.get_cell("first", "Duration"), "4.00s")
            observe("second", "pipeline_started", 15.5, "root")
            observe("second", "pipeline_failed", 17, "root")
            self.assertEqual(table.get_cell("second", "Start"), "5.50s")
            self.assertEqual(table.get_cell("second", "Finish"), "7.00s")
            self.assertEqual(table.get_cell("second", "Duration"), "1.50s")
            observe("first", "pipeline_waiting", 9, "late")
            self.assertEqual(table.get_cell("first", "Start"), "2.00s")
            self.assertEqual(table.get_cell("first", "Finish"), "6.00s")

    async def test_unpooled_pr_finish_waits_for_unpooled_child(self):
        app = BuildApp(self.directory, 0)
        async with app.run_test() as pilot:
            app.pop_screen()
            app._revisions_ready(["revision"])
            table = app.query_one(DataTable)
            for kind, timestamp in (("pipeline_started", 10), ("pipeline_started", 11),
                                    ("pipeline_finished", 12)):
                app._observe(dict(revision="revision", kind=kind, time=timestamp, pool=None))
            self.assertEqual(table.get_cell("revision", "Finish"), "—")
            app._observe(dict(revision="revision", kind="pipeline_finished", time=13, pool=None))
            self.assertEqual(table.get_cell("revision", "Start"), "0.00s")
            self.assertEqual(table.get_cell("revision", "Finish"), "3.00s")

    async def test_pipeline_events_and_blocked_tasks(self):
        app = BuildApp(self.directory, 0)
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.press("enter")
            await self.finished(app, pilot)
            revision = app._last_revisions[0]
            original = app.states[revision].copy()
            app._revision_times.clear()
            for timestamp in (10.0, 11.234, 10.5):
                app._observe(dict(revision=revision, kind="pipeline_started", time=timestamp))
            self.assertEqual(app.query_one(DataTable).get_cell(revision, "Duration"), "1.23s")
            for kind in ("pipeline_waiting", "pipeline_started", "pipeline_finished"):
                app._observe(dict(revision=revision, kind=kind, pipeline="builds"))
                self.assertEqual(app.states[revision], original)
            app._observe(dict(revision=revision, kind="deploy_requested"))
            self.assertEqual(app.states[revision]["release"], "ready")
            for task, column in (("blerg", "blerg"), ("deploy", "release")):
                app._observe(dict(revision=revision, kind="task_blocked", task=task))
                self.assertEqual(app.states[revision][column], "blocked")
            blocked = app.states[revision].copy()
            app._observe(dict(revision=revision, kind="task_blocked", task="nested"))
            self.assertEqual(app.states[revision], blocked)
            app._observe(dict(revision=revision, kind="pipeline_failed", pipeline="builds"))
            self.assertTrue(app.failed)
            self.assertEqual(app.states[revision], blocked)
            log = app.query_one(EventLog).text
            self.assertIn("task_blocked blerg", log)
            self.assertIn("pipeline_failed", log)

    async def test_hot_reload_rerun_new_changes_and_syntax_recovery(self):
        self.pipeline_patch.stop()
        declaration = Path(self.temporary.name) / "pipeline.py"

        def edit_pipeline(pool):
            declaration.write_text(
                "from lib.workflow import Build, Deploy, Pipeline\n"
                f"pipeline = Pipeline(pool={pool!r}, tasks=[\n"
                "    Build('zorch'), Build('greeb'), Build('blerg', needs=['greeb']),\n"
                "    Deploy('deploy-zorch', 'zorch', needs=['zorch']),\n"
                "    Deploy('deploy-greeb', 'greeb', needs=['greeb', 'deploy-zorch']),\n"
                "    Deploy('deploy-blerg', 'blerg', needs=['blerg', 'deploy-greeb']),\n"
                "])\n"
            )

        def assert_pool(simulation, pool):
            events = [event for event in simulation.events()
                      if event["kind"] == "pipeline_started"]
            self.assertTrue(events)
            self.assertEqual({event["pool"] for event in events}, {pool})

        edit_pipeline("first")
        app = BuildApp(self.directory, 0)
        with patch("lib.exercise.PIPELINE_PATH", declaration):
            async with app.run_test(size=(120, 35)) as pilot:
                await pilot.press("enter")
                await self.finished(app, pilot)
                self.assertFalse(app.failed)
                first = app.simulation
                first_events = first.events()
                assert_pool(first, "first")
                manifest = (self.directory / "fixture.json").read_bytes()
                revisions = app._last_revisions.copy()
                edit_pipeline("rerun")
                await pilot.press("r")
                await self.finished(app, pilot)
                self.assertFalse(app.failed)
                rerun = app.simulation
                assert_pool(rerun, "rerun")
                self.assertNotEqual(first.state, rerun.state)
                self.assertEqual(app._last_revisions, revisions)
                self.assertEqual((self.directory / "fixture.json").read_bytes(), manifest)
                edit_pipeline("changes")
                await pilot.press("n")
                await pilot.click("#change-greeb")
                await pilot.click("#run-pipeline")
                await self.finished(app, pilot)
                self.assertFalse(app.failed)
                changed = app.simulation
                assert_pool(changed, "changes")
                self.assertEqual(len(app._last_revisions), 1)
                self.assertNotEqual(changed.state, rerun.state)
                manifest = (self.directory / "fixture.json").read_bytes()
                head = git(self.directory / "repo", "rev-parse", "HEAD")
                declaration.write_text("pipeline = (\n")
                await pilot.press("n")
                await pilot.click("#change-zorch")
                await pilot.click("#run-pipeline")
                await self.finished(app, pilot)
                self.assertTrue(app.failed)
                self.assertIn("SyntaxError", str(app.query_one("#status", Static).render()))
                log = app.query_one(EventLog).text
                self.assertIn("SyntaxError", log)
                self.assertEqual((self.directory / "fixture.json").read_bytes(), manifest)
                self.assertEqual(git(self.directory / "repo", "rev-parse", "HEAD"), head)
                self.assertEqual(len(list((self.directory / "runs").iterdir())), 3)
                edit_pipeline("recovered")
                await pilot.press("r")
                await self.finished(app, pilot)
                self.assertFalse(app.failed)
                assert_pool(app.simulation, "recovered")
                self.assertEqual((self.directory / "fixture.json").read_bytes(), manifest)
                self.assertEqual(first.events(), first_events)
                for simulation in (first, rerun, changed, app.simulation):
                    self.assertTrue((simulation.state / "deployments").exists())
                self.assertEqual(len(list((self.directory / "runs").iterdir())), 4)

    async def test_worker_failure_visible_and_rerunnable(self):
        app = BuildApp(self.directory, 0)
        with patch("lib.tui.execute", side_effect=RuntimeError("broken fixture")):
            async with app.run_test() as pilot:
                await pilot.press("enter")
                await self.finished(app, pilot)
                self.assertTrue(app.failed)
                self.assertIn("broken fixture", str(app.query_one("#status", Static).render()))
                self.assertTrue(app.query_one(EventLog).text)
                with patch("lib.tui.execute", execute):
                    await pilot.press("r")
                    await self.finished(app, pilot)
                    self.assertFalse(app.failed)

    async def test_forced_exit_during_start_joins_thread(self):
        release = threading.Event()
        started = threading.Event()
        unmounting = asyncio.Event()
        self.addCleanup(release.set)
        self.directory.mkdir()
        directory = self.directory
        existed_at_completion = []

        class ImmediateExitApp(BuildApp):
            def _start(self):
                super()._start()
                self.exit()

            async def on_unmount(self):
                unmounting.set()
                await super().on_unmount()

        def controlled(directory, speed, *, observer, on_ready, reset_state):
            self.assertTrue(reset_state)
            started.set()
            if not release.wait(5):
                raise TimeoutError("test release")
            on_ready(["revision"])
            observer(dict(revision="revision", kind="deploy_finished"))
            existed_at_completion.append(directory.exists())
            return SimpleNamespace(state=directory / "runs/run-test")

        app = ImmediateExitApp(directory, 0)

        async def run():
            async with app.run_test() as pilot:
                await pilot.press("enter")

        with patch("lib.tui.execute", side_effect=controlled), patch.object(
            app, "call_from_thread", side_effect=RuntimeError("App is not running")
        ):
            task = asyncio.create_task(run())
            try:
                await asyncio.wait_for(unmounting.wait(), 5)
                self.assertTrue(started.is_set())
                self.assertTrue(app._thread.is_alive())
                self.assertTrue(app.running)
                self.assertTrue(directory.exists())
                self.assertFalse(task.done())
            finally:
                release.set()
                await asyncio.wait_for(task, 5)
        self.assertFalse(app._thread.is_alive())
        self.assertEqual(existed_at_completion, [True])
        self.assertTrue(directory.exists())

    async def test_forced_exit_while_executing_does_not_block_callbacks(self):
        release = threading.Event()
        started = threading.Event()
        unmounting = asyncio.Event()
        self.addCleanup(release.set)
        existed_at_completion = []

        class ShutdownApp(BuildApp):
            async def on_unmount(self):
                unmounting.set()
                await super().on_unmount()

        def controlled(directory, speed, *, observer, on_ready, revisions, reset_state):
            self.assertTrue(reset_state)
            self.assertEqual(revisions, app._last_revisions)
            on_ready(["revision"])
            started.set()
            if not release.wait(5):
                raise TimeoutError("test release")
            with ThreadPoolExecutor(max_workers=3) as pool:
                list(pool.map(lambda name: observer(dict(revision="revision", app=name,
                                                         kind="build_finished")), APPS))
            existed_at_completion.append(directory.exists())
            raise RuntimeError("failure during shutdown")

        app = ShutdownApp(self.directory, 0)
        run_thread = None

        async def run():
            nonlocal run_thread
            async with app.run_test() as pilot:
                await pilot.press("enter")
                await self.finished(app, pilot)
                with patch("lib.tui.execute", side_effect=controlled):
                    app.action_rerun()
                    run_thread = app._thread
                    self.assertTrue(await asyncio.to_thread(started.wait, 5))
                    app.exit()

        with patch.object(app, "call_from_thread", side_effect=RuntimeError("App is not running")):
            task = asyncio.create_task(run())
            try:
                await asyncio.wait_for(unmounting.wait(), 10)
                self.assertTrue(app.running)
                self.assertTrue(run_thread.is_alive())
                self.assertTrue(self.directory.exists())
                self.assertFalse(task.done())
            finally:
                release.set()
                await asyncio.wait_for(task, 5)
        self.assertFalse(run_thread.is_alive())
        self.assertEqual(existed_at_completion, [True])
        self.assertTrue(self.directory.exists())

    async def test_states_concurrent_callbacks_and_busy_controls(self):
        release = threading.Event()
        started = threading.Event()
        self.addCleanup(release.set)

        def controlled(directory, speed, *, observer, on_ready, reset_state):
            self.assertTrue(reset_state)
            on_ready(["revision"])
            observer(dict(revision="revision", app="zorch", kind="build_requested"))
            started.set()
            if not release.wait(10):
                raise TimeoutError("test release")
            with ThreadPoolExecutor(max_workers=3) as pool:
                list(pool.map(lambda name: observer(dict(revision="revision", app=name,
                                                         kind="build_started")), APPS))
            observer(dict(revision="revision", app="zorch", kind="cache_hit"))
            observer(dict(revision="revision", app="greeb", kind="build_failed"))
            observer(dict(revision="revision", app="blerg", kind="build_finished", cache_hit=False))
            observer(dict(revision="revision", kind="deploy_requested"))
            observer(dict(revision="revision", kind="deploy_started"))
            observer(dict(revision="revision", kind="deploy_failed"))
            raise RuntimeError("build failed")

        app = BuildApp(self.directory, 0)
        with patch("lib.tui.execute", side_effect=controlled) as mocked:
            async with app.run_test() as pilot:
                try:
                    await pilot.press("enter")
                    self.assertTrue(await asyncio.to_thread(started.wait, 5))
                    self.assertEqual(app.states["revision"]["zorch"], "waiting (cache lock)")
                    await pilot.press("r", "n", "q", "ctrl+c")
                    self.assertTrue(app.running)
                    self.assertEqual(mocked.call_count, 1)
                    self.assertEqual(app.directory, self.directory)
                finally:
                    release.set()
                await self.finished(app, pilot)
                self.assertTrue(app.failed)
                self.assertEqual(app.states["revision"], {
                    "zorch": "cache hit", "greeb": "failed", "blerg": "finished",
                    "deploy-zorch": "pending", "deploy-greeb": "pending",
                    "deploy-blerg": "pending", "release": "failed",
                })
                await pilot.press("q")


if __name__ == "__main__":
    unittest.main()
