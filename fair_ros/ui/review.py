"""mission_close summary panel and save/discard confirmation (specs/cli.md)."""

from rich.console import Console, Group
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table
from rich.text import Text

from fair_ros.manifest.schema import MissionRecord
from fair_ros.utils.topic_health import humanize_duration


def human_size(size_bytes: int) -> str:
    if size_bytes >= 1e9:
        return f"{size_bytes / 1e9:.1f} GB"
    if size_bytes >= 1e6:
        return f"{size_bytes / 1e6:.0f} MB"
    return f"{max(size_bytes, 0) / 1e3:.0f} kB"


def show_summary(record: MissionRecord, harvest_warnings: list[str],
                 console: Console | None = None) -> None:
    console = console or Console()
    facts = Table.grid(padding=(0, 2))
    facts.add_column(style="bold")
    facts.add_column()
    facts.add_row("Mission", record.intent.goal)
    facts.add_row("Where", record.intent.location_name)
    facts.add_row("When",
                  record.identity.created_at.astimezone().strftime(
                      "%A %d %B %Y, %H:%M"))
    facts.add_row("Operator", record.identity.operator_name)
    if record.robot:
        facts.add_row("Robot", f"{record.robot.name} "
                               f"({record.robot.platform})")
    total_s = sum(b.duration_s or 0 for b in record.bags)
    total_bytes = sum(b.size_bytes for b in record.bags)
    n = len(record.bags)
    # When no bag has a measurable duration, don't claim "0 seconds".
    length = (humanize_duration(total_s)
              if any(b.duration_s for b in record.bags) else "length unknown")
    facts.add_row("Recording",
                  f"{n} recording{'s' if n != 1 else ''}, "
                  f"{length}, {human_size(total_bytes)}")

    warned_sensors = {w.sensor_id for b in record.bags
                      for w in b.health_warnings if w.sensor_id}
    sensor_lines = []
    for sensor in record.sensors:
        ok = sensor.detected_at_start and sensor.sensor_id not in \
            warned_sensors
        glyph, style = ("✓", "green") if ok else ("⚠", "yellow")
        sensor_lines.append(Text(f" {glyph} {sensor.make_model}",
                                 style=style))

    warnings = list(harvest_warnings)
    warnings += [w.plain_text for b in record.bags for w in b.health_warnings]
    body: list = [facts]
    if sensor_lines:
        body += [Text(""), Text("Sensors", style="bold"), *sensor_lines]
    if warnings:
        body += [Text(""), Text("Things worth knowing", style="bold")]
        body += [Text(f" ⚠ {w}", style="yellow") for w in warnings]
    console.print(Panel(Group(*body), title="Mission summary",
                        border_style="cyan"))


def confirm_save(console: Console | None = None) -> str:
    """Returns 'save', 'discard', or 'keep' (leave spool untouched)."""
    console = console or Console()
    if Confirm.ask("Save this mission?", default=True, console=console):
        return "save"
    if Confirm.ask("Throw away this recording and all its data?",
                   default=False, console=console):
        return "discard"
    return "keep"
