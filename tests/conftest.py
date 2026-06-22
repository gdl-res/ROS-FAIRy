import sqlite3
import textwrap
from pathlib import Path

import pytest


@pytest.fixture
def fair_dirs(tmp_path, monkeypatch):
    """Relocate /var/fair-ros and /etc/fair-ros into tmp_path."""
    var = tmp_path / "var"
    cfg = tmp_path / "etc"
    (var / "spool" / "bags").mkdir(parents=True)
    (var / "archive").mkdir()
    cfg.mkdir()
    monkeypatch.setenv("FAIR_ROS_VAR_DIR", str(var))
    monkeypatch.setenv("FAIR_ROS_CONFIG_DIR", str(cfg))
    return {"var": var, "cfg": cfg}


@pytest.fixture
def identity_yaml(fair_dirs):
    """A valid robot_identity.yaml with two sensors and one calibration."""
    cal_file = fair_dirs["cfg"] / "gps0_cal.yaml"
    cal_file.write_text("offset: 0.5\n")
    path = fair_dirs["cfg"] / "robot_identity.yaml"
    path.write_text(textwrap.dedent(f"""\
        robot:
          name: Heron-02
          platform: Clearpath Heron USV
          serial_number: H02-2031-XK
        owner:
          organization: Example Marine Robotics Lab
          contact_email: fleet@example.org
        sensors:
          - sensor_id: gps0
            type: gps
            make_model: u-blox ZED-F9P
            topic: /fix
            frame_id: gps_link
            calibration: gps0_cal
          - sensor_id: sonar0
            type: sonar
            make_model: BlueRobotics Ping2
            topic: /depth
        calibrations:
          - name: gps0_cal
            source_path: {cal_file}
            format: yaml
        """))
    return path


def make_bag(bag_dir: Path, topics: dict[str, list[float]],
             types: dict[str, str] | None = None,
             storage: str = "sqlite3") -> Path:
    """Create a minimal rosbag2-shaped bag dir with sqlite3 storage.

    topics: topic name -> message timestamps in seconds (epoch).
    """
    bag_dir.mkdir(parents=True, exist_ok=True)
    types = types or {}
    all_ts = [t for stamps in topics.values() for t in stamps]
    start = min(all_ts) if all_ts else 0.0
    end = max(all_ts) if all_ts else 0.0

    db_name = f"{bag_dir.name}_0.db3"
    con = sqlite3.connect(bag_dir / db_name)
    con.execute("CREATE TABLE topics (id INTEGER PRIMARY KEY, name TEXT, "
                "type TEXT, serialization_format TEXT)")
    con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, "
                "topic_id INTEGER, timestamp INTEGER, data BLOB)")
    for tid, (name, stamps) in enumerate(topics.items(), start=1):
        con.execute("INSERT INTO topics VALUES (?, ?, ?, 'cdr')",
                    (tid, name, types.get(name, "std_msgs/msg/Empty")))
        con.executemany(
            "INSERT INTO messages (topic_id, timestamp, data) VALUES (?, ?, x'')",
            [(tid, int(ts * 1e9)) for ts in stamps])
    con.commit()
    con.close()

    _write_metadata(bag_dir, topics, types, storage, db_name, start, end)
    return bag_dir


def make_mcap_bag(bag_dir: Path, topics: dict[str, list[float]],
                  types: dict[str, str] | None = None) -> Path:
    """Create a rosbag2-shaped bag dir backed by a real ``.mcap`` file.

    Requires the optional ``mcap`` package; tests should ``importorskip`` it.
    topics: topic name -> message timestamps in seconds (epoch).
    """
    from mcap.writer import Writer

    bag_dir.mkdir(parents=True, exist_ok=True)
    types = types or {}
    all_ts = [t for stamps in topics.values() for t in stamps]
    start = min(all_ts) if all_ts else 0.0
    end = max(all_ts) if all_ts else 0.0

    mcap_name = f"{bag_dir.name}_0.mcap"
    with open(bag_dir / mcap_name, "wb") as handle:
        writer = Writer(handle)
        writer.start()
        for name, stamps in topics.items():
            schema_id = writer.register_schema(
                name=types.get(name, "std_msgs/msg/Empty"),
                encoding="ros2msg", data=b"")
            channel_id = writer.register_channel(
                topic=name, message_encoding="cdr", schema_id=schema_id)
            for ts in stamps:
                ns = int(ts * 1e9)
                writer.add_message(channel_id=channel_id, log_time=ns,
                                   data=b"", publish_time=ns)
        writer.finish()

    _write_metadata(bag_dir, topics, types, "mcap", mcap_name, start, end)
    return bag_dir


def _write_metadata(bag_dir: Path, topics: dict[str, list[float]],
                    types: dict[str, str], storage: str, rel_file: str,
                    start: float, end: float) -> None:
    all_ts = [t for stamps in topics.values() for t in stamps]
    topic_entries = "\n".join(
        f"    - topic_metadata:\n"
        f"        name: {name}\n"
        f"        type: {types.get(name, 'std_msgs/msg/Empty')}\n"
        f"        serialization_format: cdr\n"
        f"      message_count: {len(stamps)}"
        for name, stamps in topics.items())
    (bag_dir / "metadata.yaml").write_text(textwrap.dedent(f"""\
        rosbag2_bagfile_information:
          version: 5
          storage_identifier: {storage}
          relative_file_paths:
            - {rel_file}
          duration:
            nanoseconds: {int((end - start) * 1e9)}
          starting_time:
            nanoseconds_since_epoch: {int(start * 1e9)}
          message_count: {len(all_ts)}
          topics_with_message_count:
        """) + topic_entries + "\n")
