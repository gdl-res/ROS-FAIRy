"""Pluggable rosbag2 storage readers (distro-agnostic groundwork).

CLAUDE.md principle 5 keeps the core portable across ROS 2 distros. One place
that still couples to a backend is timestamp-level health analysis: it needs
each message's receive time, and where those live differs by storage format.
sqlite3 keeps them in a ``messages(timestamp, topic_id)`` table; MCAP keeps
them in chunked binary records. This module hides that behind a single reader
interface so callers (``utils/topic_health``) never branch on format.

Status:
  - ``sqlite3`` (``.db3``): implemented.
  - ``mcap`` (``.mcap``): declared extension point. Jazzy's *default* rosbag2
    storage is MCAP, so giving ``McapReader.topic_timestamps`` a real body
    immediately enables gap detection on stock Jazzy bags with no change to
    any caller.

Adding a backend: implement a reader with ``storage_id``, ``supported = True``,
and ``topic_timestamps``, then register an instance in ``_READERS``.
"""

import logging
import sqlite3
from pathlib import Path
from typing import Protocol, runtime_checkable

log = logging.getLogger("fair_ros.utils.bag_storage")


class BagStorageUnsupported(Exception):
    """A reader exists for the format but cannot extract timestamps yet."""


@runtime_checkable
class BagStorageReader(Protocol):
    """Reads per-message receive timestamps out of one rosbag2 storage format."""

    storage_id: str
    supported: bool

    def topic_timestamps(self, bag_dir: Path,
                         rel_paths: list[str]) -> dict[str, list[float]]:
        """Topic name -> ascending message receive-timestamps, in seconds."""
        ...


class SqliteReader:
    """rosbag2 sqlite3 storage: timestamps live in the ``messages`` table."""

    storage_id = "sqlite3"
    supported = True

    def topic_timestamps(self, bag_dir: Path,
                         rel_paths: list[str]) -> dict[str, list[float]]:
        series: dict[str, list[float]] = {}
        db_files = [bag_dir / p for p in rel_paths if str(p).endswith(".db3")]
        if not db_files:
            db_files = sorted(bag_dir.glob("*.db3"))
        for db_file in db_files:
            if not db_file.is_file():
                continue
            try:
                con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
                try:
                    rows = con.execute(
                        "SELECT topics.name, messages.timestamp FROM messages "
                        "JOIN topics ON messages.topic_id = topics.id"
                    ).fetchall()
                finally:
                    con.close()
            except sqlite3.Error:
                continue
            for name, ts in rows:
                series.setdefault(name, []).append(ts / 1e9)
        for stamps in series.values():
            stamps.sort()
        return series


class McapReader:
    """Extension point: MCAP per-message timestamp reading (not yet built).

    Jazzy records MCAP by default. To implement, read each ``.mcap`` file's
    message records and group ``log_time`` (nanoseconds) by channel/topic,
    mirroring ``SqliteReader``'s return shape. Suggested approach: the optional
    ``mcap`` package, imported locally so the core stays dependency-light, and
    raise ``BagStorageUnsupported`` if it is not installed. Once this returns
    real data, set ``supported = True`` and gap detection works on Jazzy's
    default storage with no caller changes.
    """

    storage_id = "mcap"
    supported = False

    def topic_timestamps(self, bag_dir: Path,
                         rel_paths: list[str]) -> dict[str, list[float]]:
        raise BagStorageUnsupported(
            "MCAP per-message timestamp reading is not implemented yet; "
            "metadata-level health checks still apply.")


_READERS: dict[str, BagStorageReader] = {
    reader.storage_id: reader for reader in (SqliteReader(), McapReader())
}


def get_reader(storage_id: str) -> BagStorageReader | None:
    """Return a reader for ``storage_id``, or None for an unknown format."""
    return _READERS.get(storage_id)


def supports_timestamps(storage_id: str) -> bool:
    """True if per-message timestamp analysis is available for this format."""
    reader = _READERS.get(storage_id)
    return reader is not None and reader.supported
