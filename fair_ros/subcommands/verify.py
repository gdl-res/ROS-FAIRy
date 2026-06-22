"""ros2 fair verify — re-check that a saved mission archive is intact.

Answers the question a data consumer actually has months later: "is this
archive still complete and unmodified, and does its metadata still parse?"
Checks, in plain language:

  - the mission record loads and validates against the schema;
  - ro-crate-metadata.json is well-formed JSON-LD (deep-loaded with the rocrate
    library when it is installed);
  - every file the crate references exists on disk;
  - each bag directory has its metadata and the storage files it lists;
  - each calibration file still matches the sha256 recorded at archive time;
  - the mission is registered in the local index.

Read-only: verify never modifies the archive or the index.
"""

import json
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from fair_ros.archive import index, locate
from fair_ros.subcommands import VerbExtension, _configure_logging
from fair_ros.utils import fsio, topic_health

OK, WARN, FAIL = "ok", "warn", "fail"
_GLYPH = {OK: "[green]✓[/green]", WARN: "[yellow]![/yellow]",
          FAIL: "[red]✗[/red]"}


def _is_external(ref: str) -> bool:
    return ref.startswith(("http://", "https://", "urn:", "#"))


def verify_archive(crate: Path) -> list[dict]:
    """Return a list of {status, title, detail} checks for one archive dir."""
    checks: list[dict] = []

    def add(status: str, title: str, detail: str = "") -> None:
        checks.append({"status": status, "title": title, "detail": detail})

    # 1. Mission record loads + validates (schema enforces required fields).
    try:
        record = locate.load_record(crate)
        add(OK, "Mission record is valid",
            f"mission {record.identity.mission_id}")
    except locate.LocateError as exc:
        add(FAIL, "Mission record is unreadable or invalid", str(exc))
        return checks  # nothing else is meaningful without the record

    # 2. RO-Crate metadata well-formed (deep-load with rocrate if present).
    crate_meta = crate / "ro-crate-metadata.json"
    graph: list[dict] = []
    if not crate_meta.is_file():
        add(FAIL, "RO-Crate metadata is missing", crate_meta.name)
    else:
        try:
            graph = json.loads(crate_meta.read_text()).get("@graph", [])
        except json.JSONDecodeError as exc:
            add(FAIL, "RO-Crate metadata is not valid JSON", str(exc))
        else:
            try:
                from rocrate.rocrate import ROCrate
                ROCrate(str(crate))
                add(OK, "RO-Crate metadata is valid JSON-LD")
            except ImportError:
                add(WARN, "RO-Crate metadata is valid JSON",
                    "install 'rocrate' for full JSON-LD validation")
            except Exception as exc:  # malformed JSON-LD
                add(FAIL, "RO-Crate metadata failed JSON-LD validation",
                    str(exc))

    # 3. Core files present.
    for rel in ("README.md", "harvest/harvest.json"):
        if (crate / rel).is_file():
            add(OK, f"{rel} present")
        else:
            add(WARN, f"{rel} is missing")

    # 4. Bags. When per-file checksums were recorded (>= 1.0) re-hash and
    #    compare for byte-level integrity; otherwise fall back to a structural
    #    check (metadata + the storage files metadata lists) for older archives.
    for bag in record.bags:
        name = Path(bag.path).name
        bag_dir = crate / bag.path
        if not bag_dir.is_dir():
            add(FAIL, f"Recording {bag.path} is missing")
            continue
        if bag.file_sha256:
            missing, modified = [], []
            for rel, expected in bag.file_sha256.items():
                f = bag_dir / rel
                if not f.is_file():
                    missing.append(rel)
                elif fsio.sha256_file(f) != expected:
                    modified.append(rel)
            if missing:
                add(FAIL, f"Recording {name} is missing data files",
                    ", ".join(missing))
            elif modified:
                add(FAIL, f"Recording {name} has been modified",
                    ", ".join(modified))
            else:
                add(OK, f"Recording {name} matches its checksums",
                    f"{len(bag.file_sha256)} file(s)")
            continue
        # Pre-1.0 archive: structural check only.
        meta = topic_health.parse_bag_metadata(bag_dir)
        if meta is None:
            add(FAIL, f"Recording {bag.path} has no readable metadata")
            continue
        missing = [f for f in meta["relative_file_paths"]
                   if not (bag_dir / f).is_file()]
        if missing:
            add(FAIL, f"Recording {bag.path} is missing data files",
                ", ".join(missing))
        else:
            add(WARN, f"Recording {name} is present (no checksums recorded)",
                f"{len(meta['relative_file_paths'])} storage file(s)")

    # 5. Calibrations: re-hash and compare to the recorded sha256.
    for cal in record.calibrations:
        if not cal.archived_path:
            continue
        cal_file = crate / cal.archived_path
        if not cal_file.is_file():
            add(FAIL, f"Calibration {cal.name} is missing", cal.archived_path)
        elif cal.sha256 and fsio.sha256_file(cal_file) != cal.sha256:
            add(FAIL, f"Calibration {cal.name} has been modified",
                "sha256 does not match the archived value")
        else:
            add(OK, f"Calibration {cal.name} matches its checksum")

    # 6. Every File entity the crate references exists on disk.
    missing_refs = []
    for entity in graph:
        types = entity.get("@type", [])
        types = types if isinstance(types, list) else [types]
        ref = entity.get("@id", "")
        if "File" in types and not _is_external(ref) \
                and not (crate / ref).exists():
            missing_refs.append(ref)
    if missing_refs:
        add(FAIL, "Some files referenced by the crate are missing",
            ", ".join(missing_refs))
    elif graph:
        add(OK, "All files referenced by the crate are present")

    # 7. Index registration (a cache, so only a warning when absent).
    rows, _ = index.query(limit=100_000)
    match = next((r for r in rows
                  if r["mission_id"] == record.identity.mission_id), None)
    if match is None:
        add(WARN, "Mission is not in the local index",
            "it won't appear in `ros2 fair list`; index.reindex() can fix this")
    elif Path(match["archive_path"]).resolve() != crate.resolve():
        add(WARN, "Index points at a different path for this mission",
            match["archive_path"])
    else:
        add(OK, "Mission is registered in the index")

    return checks


def _overall(checks: list[dict]) -> str:
    if any(c["status"] == FAIL for c in checks):
        return FAIL
    if any(c["status"] == WARN for c in checks):
        return WARN
    return OK


def _render(console: Console, crate: Path, checks: list[dict]) -> None:
    table = Table.grid(padding=(0, 1))
    table.add_column(width=1)
    table.add_column()
    for check in checks:
        detail = f" [dim]({check['detail']})[/dim]" if check["detail"] else ""
        table.add_row(_GLYPH[check["status"]], check["title"] + detail)
    overall = _overall(checks)
    title = {OK: "[green]PASS[/green]", WARN: "[yellow]PASS (with notes)[/yellow]",
             FAIL: "[red]FAIL[/red]"}[overall]
    border = {OK: "green", WARN: "yellow", FAIL: "red"}[overall]
    console.print(Panel(table, title=f"{title} — {crate.name}",
                        border_style=border))


def run(args, console: Console | None = None) -> int:
    _configure_logging(getattr(args, "debug", False))
    console = console or Console()

    identifier = getattr(args, "mission", None) or "1"
    try:
        crate = locate.resolve_archive(identifier)
    except locate.LocateError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    checks = verify_archive(crate)
    overall = _overall(checks)

    if getattr(args, "json", False):
        print(json.dumps({
            "archive": str(crate),
            "result": overall,
            "checks": checks,
        }, indent=2))
    else:
        _render(console, crate, checks)

    return 1 if overall == FAIL else 0


class VerifyVerb(VerbExtension):
    """Check that a saved mission archive is complete and unmodified."""

    def add_arguments(self, parser, cli_name):
        parser.add_argument(
            "mission", nargs="?",
            help="mission to verify: number (1 = newest), archive path, or "
                 "mission ID. Defaults to the most recent mission.")
        parser.add_argument(
            "--json", action="store_true",
            help="machine-readable output for scripts")
        parser.add_argument(
            "--debug", action="store_true",
            help="verbose logging to stderr (for engineers)")

    def main(self, *, args):
        return run(args)
