"""Resolve a user-supplied mission identifier to its archive on disk.

Shared by the verbs that take a mission argument (``diff``, ``verify``) so the
acceptance rules stay in one place. The archive directories' mission_record.json
files are the source of truth; the SQLite index is only used to map numbers and
IDs to paths.
"""

import json
from pathlib import Path

from fair_ros.archive import index
from fair_ros.manifest.schema import MissionRecord


class LocateError(Exception):
    """Plain-language failure resolving or loading a mission archive."""


def resolve_archive(identifier: str) -> Path:
    """Map a user-supplied identifier to an archive directory.

    Accepts (in order of precedence):
      - a positive integer  ->  Nth most recent mission (1 = newest)
      - a filesystem path   ->  must contain mission_record.json
      - a mission ID string ->  looked up in the index
    """
    try:
        n = int(identifier)
        if n < 1:
            raise LocateError(f"Mission number must be 1 or higher (got {n}).")
        rows, total = index.query(limit=n)
        if n > len(rows):
            raise LocateError(
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

    raise LocateError(
        f"Can't find a mission matching '{identifier}'. "
        "Use a number (1 = most recent), an archive path, or a mission ID "
        "(e.g. m-20260612-140258-9f3a).")


def load_record(path: Path) -> MissionRecord:
    """Load and validate ``mission_record.json`` from an archive directory."""
    record_file = path / "mission_record.json"
    if not record_file.is_file():
        raise LocateError(
            f"{path} doesn't look like a mission archive "
            "(no mission_record.json found).")
    try:
        return MissionRecord.model_validate(
            json.loads(record_file.read_text()))
    except Exception as exc:
        raise LocateError(
            f"Could not read mission record at {path}: {exc}") from exc
