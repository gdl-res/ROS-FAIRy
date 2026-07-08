"""Detect rosbag2 recorder processes started outside the spool.

The watchdog's inotify only sees the spool, where ``ros2 fair mission_record``
records. A plain ``ros2 bag record`` in another terminal lands in the operator's
cwd and is invisible to it. This module scans ``/proc`` for a live rosbag2
*recorder* process and resolves where it is writing — pure ``/proc`` reading, no
ROS and no sourced environment needed, so it stays version-agnostic
(specs/watchdog.md, "Foreign-bag detection").

``scan()`` is injected into the :class:`~fair_ros.watchdog.watchdog.Watchdog` so
tests can fake it; the real implementation never raises (a transient
``/proc`` read race yields a partial result, never a crash).
"""

import logging
import os
from pathlib import Path
from typing import TypedDict

from fair_ros.utils import ros_env

log = logging.getLogger("fair_ros.watchdog.recorder_scan")

STORAGE_SUFFIXES = (".db3", ".mcap")

# Injectable for tests: point at a fake proc tree instead of the real /proc.
PROC = Path("/proc")

# Recorder pids already reported as unresolvable, so the warning appears once
# per recorder in the journal rather than on every poll tick.
_reported_unresolved: set[int] = set()


class FoundRecorder(TypedDict):
    pid: int
    output_dir: Path
    discovery: dict[str, str]


def _read_cmdline(pid: str) -> list[str]:
    try:
        raw = (PROC / pid / "cmdline").read_bytes()
    except OSError:
        return []
    return [tok for tok in raw.decode("utf-8", "replace").split("\0") if tok]


def _is_record_cmd(argv: list[str]) -> bool:
    """True when argv is a ``... bag record ...`` invocation (not play/info/...).

    The verb immediately after ``bag`` is the authoritative discriminator, so an
    output dir or topic named like another verb (e.g. ``-o info``) is not
    mistaken for it.
    """
    try:
        bag_i = argv.index("bag")
    except ValueError:
        return False
    verb = argv[bag_i + 1] if bag_i + 1 < len(argv) else ""
    return verb == "record"


def _output_arg(argv: list[str]) -> str | None:
    """The value of ``-o`` / ``--output`` (space- or ``=``-separated), if any."""
    for i, tok in enumerate(argv):
        if tok in ("-o", "--output"):
            return argv[i + 1] if i + 1 < len(argv) else None
        if tok.startswith("--output="):
            return tok.split("=", 1)[1]
        if tok.startswith("-o="):
            return tok.split("=", 1)[1]
    return None


def _proc_cwd(pid: str) -> Path | None:
    try:
        return Path(os.readlink(PROC / pid / "cwd"))
    except OSError:
        return None


def _fd_output(pid: str) -> Path | None:
    """The bag directory holding a storage file the recorder has open.

    rosbag2 keeps its ``.db3``/``.mcap`` open for the whole recording, so the
    open-fd table is the authoritative answer to "where is it writing" — no
    argv parsing, no cwd guessing, and it resolves the instant recording
    starts. Unreadable fds (permissions, races) fall through to the
    argv/cwd heuristic in :func:`_resolve_output`.
    """
    try:
        fds = list((PROC / pid / "fd").iterdir())
    except OSError:
        return None
    for fd in fds:
        try:
            target = os.readlink(fd)
        except OSError:
            continue
        # Non-file fds read back as "socket:[...]" etc. and a deleted file as
        # "/path (deleted)" — neither ends with a storage suffix.
        if target.endswith(STORAGE_SUFFIXES):
            bag_dir = Path(target).parent
            if bag_dir.is_dir():
                return bag_dir
    return None


def _discovery_env(pid: str) -> dict[str, str]:
    """DDS discovery keys from the recorder's own environment.

    Only :data:`ros_env.SESSION_ADOPT_KEYS` are returned — never loader paths —
    so adopting them into the root watchdog cannot load code (same rule as
    ``session.env``). The recorder is on the partition we want to harvest, so its
    environment is the authoritative source.
    """
    try:
        raw = (PROC / pid / "environ").read_bytes()
    except OSError:
        return {}
    env: dict[str, str] = {}
    for entry in raw.decode("utf-8", "replace").split("\0"):
        key, sep, val = entry.partition("=")
        if sep and key in ros_env.SESSION_ADOPT_KEYS:
            env[key] = val
    return env


def _is_active_bag(bag_dir: Path) -> bool:
    """A directory currently being recorded: storage file present, no metadata.

    rosbag2 writes ``metadata.yaml`` only on close, so its absence (with a
    storage file present) marks a live recording — and lets the existing
    finalise machinery take over once it appears.
    """
    try:
        if (bag_dir / "metadata.yaml").is_file():
            return False
        return any(f.name.endswith(STORAGE_SUFFIXES) for f in bag_dir.iterdir())
    except OSError:
        return False


def _resolve_output(argv: list[str], cwd: Path) -> Path | None:
    """The bag directory the recorder is writing into, or None if not yet known."""
    arg = _output_arg(argv)
    if arg is not None:
        bag_dir = Path(arg)
        if not bag_dir.is_absolute():
            bag_dir = cwd / bag_dir
        return bag_dir if _is_active_bag(bag_dir) else None
    # No -o: rosbag2 creates rosbag2_<timestamp>/ in the cwd. Pick the active one.
    try:
        candidates = [d for d in cwd.glob("rosbag2_*")
                      if d.is_dir() and _is_active_bag(d)]
    except OSError:
        return None
    return max(candidates, key=lambda d: d.stat().st_mtime) if candidates else None


def record_pids() -> list[int]:
    """Pids of live ``bag record`` processes, by cmdline alone.

    cmdline is world-readable, so this works without root — ``mission_status``
    uses it to report a recorder the (unprivileged) scan cannot resolve.
    """
    try:
        pids = [p for p in os.listdir(PROC) if p.isdigit()]
    except OSError:
        return []
    return [int(p) for p in pids if _is_record_cmd(_read_cmdline(p))]


def scan() -> list[FoundRecorder]:
    """Every live rosbag2 recorder whose output directory can be resolved.

    Resolution order: the recorder's own open storage fd (authoritative,
    instant), then the argv/cwd heuristic. A recorder that matches but cannot
    be resolved is logged once so the journal explains the miss.
    """
    found: list[FoundRecorder] = []
    for pid_int in record_pids():
        pid = str(pid_int)
        argv = _read_cmdline(pid)
        if not argv:  # exited between the pid sweep and this read
            continue
        bag_dir = _fd_output(pid)
        if bag_dir is None:
            cwd = _proc_cwd(pid)
            bag_dir = _resolve_output(argv, cwd) if cwd is not None else None
        if bag_dir is None:
            if pid_int not in _reported_unresolved:
                _reported_unresolved.add(pid_int)
                log.warning(
                    "recorder pid %s matched (%s, cwd %s) but its output "
                    "directory could not be resolved; recording will not be "
                    "captured", pid, " ".join(argv), _proc_cwd(pid))
            continue
        _reported_unresolved.discard(pid_int)
        found.append(FoundRecorder(pid=pid_int,
                                   output_dir=bag_dir.resolve(),
                                   discovery=_discovery_env(pid)))
    return found


def pid_alive(pid: int) -> bool:
    """Whether a recorder process is still running (used as a finalise hint)."""
    return (PROC / str(pid)).exists()
