"""Data-quality assessment for a finished mission.

The real-robot runs repeatedly produced archives that *looked* saved but were
nearly worthless — no ROS context captured, or bags with an unusable clock. The
tool happily archived them and the problem was only discovered later. This module
turns "is this mission actually any good?" into one explicit verdict, used to
gate the save decision at ``mission_close`` and to mark degraded missions in the
index so they are findable afterwards.

Levels:
  - ``ok``       — nothing important is missing.
  - ``degraded`` — usable, but with gaps (some sensors, some recordings).
  - ``poor``     — core content is missing (no ROS context, or no usable
                   recordings); saving should be a deliberate choice.
"""

from dataclasses import dataclass, field

from fair_ros.manifest.schema import MissionRecord

OK, DEGRADED, POOR = "ok", "degraded", "poor"


@dataclass
class Quality:
    level: str
    reasons: list[str] = field(default_factory=list)


def assess(record: MissionRecord, harvest: dict | None = None) -> Quality:
    """Grade a built MissionRecord. ``harvest`` adds harvest_status context."""
    status = ((harvest or {}).get("provenance") or {}).get("harvest_status", {})
    major: list[str] = []
    minor: list[str] = []

    # No software/settings captured from the robot — the empty-archive failure.
    if status.get("ros_graph") in ("failed", "timeout") \
            or not record.ros_graph.nodes:
        major.append("No software or settings were captured from the robot, so "
                     "the recording can't be reproduced (the assistant couldn't "
                     "reach ROS).")

    # Recordings with no usable timing (broken clock) — unplayable.
    bags = record.bags
    unusable = [b for b in bags if b.duration_s is None]
    if bags and len(unusable) == len(bags):
        major.append("The recordings have no usable timing — the clock was "
                     "wrong, so they may not play back correctly.")
    elif unusable:
        minor.append(f"{len(unusable)} of {len(bags)} recordings have unusable "
                     "timing (the clock was wrong).")

    if record.robot is None:
        major.append("This robot hasn't been set up, so there's no robot or "
                     "sensor information.")

    # Declared sensors the live graph confirmed absent at start. Sensors with
    # unknown liveness (None — graph unreachable) are not counted here; ones that
    # recorded no data are caught by the never_published check below.
    if record.sensors:
        not_detected = [s for s in record.sensors if s.detected_at_start is False]
        if not_detected and len(not_detected) == len(record.sensors):
            minor.append("None of the declared sensors were detected when "
                         "recording started.")
        elif not_detected:
            minor.append(f"{len(not_detected)} of {len(record.sensors)} "
                         "sensors weren't detected when recording started.")

    # Sensors that produced no data at all.
    silent = {w.sensor_id for b in bags for w in b.health_warnings
              if w.kind == "never_published" and w.sensor_id}
    if silent:
        minor.append(f"{len(silent)} sensor(s) produced no data at all.")

    level = POOR if major else DEGRADED if minor else OK
    return Quality(level=level, reasons=major + minor)
