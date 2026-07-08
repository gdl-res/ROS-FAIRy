"""mission_status display (specs/cli.md). Read-only, instant, no prompts."""

from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from fair_ros.manifest import builder
from fair_ros.ui.review import human_size
from fair_ros.utils import fsio, paths
from fair_ros.utils.topic_health import humanize_duration
from fair_ros.watchdog import recorder_scan

STALE_HEARTBEAT_S = 300

_MODULE_LABELS = {
    "robot_identity": "robot identity",
    "system_info": "computer details",
    "python_env": "Python environment",
    "hardware_devices": "connected hardware",
    "ros_graph": "software versions and settings",
    "ros_descriptions": "robot description",
    "docker_info": "container software",
}


def _pid_alive(pid) -> bool:
    return isinstance(pid, int) and Path(f"/proc/{pid}").exists()


def assistant_line(state: dict | None) -> str:
    """One plain-language line about the watchdog."""
    if state is None or not _pid_alive(state.get("pid")):
        return "not running — recordings will still work, but background "\
               "details won't be captured"
    try:
        heartbeat = datetime.fromisoformat(state.get("heartbeat_at", ""))
        age = (datetime.now(timezone.utc) - heartbeat).total_seconds()
    except ValueError:
        age = None
    if state.get("state") == "RECORDING":
        if age is not None and age > STALE_HEARTBEAT_S:
            return "not responding — ask your engineer to check it"
        since = ""
        try:
            started = datetime.fromisoformat(state["since"]).astimezone()
            elapsed = (datetime.now(timezone.utc) -
                       datetime.fromisoformat(state["since"])).total_seconds()
            since = (f" (started {started.strftime('%H:%M')}, "
                     f"{humanize_duration(elapsed)} ago)")
        except (KeyError, ValueError):
            pass
        return f"recording{since}"
    if state.get("state") == "FINALISING":
        return "wrapping up the last recording"
    return "watching — ready for the next recording"


def outside_recordings(state: dict | None, recorders: list | None = None,
                       matched_pids: list[int] | None = None) -> dict:
    """What the recorder scan sees outside the spool, judged against the
    watchdog: is each live recording being captured, queued, already captured,
    or missed? ``recorders``/``matched_pids`` are injectable for tests.

    Run as the (unprivileged) operator, the scan may match a recorder by
    cmdline yet fail to resolve where it writes — those pids are reported
    separately rather than silently dropped, because the root watchdog may
    still see them.
    """
    if recorders is None:
        recorders = recorder_scan.scan()
    if matched_pids is None:
        matched_pids = recorder_scan.record_pids()
    harvest, _ = builder.load_spool()
    captured = {b["path"] for b in (harvest or {}).get("bags", [])}
    active = (state or {}).get("active_bag_dir")
    queued = set((state or {}).get("queued_bags") or [])
    entries = []
    for rec in recorders:
        path = str(rec["output_dir"])
        try:  # spool bags are the Recording row's business, not this one's
            if paths.bags_dir().resolve() in Path(path).parents:
                continue
        except OSError:
            pass
        if path == active:
            status = "capturing"
        elif path in queued:
            status = "queued"
        elif path in captured:
            status = "captured"
        else:
            status = "missed"
        entries.append({"path": path, "status": status})
    seen = {r["pid"] for r in recorders}
    return {"recordings": entries,
            "unresolved_pids": [p for p in matched_pids if p not in seen]}


_OUTSIDE_LABELS = {
    "capturing": "found it — capturing the details now",
    "queued": "noticed — will be captured when the current recording finishes",
    "captured": "captured — will be included when you close the mission",
    "missed": "NOT captured — if this doesn't change in a few seconds, "
              "ask your engineer to check the recording assistant",
}


def outside_lines(outside: dict) -> list[str]:
    lines = [f"{r['path']} — {_OUTSIDE_LABELS[r['status']]}"
             for r in outside["recordings"]]
    if outside["unresolved_pids"]:
        lines.append("a recording seems to be running, but this command "
                     "can't see where it saves — the background assistant "
                     "may still capture it")
    return lines


def harvest_lines(state: dict | None) -> list[str]:
    if not state or not state.get("harvest_status"):
        return []
    lines = []
    for module, result in state["harvest_status"].items():
        label = _MODULE_LABELS.get(module, module)
        if result == "ok":
            lines.append(f"✓ {label}")
        elif result == "partial":
            lines.append(f"⚠ {label} (partial)")
        elif result == "skipped":
            lines.append(f"– {label} (not used on this robot)")
        else:
            lines.append(f"✗ {label} — will keep trying")
    return lines


def show_status(state: dict | None, context: dict | None,
                console: Console | None = None,
                outside: dict | None = None) -> None:
    console = console or Console()
    outside = outside if outside is not None else outside_recordings(state)
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Assistant", assistant_line(state))

    if context:
        identity = context.get("identity", {})
        intent = context.get("intent", {})
        table.add_row("Briefing", f"{identity.get('operator_name', '?')} — "
                                  f"{intent.get('goal', '?')}")
    else:
        table.add_row("Briefing", "not started yet — run: "
                                  "ros2 fair mission_start")

    bags = sorted(p for p in paths.bags_dir().glob("*") if p.is_dir()) \
        if paths.bags_dir().is_dir() else []
    if state and state.get("active_bag_dir"):
        active = Path(state["active_bag_dir"])
        size = human_size(fsio.dir_size_bytes(active)) \
            if active.is_dir() else "?"
        table.add_row("Recording", f"{active.name} — {size} so far, growing")
    elif bags:
        total = sum(fsio.dir_size_bytes(b) for b in bags)
        table.add_row("Recordings waiting",
                      f"{len(bags)} ({human_size(total)}) — run: "
                      f"ros2 fair mission_close")
    else:
        table.add_row("Recording", "none")

    for line in outside_lines(outside):
        table.add_row("Outside recording", line)

    lines = harvest_lines(state)
    if lines:
        table.add_row("Context captured", "\n".join(lines))
    console.print(Panel(table, title="fair-ros status", border_style="cyan"))


def status_as_dict(state: dict | None, context: dict | None,
                   outside: dict | None = None) -> dict:
    """Machine-readable status for --json (the one sanctioned JSON output)."""
    bags = sorted(str(p) for p in paths.bags_dir().glob("*") if p.is_dir()) \
        if paths.bags_dir().is_dir() else []
    return {
        "assistant": assistant_line(state),
        "watchdog_state": state,
        "mission_context": context,
        "spool_bags": bags,
        "live_recorders":
            outside if outside is not None else outside_recordings(state),
    }
