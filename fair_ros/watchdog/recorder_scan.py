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
# `ros2 bag <verb>` verbs that are NOT a recording — never adopt these.
_NON_RECORD_VERBS = frozenset(
    {"play", "info", "convert", "reindex", "list"})


class FoundRecorder(TypedDict):
    pid: int
    output_dir: Path
    discovery: dict[str, str]


def _read_cmdline(pid: str) -> list[str]:
    try:
        raw = Path("/proc", pid, "cmdline").read_bytes()
    except OSError:
        return []
    return [tok for tok in raw.decode("utf-8", "replace").split("\0") if tok]


def _is_record_cmd(argv: list[str]) -> bool:
    """True when argv is a ``... bag record ...`` invocation (not play/info/...)."""
    try:
        bag_i = argv.index("bag")
    except ValueError:
        return False
    verb = argv[bag_i + 1] if bag_i + 1 < len(argv) else ""
    return verb == "record" and not (_NON_RECORD_VERBS & set(argv))


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
        return Path(os.readlink(f"/proc/{pid}/cwd"))
    except OSError:
        return None


def _discovery_env(pid: str) -> dict[str, str]:
    """DDS discovery keys from the recorder's own environment.

    Only :data:`ros_env.SESSION_ADOPT_KEYS` are returned — never loader paths —
    so adopting them into the root watchdog cannot load code (same rule as
    ``session.env``). The recorder is on the partition we want to harvest, so its
    environment is the authoritative source.
    """
    try:
        raw = Path("/proc", pid, "environ").read_bytes()
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


def scan() -> list[FoundRecorder]:
    """Every live rosbag2 recorder whose output directory can be resolved."""
    found: list[FoundRecorder] = []
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return found
    for pid in pids:
        argv = _read_cmdline(pid)
        if not _is_record_cmd(argv):
            continue
        cwd = _proc_cwd(pid)
        if cwd is None:
            continue
        bag_dir = _resolve_output(argv, cwd)
        if bag_dir is None:
            continue
        found.append(FoundRecorder(pid=int(pid),
                                   output_dir=bag_dir.resolve(),
                                   discovery=_discovery_env(pid)))
    return found


def pid_alive(pid: int) -> bool:
    """Whether a recorder process is still running (used as a finalise hint)."""
    return Path(f"/proc/{pid}").exists()
