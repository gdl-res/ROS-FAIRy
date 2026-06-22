import io
from unittest import mock

from rich.console import Console

from fair_ros.manifest import builder
from fair_ros.ui import briefing, review, status


def _console():
    return Console(file=io.StringIO(), width=100, force_terminal=False)


def _record():
    harvest = builder.compose_harvest(
        identity={
            "robot": {"name": "Heron-02", "platform": "Heron USV",
                      "serial_number": "H02", "owner_organization": "Lab",
                      "owner_contact": "a@b.c"},
            "sensors": [{"sensor_id": "gps0", "type": "gps",
                         "make_model": "u-blox ZED-F9P", "topic": "/fix",
                         "frame_id": None, "calibration_ref": None}],
            "calibrations": [], "default_license": None},
        system={"hostname": "r1", "kernel": "Linux", "arch": "x86_64",
                "ros_distro": "jazzy", "apt_ros_versions": {}},
        graph={"captured_at": "2026-06-12T14:03:00+00:00", "nodes": [],
               "topics": [{"name": "/fix", "type": "t"}], "ros_packages": [],
               "parameters": {}, "complete": True},
        docker=None, descriptions=None,
        harvest_status={"robot_identity": "ok", "system_info": "ok",
                        "ros_graph": "ok", "ros_descriptions": "timeout",
                        "docker_info": "skipped"})
    harvest["bags"] = [{
        "path": "bags/rosbag2_0", "storage_format": "sqlite3",
        "size_bytes": 3_328_599_041,
        "start_time": "2026-06-12T14:03:00+00:00",
        "end_time": "2026-06-12T14:45:00+00:00",
        "duration_s": 2520.0, "message_count": 100, "topics": [],
        "health_warnings": [{
            "topic": "/fix", "sensor_id": "gps0", "kind": "gap",
            "start_offset_s": 720.0, "duration_s": 243.2,
            "plain_text": "GPS signal was lost for 4 minutes, starting "
                          "12 minutes in."}],
    }]
    context = builder.new_mission_context(
        operator_name="Jane Doe", goal="Survey eelgrass",
        location_name="Marsh Creek")
    return builder.build(harvest, context), harvest


def test_briefing_answers_mapping():
    answers = iter(["Jane Doe", "Survey eelgrass", "Marsh Creek", "", ""])
    with mock.patch.object(briefing.Prompt, "ask",
                           side_effect=lambda *a, **k: next(answers)):
        result = briefing.ask_briefing(console=_console())
    assert result == {"operator_name": "Jane Doe", "goal": "Survey eelgrass",
                      "location_name": "Marsh Creek", "environment": None,
                      "notes": None}


def test_briefing_reasks_required():
    answers = iter(["", "  ", "Jane"])
    with mock.patch.object(briefing.Prompt, "ask",
                           side_effect=lambda *a, **k: next(answers)):
        result = briefing.ask_missing(["operator_name"], console=_console())
    assert result == {"operator_name": "Jane"}


def test_summary_is_plain_language():
    record, harvest = _record()
    console = _console()
    review.show_summary(record, builder.harvest_level_warnings(harvest),
                        console=console)
    out = console.file.getvalue()
    assert "GPS signal was lost for 4 minutes" in out
    assert "Jane Doe" in out
    assert "1 recording, 42 minutes, 3.3 GB" in out
    assert "u-blox ZED-F9P" in out
    # no jargon or raw structures
    assert "/fix" not in out
    assert "243.2" not in out
    assert "{" not in out


def test_confirm_save_paths():
    with mock.patch.object(review.Confirm, "ask", side_effect=[True]):
        assert review.confirm_save(_console()) == "save"
    with mock.patch.object(review.Confirm, "ask", side_effect=[False, True]):
        assert review.confirm_save(_console()) == "discard"
    with mock.patch.object(review.Confirm, "ask", side_effect=[False, False]):
        assert review.confirm_save(_console()) == "keep"


def test_status_lines(fair_dirs):
    assert "not running" in status.assistant_line(None)
    dead = {"pid": 99999999, "state": "IDLE"}
    assert "not running" in status.assistant_line(dead)

    import os
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    alive = {"pid": os.getpid(), "state": "IDLE", "heartbeat_at": now,
             "since": now}
    assert "watching" in status.assistant_line(alive)
    alive["state"] = "RECORDING"
    assert status.assistant_line(alive).startswith("recording")
    alive["state"] = "FINALISING"
    assert "wrapping up" in status.assistant_line(alive)

    lines = status.harvest_lines({"harvest_status": {
        "ros_graph": "failed", "docker_info": "skipped",
        "system_info": "ok", "hardware_devices": "partial"}})
    assert "✗ software versions and settings — will keep trying" in lines
    assert "– container software (not used on this robot)" in lines
    assert "✓ computer details" in lines
    assert "⚠ connected hardware (partial)" in lines


def test_show_status_renders(fair_dirs):
    console = _console()
    status.show_status(None, None, console=console)
    out = console.file.getvalue()
    assert "not started yet" in out
    assert "fair-ros status" in out
