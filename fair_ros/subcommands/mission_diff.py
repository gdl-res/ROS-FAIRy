"""ros2 fair diff — compare two saved missions."""

import json

from rich.console import Console

from fair_ros.archive import locate
from fair_ros.subcommands import VerbExtension, _configure_logging
from fair_ros.ui import diff as diff_ui
from fair_ros.utils import paths


def run(args, console: Console | None = None) -> int:
    _configure_logging(getattr(args, "debug", False))
    console = console or Console()

    if not paths.index_db_path().is_file():
        console.print("No missions have been saved on this robot yet.")
        return 1

    a_id: str | None = getattr(args, "mission_a", None)
    b_id: str | None = getattr(args, "mission_b", None)

    if a_id is None and b_id is None:
        a_id, b_id = "2", "1"
    elif b_id is None:
        console.print("[red]Provide either no arguments (compares the two most "
                      "recent missions) or two identifiers.[/red]")
        return 1
    assert a_id is not None and b_id is not None  # both set or returned above

    try:
        record_a = locate.load_record(locate.resolve_archive(a_id))
        record_b = locate.load_record(locate.resolve_archive(b_id))
    except locate.LocateError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    if getattr(args, "json", False):
        print(json.dumps(diff_ui.diff_as_dict(record_a, record_b), indent=2))
        return 0

    diff_ui.show_diff(record_a, record_b, console=console)
    return 0


class DiffVerb(VerbExtension):
    """Compare two missions and show what changed between them."""

    def add_arguments(self, parser, cli_name):
        parser.add_argument(
            "mission_a", nargs="?",
            help="older mission: number (2 = second newest), archive path, or "
                 "mission ID. Defaults to the second most recent mission.")
        parser.add_argument(
            "mission_b", nargs="?",
            help="newer mission: number (1 = newest), archive path, or mission "
                 "ID. Defaults to the most recent mission.")
        parser.add_argument(
            "--json", action="store_true",
            help="machine-readable output for scripts")
        parser.add_argument(
            "--debug", action="store_true",
            help="verbose logging to stderr (for engineers)")

    def main(self, *, args):
        return run(args)
