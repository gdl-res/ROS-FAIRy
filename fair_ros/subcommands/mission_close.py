"""ros2 fair mission_close — the single save/discard decision."""

import shutil
import sys

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm

from fair_ros.archive import assembler
from fair_ros.manifest import builder, validator
from fair_ros.subcommands import VerbExtension, _configure_logging
from fair_ros.ui import briefing, review
from fair_ros.utils import fsio, paths
from fair_ros.watchdog import watchdog as wd


def _recording_in_progress() -> bool:
    state = wd.read_state()
    if state is None or state.get("state") != "RECORDING":
        return False
    from fair_ros.ui.status import _pid_alive
    return _pid_alive(state.get("pid"))


def _salvage_bags(harvest: dict | None) -> dict | None:
    """If the watchdog never finalised, build bag records ourselves.

    Dashcam principle: closed bag directories sitting in the spool must be
    archivable even if the assistant was down the whole time.
    """
    known = {b["path"] for b in (harvest or {}).get("bags", [])}
    spool_bags = sorted(p for p in paths.bags_dir().glob("*") if p.is_dir()) \
        if paths.bags_dir().is_dir() else []
    missing = [b for b in spool_bags if str(b) not in known]
    if not missing:
        return harvest
    if harvest is None:
        harvest = builder.compose_harvest(
            None, None, None, None, None,
            {m: "failed" for m in builder.HARVEST_MODULES})
    fsio.atomic_write_json(paths.harvest_json_path(), harvest)
    for bag_dir in missing:
        if (bag_dir / "metadata.yaml").is_file() or \
                any(f.suffix in (".db3", ".mcap") for f in bag_dir.iterdir()):
            wd.append_bag_record(bag_dir)
    return builder.load_spool()[0]


def _discard_spool() -> None:
    if paths.bags_dir().is_dir():
        for bag in paths.bags_dir().glob("*"):
            shutil.rmtree(bag, ignore_errors=True)
    for f in (paths.harvest_json_path(), paths.mission_context_path(),
              paths.session_env_path()):
        f.unlink(missing_ok=True)


def run(args, console: Console | None = None) -> int:
    _configure_logging(getattr(args, "debug", False))
    console = console or Console()

    interrupted = assembler.find_interrupted_staging()
    if interrupted is not None:
        resume = Confirm.ask(
            "A previous save was interrupted but the data is safe. "
            "Finish saving it now?", default=True, console=console)
        if resume:
            try:
                final = assembler.resume_commit(interrupted)
                console.print(Panel(f"Mission saved: [bold]{final.name}"
                                    f"[/bold]", border_style="green"))
            except assembler.AssemblyError as exc:
                console.print(f"[red]{exc}[/red]")
                return 1
            return 0

    if _recording_in_progress():
        console.print("[yellow]It looks like recording is still in "
                      "progress. Stop it first (Ctrl-C in the recording "
                      "window), then run this again.[/yellow]")
        return 1

    harvest, context = builder.load_spool()
    harvest = _salvage_bags(harvest)
    if not (harvest or {}).get("bags"):
        console.print("There's nothing recorded yet.")
        return 1
    assert harvest is not None  # a None harvest has no bags, handled above

    missing = validator.missing_user_fields(context)
    if missing:
        answers = briefing.ask_missing(missing, console=console)
        if context is None:
            context = builder.new_mission_context(
                operator_name=answers.get("operator_name", ""),
                goal=answers.get("goal", ""),
                location_name=answers.get("location_name", ""))
        else:
            for field, value in answers.items():
                section = "identity" if field == "operator_name" else "intent"
                context.setdefault(section, {})[field] = value
        fsio.atomic_write_json(paths.mission_context_path(), context)

    # missing_user_fields(None) always reports all required fields, so a None
    # context above is always replaced before reaching here.
    assert context is not None
    existing_notes = (context.get("intent") or {}).get("notes")
    note_arg = getattr(args, "note", None)
    if note_arg is not None:
        new_notes = note_arg.strip() or None
    elif sys.stdin.isatty():
        new_notes = briefing.ask_notes(console, default=existing_notes)
    else:
        # Non-interactive (piped/scripted): keep whatever notes exist rather
        # than blocking on a prompt nobody can answer.
        new_notes = existing_notes
    if new_notes != existing_notes:
        context.setdefault("intent", {})["notes"] = new_notes
        fsio.atomic_write_json(paths.mission_context_path(), context)

    try:
        record = builder.build(harvest, context)
    except builder.ManifestError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    from fair_ros.manifest import quality as quality_mod
    quality = quality_mod.assess(record, harvest)
    record.provenance.data_quality = quality.level

    from fair_ros.archive import duplicates
    dup_msgs = [duplicates.describe(record, row)
                for row in duplicates.find_similar(record)]

    review.show_summary(record, builder.harvest_level_warnings(harvest),
                        console=console, quality=quality, duplicates=dup_msgs)
    decision = review.confirm_save(
        console=console, risky=quality.level == quality_mod.POOR)

    if decision == "save":
        try:
            with Progress(SpinnerColumn(),
                          TextColumn("[progress.description]{task.description}"),
                          console=console, transient=True) as progress:
                task = progress.add_task("Saving mission…", total=None)
                final = assembler.assemble(
                    record, harvest,
                    progress=lambda msg: progress.update(task,
                                                         description=msg))
        except assembler.AssemblyError as exc:
            console.print(f"[red]{exc}[/red]")
            return 1
        console.print(Panel(f"Mission saved: [bold]{final.name}[/bold]",
                            border_style="green"))
        return 0

    if decision == "discard":
        _discard_spool()
        console.print("Recording discarded.")
        return 0

    console.print("Nothing was changed — the recording is still in the "
                  "spool.")
    return 0


class MissionCloseVerb(VerbExtension):
    """Review the finished mission and decide: save it or discard it."""

    def add_arguments(self, parser, cli_name):
        parser.add_argument(
            "--debug", action="store_true",
            help="verbose logging to stderr (for engineers)")
        parser.add_argument(
            "--note", metavar="TEXT",
            help="post-mission notes (skips the interactive prompt)")

    def main(self, *, args):
        return run(args)
