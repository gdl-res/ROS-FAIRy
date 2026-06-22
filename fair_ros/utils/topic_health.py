"""Topic health analysis for recorded bags.

Produces the HealthWarning dicts defined in specs/data_model.md. Every warning
carries a pre-rendered ``plain_text`` sentence; UI layers must display that
string and never the raw numbers (CLAUDE.md UI rules).

Timestamp-level gap detection works on sqlite3 bags by querying the .db3
files directly. MCAP bags get metadata-level checks only (never_published);
their chunk format is not parsed in v1.
"""

import sqlite3
import statistics
from pathlib import Path
from typing import Any

import yaml

GAP_THRESHOLD_S = 1.0
# A gap must also dwarf the topic's own cadence, or slow topics (0.2 Hz
# diagnostics etc.) would warn on every message.
GAP_MEDIAN_FACTOR = 5.0
LOW_RATE_FRACTION = 0.25
LOW_RATE_WINDOW_S = 10.0
LOW_RATE_MIN_MESSAGES = 20

_FRIENDLY_TYPE = {
    "gps": "GPS",
    "lidar": "Lidar",
    "camera": "Camera",
    "imu": "Motion sensor (IMU)",
    "sonar": "Sonar",
}


def humanize_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 90:
        n = max(1, round(seconds))
        return f"{n} second{'s' if n != 1 else ''}"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours, minutes = divmod(minutes, 60)
    if minutes == 0:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{hours}h {minutes}m"


def parse_bag_metadata(bag_dir: Path) -> dict[str, Any] | None:
    """Parse rosbag2's metadata.yaml. None if absent/unreadable."""
    meta_path = bag_dir / "metadata.yaml"
    if not meta_path.is_file():
        return None
    try:
        raw = yaml.safe_load(meta_path.read_text())
        info = raw["rosbag2_bagfile_information"]
    except (yaml.YAMLError, KeyError, TypeError):
        return None
    start_ns = info.get("starting_time", {}).get("nanoseconds_since_epoch", 0)
    duration_ns = info.get("duration", {}).get("nanoseconds", 0)
    topics = []
    for entry in info.get("topics_with_message_count") or []:
        tm = entry.get("topic_metadata") or {}
        topics.append({
            "name": tm.get("name", ""),
            "type": tm.get("type", ""),
            "message_count": entry.get("message_count", 0),
        })
    return {
        "storage_identifier": info.get("storage_identifier", "sqlite3"),
        "start_s": start_ns / 1e9,
        "duration_s": duration_ns / 1e9,
        "message_count": info.get("message_count", 0),
        "topics": topics,
        "relative_file_paths": info.get("relative_file_paths") or [],
    }


def _friendly_name(sensor: dict | None, topic: str) -> str:
    if sensor is not None:
        return _FRIENDLY_TYPE.get(sensor.get("type", ""),
                                  sensor.get("make_model") or "A sensor")
    return f"One of the recorded data channels ({topic})"


def _signal_word(sensor: dict | None) -> str:
    return "signal" if sensor and sensor.get("type") == "gps" else "data"


def _topic_timestamps(bag_dir: Path, rel_paths: list[str]) -> dict[str, list[float]]:
    """Topic name -> sorted message timestamps (seconds) from sqlite3 storage."""
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
                    "JOIN topics ON messages.topic_id = topics.id").fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            continue
        for name, ts in rows:
            series.setdefault(name, []).append(ts / 1e9)
    for stamps in series.values():
        stamps.sort()
    return series


def _gap_warnings(topic: str, sensor: dict | None, stamps: list[float],
                  bag_start: float, bag_end: float) -> list[dict]:
    if len(stamps) < 2:
        return []
    intervals = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
    median = statistics.median(intervals)
    threshold = max(GAP_THRESHOLD_S, GAP_MEDIAN_FACTOR * median)
    who = _friendly_name(sensor, topic)
    what = _signal_word(sensor)
    warnings = []
    for prev, cur in zip(stamps, stamps[1:], strict=False):
        gap = cur - prev
        if gap <= threshold:
            continue
        offset = prev - bag_start
        warnings.append({
            "topic": topic,
            "sensor_id": sensor.get("sensor_id") if sensor else None,
            "kind": "gap",
            "start_offset_s": round(offset, 3),
            "duration_s": round(gap, 3),
            "plain_text": (
                f"{who} {what} was lost for {humanize_duration(gap)}, "
                f"starting {humanize_duration(offset)} in."),
        })
    trailing = bag_end - stamps[-1]
    if trailing > threshold:
        warnings.append({
            "topic": topic,
            "sensor_id": sensor.get("sensor_id") if sensor else None,
            "kind": "gap",
            "start_offset_s": round(stamps[-1] - bag_start, 3),
            "duration_s": round(trailing, 3),
            "plain_text": (
                f"{who} {what} stopped {humanize_duration(trailing)} before "
                f"the end of the recording and did not come back."),
        })
    return warnings


def _low_rate_warning(topic: str, sensor: dict | None, stamps: list[float],
                      duration_s: float) -> dict | None:
    if len(stamps) < LOW_RATE_MIN_MESSAGES or duration_s <= LOW_RATE_WINDOW_S:
        return None
    windows: dict[int, int] = {}
    t0 = stamps[0]
    for ts in stamps:
        windows[int((ts - t0) / LOW_RATE_WINDOW_S)] = \
            windows.get(int((ts - t0) / LOW_RATE_WINDOW_S), 0) + 1
    peak_rate = max(windows.values()) / LOW_RATE_WINDOW_S
    avg_rate = len(stamps) / duration_s
    if peak_rate <= 0 or avg_rate >= LOW_RATE_FRACTION * peak_rate:
        return None
    return {
        "topic": topic,
        "sensor_id": sensor.get("sensor_id") if sensor else None,
        "kind": "low_rate",
        "start_offset_s": None,
        "duration_s": None,
        "plain_text": (
            f"{_friendly_name(sensor, topic)} sent data much more slowly "
            f"than usual for most of the recording."),
    }


def analyse_bag(bag_dir: Path, sensors: list[dict] | None = None) -> list[dict]:
    """Return HealthWarning dicts for one bag directory.

    ``sensors`` is the declared sensor list from robot_identity (may be
    empty/None when the robot was never set up).
    """
    sensors = sensors or []
    by_topic = {s["topic"]: s for s in sensors}
    meta = parse_bag_metadata(bag_dir)
    warnings: list[dict] = []

    if meta is None:
        return [{
            "topic": "", "sensor_id": None, "kind": "never_published",
            "start_offset_s": None, "duration_s": None,
            "plain_text": "The recording ended unexpectedly and may be "
                          "incomplete.",
        }]

    recorded = {t["name"]: t["message_count"] for t in meta["topics"]}
    for sensor in sensors:
        if recorded.get(sensor["topic"], 0) == 0:
            warnings.append({
                "topic": sensor["topic"],
                "sensor_id": sensor["sensor_id"],
                "kind": "never_published",
                "start_offset_s": None,
                "duration_s": None,
                "plain_text": (
                    f"{_friendly_name(sensor, sensor['topic'])} produced no "
                    f"data at all during this recording."),
            })

    if meta["storage_identifier"] != "sqlite3":
        return warnings

    bag_start = meta["start_s"]
    bag_end = bag_start + meta["duration_s"]
    for topic, stamps in _topic_timestamps(
            bag_dir, meta["relative_file_paths"]).items():
        sensor = by_topic.get(topic)
        gaps = _gap_warnings(topic, sensor, stamps, bag_start, bag_end)
        warnings.extend(gaps)
        if not gaps:
            low = _low_rate_warning(topic, sensor, stamps, meta["duration_s"])
            if low:
                warnings.append(low)
    return warnings
