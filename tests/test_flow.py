import random
import unittest

from rich.cells import cell_len

from lib.exercise import load_pipeline
from lib.flow import flow_layout, live_flow, render_flow
from lib.workflow import Build, Call, Deploy, Pipeline


class FlowTests(unittest.TestCase):
    def test_dependencies_not_list_order_define_levels(self):
        declaration = Pipeline(pool="example", tasks=[
            Deploy("release", "zorch", needs=["zorch", "blerg"]),
            Build("blerg", needs=["greeb"]), Build("greeb"), Build("zorch"),
        ])
        output = render_flow(declaration)
        self.assertIn("stage 0: [greeb]   |   [zorch]", output)
        self.assertIn("stage 1: [blerg]", output)
        self.assertIn("stage 2: [release]", output)
        self.assertIn("greeb -> blerg", output)
        self.assertIn("pool: example", output)

    def test_nested_pipeline_and_pool_are_visible(self):
        declaration = Pipeline(tasks=[Call("component", Pipeline(
            pool="deployment", tasks=[Build("zorch")]))])
        self.assertIn("call component | pool: deployment", render_flow(declaration))
        self.assertIn("[zorch]", render_flow(declaration))
        self.assertIn("component ↗", live_flow(declaration, {}).plain)

    def test_connected_boxes_show_parallel_tasks_and_status(self):
        declaration = Pipeline(tasks=[Build("zorch"), Build("greeb"),
                                      Build("blerg", needs=["greeb"])])
        output = live_flow(declaration, {"zorch": "building", "greeb": "finished (cache hit)"},
                           "abcdef123456789").plain
        self.assertIn("PR (tip SHA): abcdef123456", output)
        self.assertIn("building", output)
        self.assertIn("cached", output)
        self.assertTrue(any("zorch" in line and "greeb" in line for line in output.splitlines()))
        self.assertIn("▼", output)
        self.assertNotIn("╳", output)
        self.assertIn("▼ dependency · ↗ child pipeline", output)
        self.assertNotIn("rows are not barriers", output)
        positions, _ = flow_layout(declaration)
        self.assertLess(positions["zorch"][0], positions["greeb"][0])
        self.assertLess(positions["greeb"][0], positions["blerg"][0])

    def test_arbitrary_dags_keep_boxes_intact_and_arrowheads_on_targets(self):
        randomizer = random.Random(1)
        for sample in range(40):
            declaration = Pipeline(tasks=[
                Deploy(f"t{index}", "zorch", needs=[f"t{parent}" for parent in range(index)
                                                   if randomizer.random() < 0.3])
                for index in range(6)
            ])
            with self.subTest(sample=sample):
                output = live_flow(declaration, {}).plain
                positions, _ = flow_layout(declaration)
                graph = output.split("\n▼ dependency")[0].splitlines()[3:]
                first_left = min(center - 10 for center, top in positions.values() if top == 0)
                shift = graph[0].index("┌") - first_left
                for task in declaration.tasks:
                    center, top = positions[task.name]
                    center += shift
                    self.assertIn(task.name, graph[top + 1])
                    self.assertEqual(graph[top][center - 10], "┌")
                    self.assertEqual(graph[top][center + 9], "┐")
                    if task.needs:
                        self.assertEqual(graph[top - 1][center], "▼")
                    for parent in task.needs:
                        self.assertLess(positions[parent][1], top)
                self.assertEqual(sum(line.count("▼") for line in graph),
                                 sum(bool(task.needs) for task in declaration.tasks))

    def test_starter_uses_stable_app_columns_and_clean_branches(self):
        declaration = load_pipeline()
        positions, _ = flow_layout(declaration)
        for app in ("zorch", "greeb", "blerg"):
            self.assertEqual(positions[app][0], positions[f"deploy-{app}"][0])
        output = live_flow(declaration, {}).plain
        self.assertNotIn("╳", output)
        self.assertNotIn("needs:", output)
        self.assertIn("├", output)
        self.assertIn("┤", output)
        graph = output.split("\n▼ dependency")[0].splitlines()[3:]
        center, top = positions["zorch"]
        end = positions["deploy-zorch"][1]
        self.assertTrue(all(graph[y][center] == "│" for y in range(top + 4, end - 1)))
        self.assertEqual(graph[end - 1][center], "▼")

    def test_live_status_styles_and_headers(self):
        declaration = Pipeline(pool="example", tasks=[
            Deploy("one", "zorch"), Deploy("two", "zorch"),
            Deploy("three", "zorch"), Deploy("four", "zorch"),
        ])
        output = live_flow(declaration, {"one": "failed", "two": "finished (cache hit)",
                                         "three": "finished", "four": "deploying"})
        self.assertIn("PR (tip SHA): select a PR", output.plain)
        self.assertIn("Pool: example", output.plain)
        for name, style in (("one", "red"), ("two", "cyan"), ("three", "green"),
                            ("four", "bold yellow")):
            start = output.plain.index(name)
            self.assertTrue(any(span.start <= start < span.end and span.style == style
                                for span in output.spans))

    def test_wide_labels_use_display_cell_width(self):
        output = live_flow(Pipeline(tasks=[Deploy("任务", "zorch"),
                                           Deploy("longer task name", "zorch")]), {}).plain
        label_row = next(line for line in output.splitlines() if "任务" in line)
        border_row = next(line for line in output.splitlines() if "┌" in line)
        self.assertEqual(cell_len(label_row), cell_len(border_row))

    def test_empty_pipeline(self):
        self.assertIn("(no tasks)", render_flow(Pipeline()))
        self.assertIn("(no tasks)", live_flow(Pipeline(), {}).plain)


if __name__ == "__main__":
    unittest.main()
