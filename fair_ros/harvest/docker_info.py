"""Snapshot of running Docker containers. Graceful no-op without Docker."""

import json
import subprocess
import time
from typing import Any

DOCKER_TIMEOUT_S = 10

_COMPOSE_PROJECT = "com.docker.compose.project"
_COMPOSE_FILES = "com.docker.compose.project.config_files"


def _run(args: list[str], timeout: float) -> str | None:
    try:
        result = subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def harvest() -> dict[str, Any]:
    """Return {docker_containers: [...], raw_inspect: [...], available: bool}.

    Never raises: any Docker problem yields an empty result with
    available=False so the watchdog records status 'skipped'.
    """
    empty = {"docker_containers": [], "raw_inspect": [], "available": False}
    deadline = time.monotonic() + DOCKER_TIMEOUT_S

    ps = _run(["ps", "-q"], timeout=DOCKER_TIMEOUT_S)
    if ps is None:
        return empty
    ids = [line.strip() for line in ps.splitlines() if line.strip()]
    if not ids:
        return {**empty, "available": True}

    inspect_out = _run(["inspect", *ids],
                       timeout=max(1.0, deadline - time.monotonic()))
    if inspect_out is None:
        return empty
    try:
        raw = json.loads(inspect_out)
    except json.JSONDecodeError:
        return empty

    containers = []
    for entry in raw:
        config = entry.get("Config") or {}
        labels = config.get("Labels") or {}
        # docker inspect puts RepoDigests on image objects, not containers;
        # for containers we re-resolve from .Image via a dedicated inspect.
        containers.append({
            "name": (entry.get("Name") or "").lstrip("/"),
            "image": config.get("Image") or "",
            "digest": _image_digest(entry.get("Image"), deadline),
            "compose_project": labels.get(_COMPOSE_PROJECT),
            "compose_file": labels.get(_COMPOSE_FILES),
        })
    return {"docker_containers": containers, "raw_inspect": raw,
            "available": True}


def _image_digest(image_id: str | None, deadline: float) -> str | None:
    if not image_id:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    out = _run(["inspect", "--format", "{{json .RepoDigests}}", image_id],
               timeout=remaining)
    if out is None:
        return None
    try:
        digests = json.loads(out)
    except json.JSONDecodeError:
        return None
    return digests[0] if isinstance(digests, list) and digests else None
