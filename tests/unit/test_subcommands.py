import io
import json
import os
from types import SimpleNamespace
from unittest import mock

from rich.console import Console

from fair_ros.manifest import builder
from fair_ros.subcommands import (
    list_missions,
    mission_close,
    mission_diff,
    mission_record,
    mission_start,
    mission_status,
)
from fair_ros.subcommands import setup as setup_cmd
from fair_ros.utils import fsio, paths
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
