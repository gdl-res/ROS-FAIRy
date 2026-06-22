"""Mission diff display (ros2 fair diff).

Compares two MissionRecord objects section by section, printing only what
actually changed. Sections with no differences are silently omitted.
"""

from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from fair_ros.manifest.schema import MissionRecord
from fair_ros.ui.review import human_size
from fair_ros.utils.topic_health import humanize_duration

_MAX_GRAPH_ROWS = 20


def _mission_label(r: MissionRecord) -> str:
    dt = r.identity.created_at.astimezone().strftime("%Y-%m-%d %H:%M")
    goal = r.intent.goal if len(r.intent.goal) <= 50 else r.intent.goal[:49] + "…"
    return f"{dt}  {goal}  —  {r.intent.location_name}  ({r.identity.operator_name})"


def _table(rows: list[tuple[str, str, str]]) -> Table:
    """Three-column grid: label | A value | B value.

    Convention for the value columns:
      a="", b="..."  → item added in B (green)
      a="...", b=""  → item removed in B (dim + "(removed)")
      both non-empty → value changed (dim old, bold new)
    """
    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim", min_width=26)
    t.add_column(min_width=22)
    t.add_column()
    for label, a, b in rows:
        if not a and b:
            t.add_row(label, "", Text(b, style="green"))
        elif a and not b:
            t.add_row(label, Text(a, style="dim"), Text("(removed)", style="dim"))
        else:
            t.add_row(label, Text(a, style="dim"), Text(b, style="bold"))
    return t


# ── per-section diff helpers ──────────────────────────────────────────────────

def _diff_context(a: MissionRecord, b: MissionRecord) -> list[tuple]:
    rows = []
    for label, va, vb in [
        ("Goal",        a.intent.goal,              b.intent.goal),
        ("Location",    a.intent.location_name,     b.intent.location_name),
        ("Operator",    a.identity.operator_name,   b.identity.operator_name),
        ("Environment", a.intent.environment or "", b.intent.environment or ""),
        ("Notes",       a.intent.notes or "",       b.intent.notes or ""),
    ]:
        if va != vb:
            rows.append((label, va or "(none)", vb or "(none)"))
    return rows


def _diff_software(a: MissionRecord, b: MissionRecord) -> list[tuple]:
    rows = []
    if a.software.ros_distro != b.software.ros_distro:
        rows.append(("ROS distro",
                     a.software.ros_distro or "(unknown)",
                     b.software.ros_distro or "(unknown)"))

    for pkg in sorted(set(a.software.apt_ros_versions) |
                      set(b.software.apt_ros_versions)):
        va = a.software.apt_ros_versions.get(pkg)
        vb = b.software.apt_ros_versions.get(pkg)
        if va != vb:
            rows.append((pkg, va or "", vb or ""))

    ca = {c.name: c for c in a.software.docker_containers}
    cb = {c.name: c for c in b.software.docker_containers}
    for name in sorted(set(ca) | set(cb)):
        ia = (ca[name].digest or ca[name].image) if name in ca else None
        ib = (cb[name].digest or cb[name].image) if name in cb else None
        if ia != ib:
            def _short(s): return s[:48] + "…" if s and len(s) > 48 else (s or "")
            rows.append((f"container {name}", _short(ia), _short(ib)))

    return rows


def _diff_sensors(a: MissionRecord, b: MissionRecord) -> list[tuple]:
    rows = []
    sa = {s.sensor_id: s for s in a.sensors}
    sb = {s.sensor_id: s for s in b.sensors}
    for sid in sorted(set(sa) | set(sb)):
        s_a, s_b = sa.get(sid), sb.get(sid)
        if s_a is None:
            assert s_b is not None  # sid came from sb's keys
            rows.append((s_b.make_model, "",
                         "✓ detected" if s_b.detected_at_start else "configured"))
        elif s_b is None:
            rows.append((s_a.make_model,
                         "✓ detected" if s_a.detected_at_start else "configured", ""))
        elif s_a.detected_at_start != s_b.detected_at_start:
            rows.append((s_a.make_model,
                         "✓ detected" if s_a.detected_at_start else "✗ not detected",
                         "✓ detected" if s_b.detected_at_start else "✗ not detected"))
    return rows


def _diff_graph(a: MissionRecord, b: MissionRecord) -> list[tuple]:
    rows: list[tuple] = []

    nodes_a, nodes_b = set(a.ros_graph.nodes), set(b.ros_graph.nodes)
    for n in sorted(nodes_a - nodes_b):
        rows.append((n, "running", ""))
    for n in sorted(nodes_b - nodes_a):
        rows.append((n, "", "running"))

    topics_a = {t.name for t in a.ros_graph.topics}
    topics_b = {t.name for t in b.ros_graph.topics}
    for t in sorted(topics_a - topics_b):
        rows.append((t, "published", ""))
    for t in sorted(topics_b - topics_a):
        rows.append((t, "", "published"))

    if len(rows) > _MAX_GRAPH_ROWS:
        overflow = len(rows) - _MAX_GRAPH_ROWS
        rows = rows[:_MAX_GRAPH_ROWS]
        rows.append((f"… and {overflow} more change{'s' if overflow != 1 else ''}",
                     "", ""))
    return rows


def _diff_recordings(a: MissionRecord, b: MissionRecord) -> list[tuple]:
    rows: list[tuple] = []

    dur_a = sum(bag.duration_s for bag in a.bags)
    dur_b = sum(bag.duration_s for bag in b.bags)
    if abs(dur_a - dur_b) > 1:
        rows.append(("Duration", humanize_duration(dur_a), humanize_duration(dur_b)))

    size_a = sum(bag.size_bytes for bag in a.bags)
    size_b = sum(bag.size_bytes for bag in b.bags)
    if size_a != size_b:
        rows.append(("Size", human_size(size_a), human_size(size_b)))

    warns_a = {w.plain_text for bag in a.bags for w in bag.health_warnings}
    warns_b = {w.plain_text for bag in b.bags for w in bag.health_warnings}
    total_a = sum(len(bag.health_warnings) for bag in a.bags)
    total_b = sum(len(bag.health_warnings) for bag in b.bags)
    if total_a != total_b:
        rows.append(("Warnings",
                     str(total_a) if total_a else "none",
                     str(total_b) if total_b else "none"))
    for text in sorted(warns_b - warns_a):
        rows.append(("", "", text))
    for text in sorted(warns_a - warns_b):
        rows.append(("", text, ""))

    return rows


# ── public entry point ────────────────────────────────────────────────────────

def show_diff(a: MissionRecord, b: MissionRecord,
              console: Console | None = None) -> None:
    console = console or Console()

    header = Table.grid(padding=(0, 2))
    header.add_column(style="bold cyan", width=2)
    header.add_column()
    header.add_row("A", _mission_label(a))
    header.add_row("B", _mission_label(b))

    sections = [
        ("Mission context", _diff_context(a, b)),
        ("Software",        _diff_software(a, b)),
        ("Sensors",         _diff_sensors(a, b)),
        ("ROS graph",       _diff_graph(a, b)),
        ("Recordings",      _diff_recordings(a, b)),
    ]
    changed = [(title, rows) for title, rows in sections if rows]

    if not changed:
        body = Group(header, Text(""),
                     Text("No differences found.", style="dim"))
    else:
        parts: list = [header]
        for title, rows in changed:
            parts.append(Text(""))
            parts.append(Rule(title, style="dim"))
            parts.append(_table(rows))
        body = Group(*parts)

    console.print(Panel(body, title="Mission diff", border_style="cyan"))


def _mission_summary(r: MissionRecord) -> dict:
    return {
        "mission_id": r.identity.mission_id,
        "created_at": r.identity.created_at.isoformat(),
        "operator": r.identity.operator_name,
        "goal": r.intent.goal,
        "location": r.intent.location_name,
    }


def diff_as_dict(a: MissionRecord, b: MissionRecord) -> dict:
    """Machine-readable form of the same diff show_diff() renders.

    `changes` holds only sections that differ; each change is the section's
    label plus the before/after values (as displayed: empty `a` = added in B,
    empty `b` = removed in B).
    """
    sections = {
        "mission_context": _diff_context(a, b),
        "software":        _diff_software(a, b),
        "sensors":         _diff_sensors(a, b),
        "ros_graph":       _diff_graph(a, b),
        "recordings":      _diff_recordings(a, b),
    }
    changes = {
        name: [{"label": label, "a": va, "b": vb} for label, va, vb in rows]
        for name, rows in sections.items() if rows
    }
    return {
        "mission_a": _mission_summary(a),
        "mission_b": _mission_summary(b),
        "changes": changes,
    }
