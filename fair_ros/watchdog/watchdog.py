"""The fair-ros watchdog: the always-on "dashcam" context recorder.

Behaviour is specified in specs/watchdog.md. Watches the spool bag directory
via inotify, harvests context when a recording starts, finalises bag records
when it stops. Never archives — that is the operator's decision at
``ros2 fair mission_close``.

Testability: the inotify object and the clock are injectable, and
``run_pipeline``/``step`` are callable synchronously, so tests drive the state
machine with fabricated events (no real ROS, Docker, or robot needed).
"""

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fair_ros.manifest import builder
from fair_ros.utils import fsio, paths, topic_health

log = logging.getLogger("fair_ros.watchdog")

BAG_INACTIVITY_S = 30
RCLPY_TIMEOUT_S = 5
DOCKER_TIMEOUT_S = 10
PIP_TIMEOUT_S = 30
HARDWARE_CMD_TIMEOUT_S = 10
HARDWARE_TOTAL_TIMEOUT_S = 60
ROS2_CLI_TIMEOUT_S = 20
PARAM_DUMP_BUDGET_S = 60
ROS_RETRY_INTERVAL_S = 60
HEARTBEAT_S = 60

STORAGE_SUFFIXES = (".db3", ".mcap")

IDLE, RECORDING, FINALISING = "IDLE", "RECORDING", "FINALISING"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_storage_file(name: str) -> bool:
    return name.endswith(STORAGE_SUFFIXES)


def run_pipeline() -> dict[str, Any]:
    """Run all harvest modules in spec order; never raises.

    Returns the composed harvest.json document (without bags).
    """
    from fair_ros.harvest import (
        docker_info,
        hardware_devices,
        python_env,
        robot_identity,
        ros_descriptions,
        ros_graph,
        system_info,
    )

    status: dict[str, str] = {}
    results: dict[str, Any] = {}

    def attempt(name: str, fn: Callable[[], dict]) -> None:
        try:
            results[name] = fn()
            status[name] = "ok"
        except Exception as exc:
            results[name] = None
            status[name] = "failed"
            log.warning("harvest module %s failed: %s", name, exc)

    attempt("robot_identity", robot_identity.harvest)
    attempt("system_info", system_info.harvest)

    attempt("python_env", python_env.harvest)
    if status["python_env"] == "ok":
        status["python_env"] = (results["python_env"] or {}).get("status", "ok")

    attempt("hardware_devices", hardware_devices.harvest)
    if status["hardware_devices"] == "ok":
        status["hardware_devices"] = \
            (results["hardware_devices"] or {}).get("status", "ok")

    attempt("ros_graph", ros_graph.harvest)
    attempt("docker_info", docker_info.harvest)
    if status["docker_info"] == "ok" and \
            not results["docker_info"]["available"]:
        status["docker_info"] = "skipped"
    attempt("ros_descriptions",
            lambda: ros_descriptions.harvest(RCLPY_TIMEOUT_S))
    if status["ros_descriptions"] == "ok" and \
            results["ros_descriptions"]["robot_description"] is None and \
            results["ros_descriptions"]["tf_static"] is None:
        status["ros_descriptions"] = "timeout"

    return builder.compose_harvest(
        identity=results["robot_identity"],
        system=results["system_info"],
        graph=results["ros_graph"],
        docker=results["docker_info"],
        descriptions=results["ros_descriptions"],
        harvest_status=status,
        python_env=results["python_env"],
        hardware_devices=results["hardware_devices"],
    )


class Watchdog:
    def __init__(self, inotify=None, clock: Callable[[], float] = time.monotonic,
                 pipeline: Callable[[], dict] = run_pipeline,
                 harvest_in_thread: bool = True):
        if inotify is None:
            from inotify_simple import INotify
            inotify = INotify()
        self.ino = inotify
        self.clock = clock
        self.pipeline = pipeline
        self.harvest_in_thread = harvest_in_thread

        self.state = IDLE
        self.since = _now_iso()
        self.active_bag_dir: Path | None = None
        self.queued_bags: list[Path] = []
        self.last_bag_event: float | None = None
        self.last_bag_event_iso: str | None = None
        self._w1: int | None = None
        self._w2: int | None = None
        self._wd_dirs: dict[int, Path] = {}
        self._candidate_dirs: set[Path] = set()
        self._next_retry: float | None = None
        self._next_heartbeat: float = self.clock() + HEARTBEAT_S
        self._harvest_lock = threading.Lock()
        self._stop = threading.Event()

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        from inotify_simple import flags
        bags = paths.bags_dir()
        bags.mkdir(parents=True, exist_ok=True, mode=0o775)
        self._w1 = self.ino.add_watch(
            str(bags), flags.CREATE | flags.MOVED_TO)
        self.recover()
        self.write_state()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        self.start()
        while not self._stop.is_set():
            self.step(timeout_ms=1000)

    # -- recovery (specs/watchdog.md "Restart recovery") ------------------

    def recover(self) -> None:
        harvest_doc, _ = builder.load_spool()
        finalised = {b["path"] for b in (harvest_doc or {}).get("bags", [])}
        for bag_dir in sorted(p for p in paths.bags_dir().iterdir()
                              if p.is_dir()):
            has_storage = any(_is_storage_file(f.name)
                              for f in bag_dir.iterdir())
            has_meta = (bag_dir / "metadata.yaml").is_file()
            if str(bag_dir) in finalised:
                continue
            if has_storage and not has_meta:
                log.info("resuming RECORDING for %s after restart", bag_dir)
                self._enter_recording(bag_dir)
            elif has_meta:
                log.info("finalising %s left over from before restart",
                         bag_dir)
                self._finalise(bag_dir)

    # -- event loop --------------------------------------------------------

    def step(self, timeout_ms: int = 1000) -> None:
        """One loop iteration: drain events, then service timers."""
        for event in self.ino.read(timeout=timeout_ms):
            self._handle_event(event)
        self._service_timers()

    def _handle_event(self, event) -> None:
        from inotify_simple import flags
        mask, name = event.mask, event.name
        if event.wd == self._w1:
            if mask & flags.ISDIR and mask & (flags.CREATE | flags.MOVED_TO):
                new_dir = paths.bags_dir() / name
                self._watch_candidate(new_dir)
                self._promote_candidate(new_dir)
            return

        bag_dir = self._wd_dirs.get(event.wd)
        if bag_dir is None:
            return
        if _is_storage_file(name) and mask & flags.CREATE:
            if self.state == IDLE and bag_dir in self._candidate_dirs:
                self._enter_recording(bag_dir)
            elif self.state == RECORDING and bag_dir != self.active_bag_dir \
                    and bag_dir not in self.queued_bags:
                log.warning("second bag %s appeared while recording %s; "
                            "queued", bag_dir, self.active_bag_dir)
                self.queued_bags.append(bag_dir)
        if bag_dir == self.active_bag_dir and name != "metadata.yaml":
            self._touch_activity()
        if name == "metadata.yaml" and mask & flags.CLOSE_WRITE and \
                bag_dir == self.active_bag_dir:
            self._finalise(bag_dir)

    def _service_timers(self) -> None:
        now = self.clock()
        if self.state == RECORDING:
            if self.last_bag_event is not None and \
                    now - self.last_bag_event >= BAG_INACTIVITY_S:
                log.info("bag inactive for %ss, finalising", BAG_INACTIVITY_S)
                self._finalise(self.active_bag_dir)
                return
            if self._next_retry is not None and now >= self._next_retry:
                self._maybe_retry_ros()
            if now >= self._next_heartbeat:
                self._next_heartbeat = now + HEARTBEAT_S
                self.write_state()

    # -- transitions -------------------------------------------------------

    def _watch_candidate(self, bag_dir: Path) -> None:
        from inotify_simple import flags
        try:
            wd = self.ino.add_watch(
                str(bag_dir),
                flags.CREATE | flags.MODIFY | flags.CLOSE_WRITE)
        except OSError as exc:
            log.warning("cannot watch %s: %s", bag_dir, exc)
            return
        self._wd_dirs[wd] = bag_dir
        self._candidate_dirs.add(bag_dir)

    def _promote_candidate(self, bag_dir: Path) -> None:
        """Catch a storage file that already existed when we armed the watch.

        inotify only reports events that happen *after* ``add_watch``, so a
        bag whose first chunk lands in the race window between the directory
        appearing and W2 being armed (or a finished bag dir moved into the
        spool) would otherwise never trigger RECORDING and never be harvested.
        Scan once on arm and apply the same IDLE→enter / RECORDING→queue logic
        the live CREATE event would have.
        """
        try:
            has_storage = any(_is_storage_file(f.name)
                              for f in bag_dir.iterdir())
        except OSError:
            return
        if not has_storage:
            return
        if self.state == IDLE and bag_dir in self._candidate_dirs:
            log.info("storage already present in %s when armed", bag_dir)
            self._enter_recording(bag_dir)
        elif self.state == RECORDING and bag_dir != self.active_bag_dir \
                and bag_dir not in self.queued_bags:
            log.warning("second bag %s already had data when seen; queued",
                        bag_dir)
            self.queued_bags.append(bag_dir)

    def _enter_recording(self, bag_dir: Path) -> None:
        if bag_dir not in self._wd_dirs.values():
            self._watch_candidate(bag_dir)
        self._candidate_dirs.discard(bag_dir)
        self.state = RECORDING
        self.since = _now_iso()
        self.active_bag_dir = bag_dir
        self._touch_activity()
        log.info("recording detected: %s", bag_dir)
        self.write_state()
        self._run_harvest()

    def _finalise(self, bag_dir: Path | None) -> None:
        if bag_dir is None:
            return
        self.state = FINALISING
        self.write_state()
        try:
            self._append_bag_record(bag_dir)
        except Exception:
            log.exception("failed to finalise %s", bag_dir)
        self._unwatch(bag_dir)
        self.state = IDLE
        self.since = _now_iso()
        self.active_bag_dir = None
        self.last_bag_event = None
        self.last_bag_event_iso = None
        log.info("finalised %s", bag_dir)
        self.write_state()
        # A recording that started while we were busy takes over now.
        while self.queued_bags:
            queued = self.queued_bags.pop(0)
            if queued.is_dir() and any(_is_storage_file(f.name)
                                       for f in queued.iterdir()):
                self._enter_recording(queued)
                break

    def _unwatch(self, bag_dir: Path) -> None:
        for wd, known in list(self._wd_dirs.items()):
            if known == bag_dir:
                try:
                    self.ino.rm_watch(wd)
                except OSError:
                    pass
                del self._wd_dirs[wd]

    def _touch_activity(self) -> None:
        self.last_bag_event = self.clock()
        self.last_bag_event_iso = _now_iso()

    # -- harvest -----------------------------------------------------------

    def _run_harvest(self) -> None:
        if self.harvest_in_thread:
            threading.Thread(target=self._harvest_once, daemon=True).start()
        else:
            self._harvest_once()

    def _harvest_once(self) -> None:
        with self._harvest_lock:
            doc = self.pipeline()
            self._save_harvest(doc)
            status = doc["provenance"]["harvest_status"]
            if status.get("ros_graph") in ("failed", "timeout") or \
                    status.get("ros_descriptions") in ("failed", "timeout"):
                self._next_retry = self.clock() + ROS_RETRY_INTERVAL_S
            else:
                self._next_retry = None
            self.write_state()

    def _maybe_retry_ros(self) -> None:
        """Re-run the full pipeline; cheap modules are cheap, ROS may be up now."""
        log.info("retrying harvest (ROS was unreachable)")
        self._next_retry = self.clock() + ROS_RETRY_INTERVAL_S
        self._run_harvest()

    def _save_harvest(self, doc: dict) -> None:
        """Write harvest.json, preserving bag records already finalised."""
        existing, _ = builder.load_spool()
        if existing and existing.get("bags"):
            doc = {**doc, "bags": existing["bags"]}
            if existing.get("provenance", {}).get("harvested_at"):
                doc["provenance"]["harvested_at"] = \
                    existing["provenance"]["harvested_at"]
        fsio.atomic_write_json(paths.harvest_json_path(), doc)

    def _append_bag_record(self, bag_dir: Path) -> None:
        append_bag_record(bag_dir)

    # -- state file ----------------------------------------------------------

    def write_state(self) -> None:
        harvest_doc, _ = builder.load_spool()
        status = (harvest_doc or {}).get("provenance", {}).get(
            "harvest_status", {})
        fsio.atomic_write_json(paths.watchdog_state_path(), {
            "version": 1,
            "pid": os.getpid(),
            "state": self.state,
            "since": self.since,
            "heartbeat_at": _now_iso(),
            "active_bag_dir": str(self.active_bag_dir)
            if self.active_bag_dir else None,
            "last_bag_event_at": self.last_bag_event_iso,
            "harvest_status": status,
        })


def append_bag_record(bag_dir: Path) -> None:
    """Finalise one bag into harvest.json (also used by mission_close to
    salvage bags the watchdog never saw)."""
    harvest_doc, _ = builder.load_spool()
    if harvest_doc is None:
        harvest_doc = builder.compose_harvest(
            None, None, None, None, None,
            {m: "failed" for m in builder.HARVEST_MODULES})
    sensors = harvest_doc.get("sensors", [])
    warnings = topic_health.analyse_bag(bag_dir, sensors)
    meta = topic_health.parse_bag_metadata(bag_dir)
    if meta is not None:
        start = datetime.fromtimestamp(meta["start_s"], tz=timezone.utc)
        end = datetime.fromtimestamp(
            meta["start_s"] + meta["duration_s"], tz=timezone.utc)
        bag = {
            "path": str(bag_dir),
            "storage_format": meta["storage_identifier"],
            "size_bytes": fsio.dir_size_bytes(bag_dir),
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "duration_s": meta["duration_s"],
            "message_count": meta["message_count"],
            "topics": [
                {"name": t["name"], "type": t["type"],
                 "message_count": t["message_count"],
                 "avg_frequency_hz": (
                     round(t["message_count"] / meta["duration_s"], 3)
                     if meta["duration_s"] > 0 else None)}
                for t in meta["topics"]],
            "health_warnings": warnings,
        }
    else:
        # Hard crash mid-write: recover what the filesystem still knows.
        files = [f for f in bag_dir.rglob("*") if f.is_file()]
        mtimes = [f.stat().st_mtime for f in files] or [time.time()]
        bag = {
            "path": str(bag_dir),
            "storage_format": "unknown",
            "size_bytes": fsio.dir_size_bytes(bag_dir),
            "start_time": datetime.fromtimestamp(
                min(mtimes), tz=timezone.utc).isoformat(),
            "end_time": datetime.fromtimestamp(
                max(mtimes), tz=timezone.utc).isoformat(),
            "duration_s": max(mtimes) - min(mtimes),
            "message_count": 0,
            "topics": [],
            "health_warnings": warnings,
        }
    harvest_doc.setdefault("bags", []).append(bag)
    harvest_doc.setdefault("provenance", {})["harvested_at"] = _now_iso()
    fsio.atomic_write_json(paths.harvest_json_path(), harvest_doc)


def read_state() -> dict | None:
    """For mission_status: the state file, or None if absent/unreadable."""
    path = paths.watchdog_state_path()
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    Watchdog().run()


if __name__ == "__main__":
    main()
