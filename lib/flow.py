"""Text view of declared dependencies, independent of task list order."""

# Exercise infrastructure: do not modify unless fixing an exercise bug.
# Commit infrastructure fixes separately; normal solution changes belong in pipeline.py.

from lib.workflow import Build, Call, Deploy, validate


def flow_levels(pipeline):
    validate(pipeline)
    remaining = {task.name: task for task in pipeline.tasks}
    done = set()
    levels = []
    while remaining:
        ready = [task for task in remaining.values() if set(task.needs) <= done]
        levels.append(ready)
        for task in ready:
            del remaining[task.name]
        done.update(task.name for task in ready)
    return levels


def task_app(task):
    if isinstance(task, Build):
        return task.name
    if isinstance(task, Deploy):
        return task.app
    apps = {task_app(child) for child in task.pipeline.tasks} - {None}
    return next(iter(apps)) if len(apps) == 1 else None


def flow_layout(pipeline, width=20):
    levels = flow_levels(pipeline)
    edges = [(parent, task.name) for level in levels for task in level
             for parent in dict.fromkeys(task.needs)]
    lanes = {}
    for level in levels:
        for task in level:
            app = task_app(task)
            if app is not None and app not in lanes:
                lanes[app] = len(lanes)
    positions = {}
    for rank, level in enumerate(levels):
        occupied = set()
        for task in level:
            app = task_app(task)
            lane = lanes.get(app)
            if lane is None:
                lane = (positions[task.needs[0]][0] - width // 2) // (width + 6) if task.needs else 0
            while lane in occupied:
                lane += 1
            occupied.add(lane)
            positions[task.name] = (width // 2 + lane * (width + 6), rank)
    y = 0
    ranks = {name: rank for name, (_, rank) in positions.items()}
    for rank, level in enumerate(levels):
        for task in level:
            positions[task.name] = (positions[task.name][0], y)
        routed = [edge for edge in edges
                  if ranks[edge[0]] <= rank < ranks[edge[1]]
                  and (positions[edge[0]][0] != positions[edge[1]][0]
                       or ranks[edge[1]] - ranks[edge[0]] > 1)]
        y += 4 + max(3, len(routed) + 2)
    return positions, edges


def live_flow(pipeline, states, commit=None):
    from rich.cells import cell_len
    from rich.text import Text

    levels = flow_levels(pipeline)
    labels = {}
    statuses = {}
    styles = {}
    for level in levels:
        for task in level:
            status = states.get(task.name, "pending")
            if task.name not in states and task.__class__.__name__ == "Deploy":
                status = states.get(f"deploy-{task.app}", states.get("release", "pending"))
            status = status.replace("finished (cache hit)", "cached").replace("finished", "done")
            statuses[task.name] = status
            styles[task.name] = ("red" if status in ("failed", "blocked") else
                                 "cyan" if "cache" in status else
                                 "green" if status == "done" else
                                 "bold yellow" if status in ("building", "deploying") else "dim")
            labels[task.name] = task.name + (" ↗" if isinstance(task, Call) else "")
    width = max([20] + [cell_len(value) + 4 for value in labels.values()] +
                [cell_len(value) + 4 for value in statuses.values()] +
                [2 * len(task.needs) + 4 for level in levels for task in level])
    positions, edges = flow_layout(pipeline, width)
    outgoing = {name: [] for name in positions}
    incoming = {name: [] for name in positions}
    for edge in edges:
        outgoing[edge[0]].append(edge)
        incoming[edge[1]].append(edge)
    width = max(width, max((2 * len(value) + 4 for value in outgoing.values()), default=0))
    width += width % 2
    positions, edges = flow_layout(pipeline, width)
    source_ports = {edge: positions[edge[0]][0] for edge in edges}
    target_ports = {edge: positions[edge[1]][0] for edge in edges}
    ranks = {task.name: rank for rank, level in enumerate(levels) for task in level}
    gap_rows = {}
    for rank, level in enumerate(levels[:-1]):
        top = positions[level[0].name][1] + 4
        routed = [edge for edge in edges
                  if ranks[edge[0]] <= rank < ranks[edge[1]]
                  and (source_ports[edge] != target_ports[edge]
                       or ranks[edge[1]] - ranks[edge[0]] > 1)]
        for index, edge in enumerate(routed):
            gap_rows[edge, rank] = top + index
    lines = {}
    arrows = set()
    trunks = []

    def segment(edge, start, end):
        x, y = start
        dx = (end[0] > x) - (end[0] < x)
        dy = (end[1] > y) - (end[1] < y)
        while (x, y) != end:
            nx, ny = x + dx, y + dy
            forward, backward = (2, 8) if dx > 0 else (8, 2) if dx < 0 else (4, 1)
            for point, direction in (((x, y), forward), ((nx, ny), backward)):
                cell = lines.setdefault(point, {})
                cell[edge] = cell.get(edge, 0) | direction
            x, y = nx, ny

    for edge in edges:
        parent, child = edge
        sx, tx = source_ports[edge], target_ports[edge]
        sy, ty = positions[parent][1] + 4, positions[child][1] - 1
        first, last = ranks[parent], ranks[child] - 1
        start_row, end_row = gap_rows.get((edge, first), sy), gap_rows.get((edge, last), sy)
        obstacles = [(x - width // 2, x - width // 2 + width - 1)
                     for x, y in positions.values() if sy <= y < ty]
        if sx == tx and not any(left <= sx <= right for left, right in obstacles):
            points = [(sx, sy), (tx, ty)]
        elif first == last:
            points = [(sx, sy), (sx, end_row), (tx, end_row), (tx, ty)]
        else:
            candidates = [sx] + [x for left, right in obstacles for x in (left - 2, right + 2)]
            candidates += [max(x for x, y in positions.values()) + width + 2 * index
                           for index in range(len(edges) + 1)]
            candidates.sort(key=lambda x: abs(x - sx) + abs(x - tx))
            trunk = next(x for x in candidates
                         if not any(left <= x <= right for left, right in obstacles)
                         and x not in target_ports.values()
                         and not any(x == port and other != edge
                                     for other, port in source_ports.items())
                         and not any(x == used and sy <= bottom and ty >= top
                                     for used, top, bottom in trunks))
            trunks.append((trunk, sy, ty))
            points = [(sx, sy), (sx, start_row), (trunk, start_row),
                      (trunk, end_row), (tx, end_row), (tx, ty)]
        for start, end in zip(points, points[1:]):
            segment(edge, start, end)
        cell = lines.setdefault((sx, sy), {})
        cell[edge] = cell.get(edge, 0) | 1
        arrows.add((tx, ty))

    glyphs = {1: "│", 2: "─", 4: "│", 8: "─", 5: "│", 10: "─",
              3: "└", 6: "┌", 9: "┘", 12: "┐", 7: "├", 13: "┤",
              11: "┴", 14: "┬", 15: "┼"}
    grid = {}
    for point, owners in lines.items():
        connected = (len({parent for parent, _ in owners}) == 1
                     or len({child for _, child in owners}) == 1)
        mask = 0
        for directions in owners.values():
            mask |= directions
        grid[point] = ("▼" if point in arrows else glyphs[mask] if connected else "╳", "dim")
    box_rows = {}
    for name, (center, y) in positions.items():
        left = center - width // 2
        top = list("┌" + "─" * (width - 2) + "┐")
        bottom = list("└" + "─" * (width - 2) + "┘")
        for edge in incoming[name]:
            top[target_ports[edge] - left] = "┴"
        for edge in outgoing[name]:
            bottom[source_ports[edge] - left] = "┬"

        def centered(value):
            padding = width - 2 - cell_len(value)
            return "│" + " " * (padding // 2) + value + " " * (padding - padding // 2) + "│"

        for offset, row in enumerate(("".join(top), centered(labels[name]),
                                      centered(statuses[name]), "".join(bottom))):
            box_rows[left, y + offset] = (row, styles[name])
    output = Text()
    output.append(f"PR (tip SHA): {commit[:12] if commit else 'select a PR'}\n", style="bold")
    output.append(f"Pool: {pipeline.pool or 'none'}\n\n", style="dim")
    if positions:
        left = min([x - width // 2 for x, y in positions.values()] + [x for x, y in grid])
        right = max([x - width // 2 + width for x, y in positions.values()] + [x + 1 for x, y in grid])
        for y in range(max(y for x, y in positions.values()) + 4):
            row = Text()
            x = left
            while x < right:
                if (x, y) in box_rows:
                    value, style = box_rows[x, y]
                    row.append(value, style=style)
                    x += width
                else:
                    value, style = grid.get((x, y), (" ", ""))
                    row.append(value, style=style)
                    x += 1
            row.rstrip()
            output.append_text(row)
            output.append("\n")
    else:
        output.append("(no tasks)\n", style="dim")
    output.append("\n▼ dependency · ↗ child pipeline", style="dim")
    if any(value == "╳" for value, _ in grid.values()):
        output.append("\n╳ crossing, not a join", style="dim")
    return output


def render_flow(pipeline, label="pipeline", depth=0):
    indent = "  " * depth
    lines = [f"{indent}{label} | pool: {pipeline.pool or 'none'}"]
    for stage, ready in enumerate(flow_levels(pipeline)):
        lines.append(f"{indent}  stage {stage}: " + "   |   ".join(
            f"[{task.name}]" for task in ready))
        for task in ready:
            if task.needs:
                lines.append(f"{indent}    {', '.join(task.needs)} -> {task.name}")
            if isinstance(task, Call):
                lines.extend(render_flow(task.pipeline, f"call {task.name}", depth + 2).splitlines())
    if not pipeline.tasks:
        lines.append(f"{indent}  (no tasks)")
    return "\n".join(lines)
