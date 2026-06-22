import importlib.util
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from rich.console import Console

from fair_ros.manifest import builder
from fair_ros.subcommands import (
    doctor,
    export,
    list_missions,
    mission_close,
    mission_diff,
    mission_record,
    mission_start,
    mission_status,
    repair,
)
from fair_ros.subcommands import setup as setup_cmd
from fair_ros.utils import clock, fsio, paths
from tests.unit.test_archive import _spool


def _console():
    return Console(file=io.StringIO(), width=120, force_terminal=False)


ARGS = SimpleNamespace()


# --- mission_start -----------------------------------------------------------

def test_mission_start_writes_context(fair_dirs):
    answers = {"operator_name": "Jane", "goal": "Map the creek",
               "location_name": "Marsh Creek", "environment": None,
               "notes": None}
    console = _console()
    with mock.patch.object(mission_start.briefing, "ask_briefing",
                           return_value=answers):
        assert mission_start.run(ARGS, console=console) == 0
    context = json.loads(paths.mission_context_path().read_text())
    assert context["identity"]["operator_name"] == "Jane"
    assert context["identity"]["mission_id"].startswith("m-")
    assert "mission_record" in console.file.getvalue()


def test_mission_start_keeps_existing_when_declined(fair_dirs):
    existing = builder.new_mission_context("Sam", "Old goal", "Old place")
    fsio.atomic_write_json(paths.mission_context_path(), existing)
    with mock.patch.object(mission_start.Confirm, "ask",
                           return_value=False):
        assert mission_start.run(ARGS, console=_console()) == 0
    context = json.loads(paths.mission_context_path().read_text())
    assert context["identity"]["operator_name"] == "Sam"


# --- mission_record ----------------------------------------------------------

def test_mission_record_requires_ros2(fair_dirs):
    console = _console()
    with mock.patch.object(mission_record.shutil, "which",
                           return_value=None):
        assert mission_record.run(ARGS, console=console) == 1
    assert "can't find ROS 2" in console.file.getvalue()


def test_clock_is_synchronized_parsing():
    def result(val):
        return SimpleNamespace(returncode=0, stdout=val + "\n")
    with mock.patch.object(clock.subprocess, "run", return_value=result("yes")):
        assert clock.is_synchronized() is True
    with mock.patch.object(clock.subprocess, "run", return_value=result("no")):
        assert clock.is_synchronized() is False
    with mock.patch.object(clock.subprocess, "run",
                           side_effect=FileNotFoundError):
        assert clock.is_synchronized() is None


def test_mission_record_aborts_on_unsynced_clock(fair_dirs):
    _spool(fair_dirs)  # a mission context, so the briefing prompt is skipped
    console = _console()
    with mock.patch.object(mission_record.shutil, "which",
                           return_value="/usr/bin/ros2"), \
         mock.patch.object(mission_record.clock, "is_synchronized",
                           return_value=False), \
         mock.patch.object(mission_record.Confirm, "ask",
                           return_value=False) as ask, \
         mock.patch.object(mission_record.subprocess, "Popen") as popen:
        assert mission_record.run(ARGS, console=console) == 0
    ask.assert_called_once()          # the clock prompt
    popen.assert_not_called()         # recording never started


def test_build_record_command_default(fair_dirs):
    cmd = mission_record.build_record_command("/out")
    assert cmd == ["ros2", "bag", "record", "--all", "--output", "/out"]


def test_build_record_command_from_identity(fair_dirs, identity_yaml):
    text = identity_yaml.read_text() + \
        "recording:\n  topics: [/fix, /depth]\n  storage: mcap\n"
    identity_yaml.write_text(text)
    cmd = mission_record.build_record_command("/out")
    assert cmd == ["ros2", "bag", "record", "/fix", "/depth",
                   "--storage", "mcap", "--output", "/out"]


# --- mission_close -----------------------------------------------------------

def test_mission_close_nothing_recorded(fair_dirs):
    console = _console()
    assert mission_close.run(ARGS, console=console) == 1
    assert "nothing recorded" in console.file.getvalue()


def test_mission_close_blocks_while_recording(fair_dirs):
    _spool(fair_dirs)
    fsio.atomic_write_json(paths.watchdog_state_path(), {
        "pid": os.getpid(), "state": "RECORDING"})
    console = _console()
    assert mission_close.run(ARGS, console=console) == 1
    assert "still in progress" in console.file.getvalue()


def test_mission_close_save_flow(fair_dirs):
    _spool(fair_dirs)
    console = _console()
    with mock.patch.object(mission_close.review, "confirm_save",
                           return_value="save"):
        assert mission_close.run(ARGS, console=console) == 0
    out = console.file.getvalue()
    assert "Mission saved" in out
    archives = [p for p in paths.archive_dir().iterdir()
                if p.is_dir() and p.name != ".staging"]
    assert len(archives) == 1
    assert (archives[0] / "ro-crate-metadata.json").is_file()
    assert not paths.mission_context_path().exists()


def test_mission_close_discard_flow(fair_dirs):
    _spool(fair_dirs)
    console = _console()
    with mock.patch.object(mission_close.review, "confirm_save",
                           return_value="discard"):
        assert mission_close.run(ARGS, console=console) == 0
    assert "discarded" in console.file.getvalue()
    assert not any(paths.bags_dir().iterdir())
    assert not paths.harvest_json_path().exists()


def test_mission_close_keep_flow(fair_dirs):
    _spool(fair_dirs)
    console = _console()
    with mock.patch.object(mission_close.review, "confirm_save",
                           return_value="keep"):
        assert mission_close.run(ARGS, console=console) == 0
    assert "still in the spool" in console.file.getvalue()
    assert any(paths.bags_dir().iterdir())


def test_mission_close_gap_fill_briefing(fair_dirs):
    _spool(fair_dirs)
    paths.mission_context_path().unlink()
    answers = {"operator_name": "Sam", "goal": "Salvage run",
               "location_name": "Pier 4"}
    console = _console()
    with mock.patch.object(mission_close.briefing, "ask_missing",
                           return_value=answers), \
         mock.patch.object(mission_close.review, "confirm_save",
                           return_value="save"):
        assert mission_close.run(ARGS, console=console) == 0
    archives = [p for p in paths.archive_dir().iterdir()
                if p.is_dir() and p.name != ".staging"]
    record = json.loads(
        (archives[0] / "mission_record.json").read_text())
    assert record["identity"]["operator_name"] == "Sam"
    assert record["intent"]["location_name"] == "Pier 4"


def test_mission_close_salvages_unfinalised_bag(fair_dirs):
    # bags exist but the watchdog never wrote harvest.json
    _spool(fair_dirs)
    paths.harvest_json_path().unlink()
    console = _console()
    with mock.patch.object(mission_close.review, "confirm_save",
                           return_value="save"):
        assert mission_close.run(ARGS, console=console) == 0
    archives = [p for p in paths.archive_dir().iterdir()
                if p.is_dir() and p.name != ".staging"]
    record = json.loads((archives[0] / "mission_record.json").read_text())
    assert len(record["bags"]) == 1
    assert record["bags"][0]["message_count"] > 0
    # honest about the missing context
    assert "hasn't been set up" in console.file.getvalue()


# --- mission_status / list ----------------------------------------------------

def test_mission_status_json(fair_dirs, capsys):
    args = SimpleNamespace(json=True)
    assert mission_status.run(args, console=_console()) == 0
    data = json.loads(capsys.readouterr().out)
    assert "assistant" in data
    assert data["watchdog_state"] is None


def test_list_no_index(fair_dirs):
    console = _console()
    assert list_missions.run(SimpleNamespace(), console=console) == 0
    assert "No missions have been saved" in console.file.getvalue()


def test_list_shows_missions(fair_dirs):
    harvest, context = _spool(fair_dirs)
    record = builder.build(harvest, context)
    from fair_ros.archive import assembler
    assembler.assemble(record, harvest)

    console = _console()
    args = SimpleNamespace(operator=None, location=None, since=None,
                           until=None, limit=20, path=False)
    assert list_missions.run(args, console=console) == 0
    out = console.file.getvalue()
    assert "Jane Doe" in out
    assert "Survey eelgrass beds" in out
    assert "10 minutes" in out

    console = _console()
    args.operator = "nobody"
    assert list_missions.run(args, console=console) == 0
    assert "No missions found" in console.file.getvalue()


def test_list_json(fair_dirs, capsys):
    from fair_ros.archive import assembler
    harvest, context = _spool(fair_dirs)
    assembler.assemble(builder.build(harvest, context), harvest)

    args = SimpleNamespace(operator=None, location=None, since=None,
                           until=None, limit=20, path=False, json=True)
    assert list_missions.run(args, console=_console()) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["total"] == 1
    assert data["shown"] == 1
    assert data["missions"][0]["operator"] == "Jane Doe"
    assert data["missions"][0]["goal"] == "Survey eelgrass beds"


def test_list_json_no_index(fair_dirs, capsys):
    args = SimpleNamespace(json=True)
    assert list_missions.run(args, console=_console()) == 0
    data = json.loads(capsys.readouterr().out)
    assert data == {"missions": [], "total": 0, "shown": 0}


def test_diff_json(fair_dirs, capsys):
    from fair_ros.archive import assembler
    h1, c1 = _spool(fair_dirs)
    assembler.assemble(builder.build(h1, c1), h1)
    h2, c2 = _spool(fair_dirs)
    c2["intent"]["goal"] = "A different goal entirely"
    assembler.assemble(builder.build(h2, c2), h2)

    args = SimpleNamespace(mission_a="2", mission_b="1", json=True)
    assert mission_diff.run(args, console=_console()) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["mission_a"]["goal"] == "Survey eelgrass beds"   # older
    assert data["mission_b"]["goal"] == "A different goal entirely"  # newer
    goal_changes = data["changes"].get("mission_context", [])
    assert any(c["label"] == "Goal" for c in goal_changes)


# --- setup ---------------------------------------------------------------------

def test_setup_requires_root(fair_dirs):
    console = _console()
    with mock.patch.object(setup_cmd.os, "geteuid", return_value=1000):
        assert setup_cmd.run(ARGS, console=console) == 1
    assert "sudo" in console.file.getvalue()


def test_setup_ask_robot_validates_email(fair_dirs):
    answers = iter(["Heron-02", "Clearpath Heron", "H02", "Lab",
                    "not-an-email", "fleet@example.org"])
    with mock.patch.object(setup_cmd.Prompt, "ask",
                           side_effect=lambda *a, **k: next(answers)):
        config = setup_cmd.ask_robot(_console(), {})
    assert config["owner"]["contact_email"] == "fleet@example.org"


# --- repair ------------------------------------------------------------------

_MCAP = importlib.util.find_spec("mcap") is not None


@pytest.mark.skipif(not _MCAP, reason="mcap package not installed")
def test_repair_command_on_single_bad_bag(tmp_path):
    from tests.conftest import make_mcap_bag
    bad = make_mcap_bag(tmp_path / "rosbag2_bad",
                        {"/data": [float(i) for i in range(1, 31)]
                         + [1_750_000_000.0 + i * 0.5 for i in range(11)]})
    out = tmp_path / "out"
    args = SimpleNamespace(mission=str(bad), output=str(out), all=False,
                           duration=10.0, force=False, json=False)
    assert repair.run(args, console=_console()) == 0
    fixed = out / bad.name
    assert (fixed / "metadata.yaml").is_file() and list(fixed.glob("*.mcap"))


@pytest.mark.skipif(not _MCAP, reason="mcap package not installed")
def test_repair_command_skips_healthy_bag(tmp_path):
    from tests.conftest import make_mcap_bag
    good = make_mcap_bag(tmp_path / "rosbag2_ok",
                         {"/data": [1_750_000_000.0 + i * 0.1 for i in range(50)]})
    out = tmp_path / "out"
    console = _console()
    args = SimpleNamespace(mission=str(good), output=str(out), all=False,
                           duration=None, force=False, json=False)
    assert repair.run(args, console=console) == 0
    assert "nothing to repair" in console.file.getvalue()
    assert not out.exists()


def test_repair_command_unknown_target(fair_dirs):
    args = SimpleNamespace(mission="nope", output=None, all=False,
                           duration=None, force=False, json=False)
    assert repair.run(args, console=_console()) == 1


# --- data quality / degradation gate -----------------------------------------

def test_quality_ok_for_healthy_mission(fair_dirs):
    from fair_ros.manifest import quality
    harvest, context = _spool(fair_dirs)
    record = builder.build(harvest, context)
    assert quality.assess(record, harvest).level == quality.OK


def test_quality_poor_without_ros_context(fair_dirs):
    from fair_ros.manifest import quality
    harvest, context = _spool(fair_dirs)
    harvest["ros_graph"]["nodes"] = []
    harvest["provenance"]["harvest_status"]["ros_graph"] = "failed"
    record = builder.build(harvest, context)
    q = quality.assess(record, harvest)
    assert q.level == quality.POOR and any("software" in r for r in q.reasons)


def test_quality_poor_when_all_bags_unusable(fair_dirs):
    from fair_ros.manifest import quality
    harvest, context = _spool(fair_dirs)
    record = builder.build(harvest, context)
    for b in record.bags:
        b.duration_s = None
    assert quality.assess(record, harvest).level == quality.POOR


def test_quality_degraded_when_sensor_not_detected(fair_dirs):
    from fair_ros.manifest import quality
    harvest, context = _spool(fair_dirs)
    record = builder.build(harvest, context)
    for s in record.sensors:
        s.detected_at_start = False
    assert quality.assess(record, harvest).level == quality.DEGRADED


def test_mission_close_gates_poor_mission(fair_dirs):
    # Spool harvest looks like no ROS context was captured -> poor.
    harvest, _ = _spool(fair_dirs)
    harvest["ros_graph"]["nodes"] = []
    harvest["provenance"]["harvest_status"]["ros_graph"] = "failed"
    fsio.atomic_write_json(paths.harvest_json_path(), harvest)

    captured = {}

    def fake_confirm(console=None, *, risky=False):
        captured["risky"] = risky
        return "keep"

    with mock.patch.object(mission_close.review, "confirm_save", fake_confirm):
        assert mission_close.run(ARGS, console=_console()) == 0
    assert captured["risky"] is True


def test_mission_close_does_not_gate_healthy_mission(fair_dirs):
    _spool(fair_dirs)
    captured = {}

    def fake_confirm(console=None, *, risky=False):
        captured["risky"] = risky
        return "keep"

    with mock.patch.object(mission_close.review, "confirm_save", fake_confirm):
        assert mission_close.run(ARGS, console=_console()) == 0
    assert captured["risky"] is False


# --- export ------------------------------------------------------------------

def _make_archive(fair_dirs):
    from fair_ros.archive import assembler
    harvest, context = _spool(fair_dirs)
    record = builder.build(harvest, context)
    return assembler.assemble(record, harvest)


def test_export_creates_zip_and_checksum(fair_dirs, tmp_path):
    import zipfile
    crate = _make_archive(fair_dirs)
    out = tmp_path / "share"
    out.mkdir()  # an existing directory is treated as the output folder
    args = SimpleNamespace(mission=str(crate), output=str(out), format="zip",
                           force=False, json=False)
    assert export.run(args, console=_console()) == 0

    bundle = out / f"{crate.name}.zip"
    sidecar = out / f"{crate.name}.zip.sha256"
    assert bundle.is_file() and sidecar.is_file()
    # checksum sidecar is correct and sha256sum-compatible
    digest, name = sidecar.read_text().split()
    assert digest == fsio.sha256_file(bundle) and name == bundle.name
    # bundle has a top-level crate folder
    with zipfile.ZipFile(bundle) as zf:
        names = zf.namelist()
    assert f"{crate.name}/mission_record.json" in names


def test_export_refuses_existing_without_force(fair_dirs, tmp_path):
    crate = _make_archive(fair_dirs)
    dest = tmp_path / "m.zip"
    dest.write_text("old")
    base = dict(mission=str(crate), output=str(dest), format="zip", json=False)
    assert export.run(SimpleNamespace(**base, force=False),
                      console=_console()) == 1
    assert export.run(SimpleNamespace(**base, force=True),
                      console=_console()) == 0
    assert dest.read_bytes()[:2] == b"PK"  # overwritten with a real zip


def test_export_json(fair_dirs, tmp_path, capsys):
    crate = _make_archive(fair_dirs)
    args = SimpleNamespace(mission=str(crate), output=str(tmp_path),
                           format="zip", force=False, json=True)
    assert export.run(args) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["sha256"] == fsio.sha256_file(Path(data["bundle"]))
    assert data["mission_id"] and data["verify_result"] in ("ok", "warn", "fail")


def test_export_unknown_mission(fair_dirs):
    args = SimpleNamespace(mission="does-not-exist", output=None, format="zip",
                           force=False, json=False)
    assert export.run(args, console=_console()) == 1


# --- doctor ------------------------------------------------------------------

def test_doctor_check_clock(monkeypatch):
    monkeypatch.setattr(doctor.clock, "is_synchronized", lambda: False)
    assert doctor._check_clock()["status"] == doctor.FAIL
    monkeypatch.setattr(doctor.clock, "is_synchronized", lambda: True)
    assert doctor._check_clock()["status"] == doctor.OK
    monkeypatch.setattr(doctor.clock, "is_synchronized", lambda: None)
    assert doctor._check_clock()["status"] == doctor.SKIP


def test_doctor_service_harvest_distinguishes_service_context():
    from fair_ros.watchdog import watchdog as wd
    with mock.patch.object(wd, "read_state",
                           return_value={"harvest_status": {"ros_graph": "ok"}}):
        assert doctor._check_service_harvest()["status"] == doctor.OK
    with mock.patch.object(
            wd, "read_state",
            return_value={"harvest_status": {"ros_graph": "failed"}}):
        c = doctor._check_service_harvest()
        assert c["status"] == doctor.FAIL and "ros2 fair setup" in c["hint"]
    with mock.patch.object(wd, "read_state", return_value=None):
        assert doctor._check_service_harvest()["status"] == doctor.SKIP


def test_doctor_check_that_raises_becomes_fail():
    def boom():
        raise RuntimeError("nope")
    with mock.patch.object(doctor, "_CHECKS", (boom,)):
        results = doctor.diagnose()
    assert results[0]["status"] == doctor.FAIL
    assert "nope" in results[0]["detail"]


def test_doctor_run_not_ready_exit_and_json(capsys):
    fake = [{"status": doctor.OK, "title": "a", "detail": "", "hint": ""},
            {"status": doctor.FAIL, "title": "b", "detail": "d", "hint": "h"}]
    with mock.patch.object(doctor, "diagnose", return_value=fake):
        rc = doctor.run(SimpleNamespace(json=True))
    data = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert data["result"] == "fail" and len(data["checks"]) == 2


def test_doctor_run_ready():
    fake = [{"status": doctor.OK, "title": "a", "detail": "", "hint": ""}]
    console = _console()
    with mock.patch.object(doctor, "diagnose", return_value=fake):
        rc = doctor.run(ARGS, console=console)
    assert rc == 0
    assert "READY" in console.file.getvalue()


def test_setup_captures_ros_environment(fair_dirs):
    """The watchdog runs as a service with no sourced ROS env, so setup must
    snapshot the operator's ROS environment into the unit's EnvironmentFile."""
    keep = {"FAIR_ROS_CONFIG_DIR": os.environ["FAIR_ROS_CONFIG_DIR"]}
    env = {**keep,
           "ROS_DISTRO": "jazzy",
           "AMENT_PREFIX_PATH": "/opt/ros/jazzy",
           "RMW_IMPLEMENTATION": "rmw_cyclonedds_cpp",
           "ROS_DOMAIN_ID": "7",
           "PATH": "/opt/ros/jazzy/bin:/usr/bin",
           "HOME": "/root", "EDITOR": "vim"}
    with mock.patch.dict(setup_cmd.os.environ, env, clear=True):
        setup_cmd.write_watchdog_env(_console())
        text = paths.watchdog_env_path().read_text()
    assert "ROS_DISTRO=jazzy" in text
    assert "RMW_IMPLEMENTATION=rmw_cyclonedds_cpp" in text
    assert "ROS_DOMAIN_ID=7" in text
    assert "AMENT_PREFIX_PATH=/opt/ros/jazzy" in text
    assert "PATH=/opt/ros/jazzy/bin:/usr/bin" in text
    # Non-ROS variables are not snapshotted.
    assert "EDITOR" not in text
    assert "HOME=" not in text


def test_setup_warns_when_ros_environment_missing(fair_dirs):
    keep = {"FAIR_ROS_CONFIG_DIR": os.environ["FAIR_ROS_CONFIG_DIR"]}
    console = _console()
    with mock.patch.dict(setup_cmd.os.environ, {**keep, "PATH": "/usr/bin"},
                         clear=True):
        setup_cmd.write_watchdog_env(console)
    assert "ROS_DISTRO is unset" in console.file.getvalue()


def test_setup_written_identity_is_harvestable(fair_dirs):
    config = {
        "robot": {"name": "Heron-02", "platform": "Heron",
                  "serial_number": "H02"},
        "owner": {"organization": "Lab", "contact_email": "a@b.c"},
        "sensors": [{"sensor_id": "gps0", "type": "gps",
                     "make_model": "F9P", "topic": "/fix"}],
    }
    setup_cmd.write_identity(config)
    from fair_ros.harvest import robot_identity
    data = robot_identity.harvest()
    assert data["robot"]["name"] == "Heron-02"
    assert data["sensors"][0]["sensor_id"] == "gps0"
