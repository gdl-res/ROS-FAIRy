"""ros2 fair list — table of saved missions from the SQLite index."""

import json
from datetime import datetime

from rich.console import Console
from rich.table import Table

from fair_ros.archive import index
from fair_ros.subcommands import VerbExtension, _configure_logging
from fair_ros.ui.review import human_size
from fair_ros.utils import paths
from fair_ros.utils.topic_health import humanize_duration


def _fmt_date(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime(
            "%Y-%m-%d %H:%M")
    except ValueError:
        return iso


_QUALITY_CELL = {
    "degraded": "[yellow]partial[/yellow]",
    "poor": "[red]poor[/red]",
}


def _quality_cell(value: str | None) -> str:
    return _QUALITY_CELL.get(value or "", "")


def run(args, console: Console | None = None) -> int:
    _configure_logging(getattr(args, "debug", False))
    console = console or Console()
    as_json = getattr(args, "json", False)

    if not paths.index_db_path().is_file():
        if as_json:
            print(json.dumps({"missions": [], "total": 0, "shown": 0},
                             indent=2))
        else:
            console.print("No missions have been saved on this robot yet.")
        return 0

    rows, total = index.query(
        operator=getattr(args, "operator", None),
        location=getattr(args, "location", None),
        since=getattr(args, "since", None),
        until=getattr(args, "until", None),
        quality=getattr(args, "quality", None),
        limit=getattr(args, "limit", 20) or 20)

    if as_json:
        # Rows are already plain dicts of index columns (all JSON-native).
        print(json.dumps(
            {"missions": rows, "total": total, "shown": len(rows)}, indent=2))
        return 0

    if not rows:
        console.print("No missions found.")
        return 0

    show_path = getattr(args, "path", False)
    table = Table(border_style="dim")
    table.add_column("Date")
    table.add_column("Mission")
    table.add_column("Location")
    table.add_column("Operator")
    table.add_column("Duration", justify="right")
    table.add_column("Size", justify="right")
    table.add_column("⚠", justify="right")
    table.add_column("Data")
    if show_path:
        table.add_column("Path")
    for row in rows:
        goal = row["goal"]
        if len(goal) > 40:
            goal = goal[:39] + "…"
        cells = [
            _fmt_date(row["created_at"]),
            goal,
            row["location"],
            row["operator"],
            humanize_duration(row["duration_s"]) if row["duration_s"] else "",
            human_size(row["size_bytes"]) if row["size_bytes"] else "",
            str(row["warning_count"]) if row["warning_count"] else "",
            _quality_cell(row.get("data_quality")),
        ]
        if show_path:
            cells.append(row["archive_path"])
        table.add_row(*cells)
    console.print(table)
    if total > len(rows):
        console.print(f"Showing {len(rows)} of {total} missions")
    return 0


class ListVerb(VerbExtension):
    """List the missions saved on this robot."""

    def add_arguments(self, parser, cli_name):
        parser.add_argument("--debug", action="store_true",
                            help="verbose logging to stderr (for engineers)")
        parser.add_argument("--operator", help="filter by operator name")
        parser.add_argument("--location", help="filter by location")
        parser.add_argument("--since", metavar="YYYY-MM-DD",
                            help="only missions on or after this date")
        parser.add_argument("--until", metavar="YYYY-MM-DD",
                            help="only missions up to this date")
        parser.add_argument("--quality", choices=["ok", "degraded", "poor"],
                            help="only missions with this data-quality verdict")
        parser.add_argument("--limit", type=int, default=20,
                            help="maximum rows to show (default 20)")
        parser.add_argument("--path", action="store_true",
                            help="also show archive directory paths")
        parser.add_argument("--json", action="store_true",
                            help="machine-readable output for scripts")

    def main(self, *, args):
        return run(args)
