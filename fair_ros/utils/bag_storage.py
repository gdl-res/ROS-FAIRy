"""Pluggable rosbag2 storage readers (distro-agnostic groundwork).

CLAUDE.md principle 5 keeps the core portable across ROS 2 distros. One place
that still couples to a backend is timestamp-level health analysis: it needs
each message's receive time, and where those live differs by storage format.
sqlite3 keeps them in a ``messages(timestamp, topic_id)`` table; MCAP keeps
them in chunked binary records. This module hides that behind a single reader
interface so callers (``utils/topic_health``) never branch on format.

Status:
  - ``sqlite3`` (``.db3``): implemented, stdlib only.
  - ``mcap`` (``.mcap``, Jazzy's default): implemented via the optional
    ``mcap`` package. When that package is absent ``McapReader.supported`` is
    False and callers transparently fall back to metadata-level checks.

Adding a backend: implement a reader with ``storage_id``, ``supported = True``,
and ``topic_timestamps``, then register an instance in ``_READERS``.
"""

import importlib.util
import logging
import sqlite3
from pathlib import Path
from typing import Protocol, runtime_checkable

log = logging.getLogger("fair_ros.utils.bag_storage")


def _mcap_available() -> bool:
    """True if the optional ``mcap`` package is importable."""
    return importlib.util.find_spec("mcap") is not None


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
    """rosbag2 MCAP storage (Jazzy's default).

    Reads each ``.mcap`` file's message records and groups ``log_time``
    (the receive timestamp rosbag2 records, in nanoseconds) by channel/topic,
    mirroring ``SqliteReader``. Only record headers are read — messages are not
    deserialised — so no message-type packages are needed, just ``mcap`` for
    the container format. ``mcap`` is an optional dependency: when it is absent
    ``supported`` is False and callers fall back to metadata-level checks; if a
    caller invokes this anyway, ``BagStorageUnsupported`` is raised.
    """

    storage_id = "mcap"
    supported = _mcap_available()

    def topic_timestamps(self, bag_dir: Path,
                         rel_paths: list[str]) -> dict[str, list[float]]:
        try:
            from mcap.reader import make_reader
        except ImportError as exc:  # pragma: no cover - exercised without mcap
            raise BagStorageUnsupported(
                "the 'mcap' package is required to analyse MCAP bags") from exc

        series: dict[str, list[float]] = {}
        mcap_files = [bag_dir / p for p in rel_paths
                      if str(p).endswith(".mcap")]
        if not mcap_files:
            mcap_files = sorted(bag_dir.glob("*.mcap"))
        for mcap_file in mcap_files:
            if not mcap_file.is_file():
                continue
            try:
                with open(mcap_file, "rb") as handle:
                    for _schema, channel, message in \
                            make_reader(handle).iter_messages():
                        series.setdefault(channel.topic, []).append(
                            message.log_time / 1e9)
            except Exception:
                # A truncated/corrupt .mcap (e.g. crash mid-write) must not
                # break health analysis; salvage what the other files yield.
                log.warning("could not read mcap file %s", mcap_file)
                continue
        for stamps in series.values():
            stamps.sort()
        return series


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
