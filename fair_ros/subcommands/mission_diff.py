"""ros2 fair diff — compare two saved missions."""

import json
from pathlib import Path

from rich.console import Console

from fair_ros.archive import index
from fair_ros.manifest.schema import MissionRecord
from fair_ros.subcommands import VerbExtension, _configure_logging
from fair_ros.ui import diff as diff_ui
from fair_ros.utils import paths


class DiffError(Exception):
    pass


def _resolve(identifier: str) -> Path:
    """Map a user-supplied identifier to an archive directory.

    Accepts (in order of precedence):
      - a positive integer  →  Nth most recent mission (1 = newest)
      - a filesystem path   →  must contain mission_record.json
      - a mission ID string →  looked up in the index
    """
    try:
        n = int(identifier)
        if n < 1:
            raise DiffError(f"Mission number must be 1 or higher (got {n}).")
        rows, total = index.query(limit=n)
        if n > len(rows):
            raise DiffError(
                f"There {'is' if total == 1 else 'are'} only {total} saved "
                f"mission{'s' if total != 1 else ''}; {n} is out of range.")
        return Path(rows[n - 1]["archive_path"])
    except ValueError:
        pass

    p = Path(identifier)
    if p.is_dir() and (p / "mission_record.json").is_file():
        return p

    rows, _ = index.query(limit=10_000)
    for row in rows:
        if row["mission_id"] == identifier:
            return Path(row["archive_path"])

    raise DiffError(
        f"Can't find a mission matching '{identifier}'. "
        "Use a number (1 = most recent), an archive path, or a mission ID "
        "(e.g. m-20260612-140258-9f3a).")


def _load(path: Path) -> MissionRecord:
    record_file = path / "mission_record.json"
    if not record_file.is_file():
        raise DiffError(
            f"{path} doesn't look like a mission archive "
            "(no mission_record.json found).")
    try:
        return MissionRecord.model_validate(
            json.loads(record_file.read_text()))
    except Exception as exc:
        raise DiffError(f"Could not read mission record at {path}: {exc}") from exc


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
        record_a = _load(_resolve(a_id))
        record_b = _load(_resolve(b_id))
    except DiffError as exc:
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
