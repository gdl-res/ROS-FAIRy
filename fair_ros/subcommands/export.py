"""ros2 fair export — package a saved mission as one portable, checksummed file.

A mission archive is an RO-Crate *directory*; sharing it means an operator
hand-zips it (and hopes the transfer didn't corrupt anything). This makes that a
first-class, safe step: it bundles the crate into a single ``.zip`` (or ``.tar``)
with a top-level folder, writes a ``sha256sum``-compatible sidecar so the
recipient can prove the transfer, and refuses to clobber an existing file. The
crate's own per-file checksums still let the recipient run ``ros2 fair verify``
after unpacking.

Read-only with respect to the archive and index.
"""

import json
import os
import tarfile
import zipfile
from pathlib import Path

from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
)

from fair_ros.archive import locate
from fair_ros.subcommands import VerbExtension, _configure_logging
from fair_ros.ui.review import human_size
from fair_ros.utils import fsio

FORMATS = ("zip", "tar")
_EXT = {"zip": ".zip", "tar": ".tar"}


def _resolve_output(crate: Path, output: str | None, fmt: str) -> Path:
    """Where to write the bundle, from --output (file or dir) and format."""
    default_name = crate.name + _EXT[fmt]
    if not output:
        return Path.cwd() / default_name
    out = Path(output).expanduser()
    if out.is_dir() or output.endswith(os.sep):
        return out / default_name
    return out


def _bundle_files(crate: Path) -> list[tuple[Path, str]]:
    """(absolute path, arcname) for every file, under a top-level crate folder."""
    return [(f, f"{crate.name}/{f.relative_to(crate).as_posix()}")
            for f in sorted(crate.rglob("*")) if f.is_file()]


def _write_bundle(files: list[tuple[Path, str]], dest: Path, fmt: str,
                  console: Console) -> None:
    """Pack files into dest atomically (.part then rename). ZIP/TAR are stored
    uncompressed — bag data (MCAP, images) is already compressed, so deflating
    multi-GB recordings only burns CPU for no gain."""
    total = sum(f.stat().st_size for f, _ in files) or 1
    part = dest.with_name(dest.name + ".part")
    progress = Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(), DownloadColumn(), TimeRemainingColumn(),
        console=console, transient=True)
    try:
        with progress:
            task = progress.add_task("Packaging", total=total)
            if fmt == "zip":
                with zipfile.ZipFile(part, "w", zipfile.ZIP_STORED,
                                     allowZip64=True) as zf:
                    for src, arc in files:
                        zf.write(src, arc)
                        progress.advance(task, src.stat().st_size)
            else:
                with tarfile.open(part, "w") as tf:
                    for src, arc in files:
                        tf.add(str(src), arcname=arc, recursive=False)
                        progress.advance(task, src.stat().st_size)
        os.replace(part, dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise


def run(args, console: Console | None = None) -> int:
    _configure_logging(getattr(args, "debug", False))
    console = console or Console()

    fmt = getattr(args, "format", "zip")
    try:
        crate = locate.resolve_archive(getattr(args, "mission", None) or "1")
        record = locate.load_record(crate)
    except locate.LocateError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    dest = _resolve_output(crate, getattr(args, "output", None), fmt)
    if dest.exists() and not getattr(args, "force", False):
        console.print(f"[red]{dest} already exists. Choose another name with "
                      "--output, or pass --force to overwrite.[/red]")
        return 1

    # Integrity gate: warn loudly if the source archive doesn't verify, but let
    # the operator export it anyway (they asked, and a flawed copy can still be
    # worth shipping for diagnosis). verify must never block the export, so a
    # broken index or any other hiccup degrades to "unknown".
    try:
        from fair_ros.subcommands.verify import _overall, verify_archive
        verify_result = _overall(verify_archive(crate))
    except Exception:
        verify_result = "unknown"
    if verify_result == "fail":
        console.print("[yellow]Warning: this archive failed integrity checks "
                      "(run `ros2 fair verify` for details). Exporting "
                      "anyway.[/yellow]")

    files = _bundle_files(crate)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        _write_bundle(files, dest, fmt, console)
    except OSError as exc:
        console.print(f"[red]Couldn't write the bundle: "
                      f"{exc.strerror or exc}. Nothing was left behind.[/red]")
        return 1

    digest = fsio.sha256_file(dest)
    checksum_path = dest.with_name(dest.name + ".sha256")
    # sha256sum-compatible: recipient runs `sha256sum -c <file>.sha256`.
    fsio.atomic_write_text(checksum_path, f"{digest}  {dest.name}\n")

    size = dest.stat().st_size
    if getattr(args, "json", False):
        print(json.dumps({
            "mission_id": record.identity.mission_id,
            "source": str(crate),
            "bundle": str(dest),
            "format": fmt,
            "size_bytes": size,
            "sha256": digest,
            "checksum_file": str(checksum_path),
            "verify_result": verify_result,
        }, indent=2))
    else:
        console.print(
            f"[green]Exported[/green] {record.identity.mission_id} → "
            f"{dest} [dim]({human_size(size)})[/dim]")
        console.print(f"[dim]sha256: {digest}[/dim]")
        console.print(f"[dim]checksum saved to {checksum_path.name} — the "
                      "recipient can run `ros2 fair verify` after "
                      "unpacking.[/dim]")
    return 0


class ExportVerb(VerbExtension):
    """Package a saved mission into one portable file for sharing."""

    def add_arguments(self, parser, cli_name):
        parser.add_argument(
            "mission", nargs="?",
            help="mission to export: number (1 = newest), archive path, or "
                 "mission ID. Defaults to the most recent mission.")
        parser.add_argument(
            "--output", "-o",
            help="output file, or a directory to write <mission>.<ext> into "
                 "(default: current directory)")
        parser.add_argument(
            "--format", choices=FORMATS, default="zip",
            help="bundle format (default: zip)")
        parser.add_argument(
            "--force", action="store_true",
            help="overwrite the output file if it already exists")
        parser.add_argument(
            "--json", action="store_true",
            help="machine-readable output for scripts")
        parser.add_argument(
            "--debug", action="store_true",
            help="verbose logging to stderr (for engineers)")

    def main(self, *, args):
        return run(args)
