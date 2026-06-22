"""ros2 fair mission_start — the briefing wizard (specs/cli.md)."""

from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm

from fair_ros.manifest import builder
from fair_ros.subcommands import VerbExtension, _configure_logging
from fair_ros.ui import briefing
from fair_ros.utils import clock, fsio, paths
from fair_ros.watchdog import watchdog as wd


def _watchdog_alive() -> bool:
    state = wd.read_state()
    if state is None:
        return False
    from fair_ros.ui.status import _pid_alive
    return _pid_alive(state.get("pid"))


def _last_operator() -> str | None:
    try:
        from fair_ros.archive import index
        rows, _ = index.query(limit=1)
        return rows[0]["operator"] if rows else None
    except Exception:
        return None


def run(args, console: Console | None = None) -> int:
    _configure_logging(getattr(args, "debug", False))
    console = console or Console()

    if not _watchdog_alive():
        console.print("[yellow]Background recording assistant isn't running "
                      "— your answers will still be saved.[/yellow]")

    if clock.is_synchronized() is False:
        console.print(f"[yellow]{clock.WARNING}[/yellow]")

    context_path = paths.mission_context_path()
    if context_path.is_file():
        existing = builder.load_spool()[1]
        if existing:
            identity = existing.get("identity", {})
            when = identity.get("created_at", "")
            try:
                when = datetime.fromisoformat(when).astimezone().strftime(
                    "%d %B, %H:%M")
            except ValueError:
                pass
            replace = Confirm.ask(
                f"There's already an unfinished mission from {when} by "
                f"{identity.get('operator_name', 'someone')}. Start a new "
                f"one and replace it?", default=False, console=console)
            if not replace:
                return 0

    answers = briefing.ask_briefing(console=console,
                                    default_operator=_last_operator())
    context = builder.new_mission_context(
        operator_name=answers["operator_name"],
        goal=answers["goal"],
        location_name=answers["location_name"],
        environment=answers["environment"],
        notes=answers["notes"])
    paths.spool_dir().mkdir(parents=True, exist_ok=True)
    fsio.atomic_write_json(context_path, context)
    console.print(Panel("Mission briefing saved. Start recording with: "
                        "[bold]ros2 fair mission_record[/bold]",
                        border_style="green"))
    return 0


class MissionStartVerb(VerbExtension):
    """Answer five quick questions to describe the mission you're starting."""

    def add_arguments(self, parser, cli_name):
        parser.add_argument(
            "--debug", action="store_true",
            help="verbose logging to stderr (for engineers)")

    def main(self, *, args):
        return run(args)
