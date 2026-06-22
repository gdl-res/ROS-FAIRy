"""Atomic file write helpers.

harvest.json and watchdog.state must never be observable in a torn state
(specs/watchdog.md): write to a sibling temp file, fsync, rename.
"""

import hashlib
import json
import os
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.rename(tmp, path)


def atomic_write_json(path: Path, document: Any) -> None:
    atomic_write_text(path, json.dumps(document, indent=2) + "\n")


def dir_size_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def sha256_file(path: Path) -> str:
    """Streaming SHA-256 hex digest of a file (constant memory)."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
