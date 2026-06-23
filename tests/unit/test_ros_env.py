"""Tests for utils/ros_env — the ROS-environment capture/serialise helpers."""

from fair_ros.utils import ros_env


def test_capture_keeps_only_ros_variables():
    env = {"ROS_DISTRO": "jazzy", "AMENT_PREFIX_PATH": "/opt/ros/jazzy",
           "RMW_IMPLEMENTATION": "rmw_cyclonedds_cpp", "ROS_DOMAIN_ID": "7",
           "PATH": "/opt/ros/jazzy/bin", "LD_LIBRARY_PATH": "/opt/ros/jazzy/lib",
           "EDITOR": "vim", "HOME": "/root"}
    captured = ros_env.capture(env)
    assert captured["ROS_DISTRO"] == "jazzy"
    assert captured["AMENT_PREFIX_PATH"] == "/opt/ros/jazzy"
    assert "PATH" in captured and "LD_LIBRARY_PATH" in captured
    assert "EDITOR" not in captured and "HOME" not in captured


def test_serialize_round_trips_through_parse():
    env = {"ROS_DISTRO": "jazzy", "ROS_DOMAIN_ID": "7",
           "PATH": "/opt/ros/jazzy/bin:/usr/bin"}
    assert ros_env.parse(ros_env.serialize(env)) == env


def test_serialize_is_sorted_and_newline_terminated():
    text = ros_env.serialize({"ROS_DOMAIN_ID": "7", "AMENT_PREFIX_PATH": "/x"})
    assert text == "AMENT_PREFIX_PATH=/x\nROS_DOMAIN_ID=7\n"


def test_serialize_empty_is_empty_string():
    assert ros_env.serialize({}) == ""


def test_parse_ignores_blanks_and_comments():
    env = ros_env.parse("# a comment\n\nROS_DISTRO=jazzy\n  ROS_DOMAIN_ID=3 \n")
    assert env == {"ROS_DISTRO": "jazzy", "ROS_DOMAIN_ID": "3"}


def test_parse_keeps_equals_in_value():
    assert ros_env.parse("CYCLONEDDS_URI=file:///x?a=b")["CYCLONEDDS_URI"] == \
        "file:///x?a=b"


def test_safe_session_env_keeps_only_discovery_keys():
    """The root watchdog must never adopt loader paths from the group-writable
    session.env — that would be a local privilege-escalation vector."""
    env = {"ROS_DOMAIN_ID": "7", "RMW_IMPLEMENTATION": "rmw_x",
           "ROS_LOCALHOST_ONLY": "1",
           "PATH": "/tmp/evil", "LD_LIBRARY_PATH": "/tmp/evil",
           "PYTHONPATH": "/tmp/evil", "AMENT_PREFIX_PATH": "/tmp/evil",
           "ROS_DISTRO": "jazzy"}
    safe = ros_env.safe_session_env(env)
    assert safe == {"ROS_DOMAIN_ID": "7", "RMW_IMPLEMENTATION": "rmw_x",
                    "ROS_LOCALHOST_ONLY": "1"}
    for dangerous in ("PATH", "LD_LIBRARY_PATH", "PYTHONPATH",
                      "AMENT_PREFIX_PATH"):
        assert dangerous not in safe


def test_read_file_missing_returns_empty(tmp_path):
    assert ros_env.read_file(tmp_path / "nope.env") == {}


def test_write_then_read_file(tmp_path):
    path = tmp_path / "sub" / "session.env"
    ros_env.write_file(path, {"ROS_DISTRO": "jazzy"})
    assert path.is_file()
    assert ros_env.read_file(path) == {"ROS_DISTRO": "jazzy"}
