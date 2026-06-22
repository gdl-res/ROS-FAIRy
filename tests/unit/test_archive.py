import json

import pytest

from fair_ros.archive import assembler, index
from fair_ros.archive.assembler import AssemblyError
from fair_ros.manifest import builder
from fair_ros.utils import fsio, paths
from tests.conftest import make_bag

T0 = 1_750_000_000.0


def _steady(start, end, hz):
    n = int((end - start) * hz)
    return [start + i / hz for i in range(n + 1)]


def _spool(fair_dirs, n_bags=1, with_cal=True):
    """Populate the spool with real bags + harvest + context documents."""
    cal_file = fair_dirs["cfg"] / "gps0.yaml"
    cal_file.write_text("fx: 1\n")
    identity = {
        "robot": {"name": "Heron-02", "platform": "Clearpath Heron USV",
                  "serial_number": "H02", "owner_organization": "Lab",
                  "owner_contact": "fleet@example.org"},
        "sensors": [{"sensor_id": "gps0", "type": "gps",
                     "make_model": "u-blox ZED-F9P", "topic": "/fix",
                     "frame_id": "gps_link",
                     "calibration_ref": "gps0_cal" if with_cal else None}],
        "calibrations": [{"name": "gps0_cal", "source_path": str(cal_file),
                          "format": "yaml"}] if with_cal else [],
        "default_license": "https://spdx.org/licenses/CC-BY-4.0",
    }
    harvest = builder.compose_harvest(
        identity=identity,
        system={"hostname": "r1", "kernel": "Linux 6.8", "arch": "x86_64",
                "ros_distro": "jazzy", "apt_ros_versions": {}},
        graph={"captured_at": "2026-06-12T14:03:00+00:00",
               "nodes": ["/navsat"],
               "topics": [{"name": "/fix",
                           "type": "sensor_msgs/msg/NavSatFix"}],
               "ros_packages": ["rclpy"], "parameters": {}, "complete": True},
        docker={"docker_containers": [
            {"name": "navstack", "image": "example/navstack:1.4",
             "digest": "example/navstack@sha256:7be1",
             "compose_project": None, "compose_file": None}],
            "raw_inspect": [{"Name": "/navstack"}], "available": True},
        descriptions={"robot_description": "<robot name='heron'/>",
                      "tf_static": [{"parent_frame": "base", "child_frame":
                                     "gps_link"}]},
        harvest_status={"robot_identity": "ok", "system_info": "ok",
                        "python_env": "ok", "hardware_devices": "partial",
                        "ros_graph": "ok", "ros_descriptions": "ok",
                        "docker_info": "ok"},
        python_env={
            "python_env": {
                "executable": "/opt/venv/bin/python3",
                "version": "3.12.3 (main)",
                "venv_path": "/opt/venv",
                "pip_version": "24.0",
                "packages": [{"name": "rclpy", "version": "5.0.0",
                              "installer": "apt", "editable": False,
                              "location": None}],
                "fair_ros_editable": False,
                "sys_path": ["/opt/venv/lib/python3.12/site-packages"]},
            "pip_freeze": "rclpy==5.0.0\n",
            "pip_list_json": None},
        hardware_devices={
            "devices": [{"device_class": "usb", "vendor_id": "1546",
                         "product_id": "01a9", "vendor_name": "u-blox AG",
                         "product_name": "ZED-F9P", "serial_number": None,
                         "device_path": None,
                         "bus_path": "Bus 002 Device 003", "driver": None,
                         "source_command": "lsusb",
                         "udev_properties": None}],
            "lsusb_verbose": "Bus 002 Device 003: ID 1546:01a9\n",
            "dmesg_usb": None})
    for i in range(n_bags):
        bag = make_bag(paths.bags_dir() / f"rosbag2_{i}",
                       {"/fix": _steady(T0 + i * 700, T0 + i * 700 + 600, 10)})
        harvest["bags"].append({
            "path": str(bag), "storage_format": "sqlite3",
            "size_bytes": fsio.dir_size_bytes(bag),
            "start_time": "2026-06-12T14:03:00+00:00",
            "end_time": "2026-06-12T14:13:00+00:00",
            "duration_s": 600.0, "message_count": 6001,
            "topics": [{"name": "/fix", "type": "sensor_msgs/msg/NavSatFix",
                        "message_count": 6001, "avg_frequency_hz": 10.0}],
            "health_warnings": []})
    harvest["provenance"]["harvested_at"] = "2026-06-12T14:14:00+00:00"
    context = builder.new_mission_context(
        operator_name="Jane Doe", goal="Survey eelgrass beds",
        location_name="Marsh Creek, north bank", environment="marine")
    fsio.atomic_write_json(paths.harvest_json_path(), harvest)
    fsio.atomic_write_json(paths.mission_context_path(), context)
    return harvest, context


def test_sanitise():
    assert assembler.sanitise("Marsh Creek, north bank") == \
        "marsh-creek-north-bank"
    assert assembler.sanitise("Jane Doe") == "jane-doe"
    assert assembler.sanitise("Forêt d'Orléans!") == "foret-d-orleans"
    assert assembler.sanitise("///") == "unknown"


def test_assemble_full_crate(fair_dirs):
    harvest, context = _spool(fair_dirs)
    record = builder.build(harvest, context)
    final = assembler.assemble(record, harvest)

    assert final.parent == paths.archive_dir()
    assert final.name.endswith("_marsh-creek-north-bank_jane-doe")
    # bags moved, not copied
    assert not any(paths.bags_dir().iterdir())
    assert (final / "bags" / "rosbag2_0" / "metadata.yaml").is_file()
    # artifacts
    assert (final / "harvest" / "harvest.json").is_file()
    assert (final / "harvest" / "robot_description.urdf").read_text() == \
        "<robot name='heron'/>"
    assert (final / "harvest" / "tf_static.json").is_file()
    assert (final / "calibrations" / "gps0.yaml").is_file()
    assert (final / "docker" / "containers.json").is_file()
    # python env + hardware raw artifacts extracted (dmesg was None → no file)
    assert (final / "harvest" / "pip_freeze.txt").read_text() == \
        "rclpy==5.0.0\n"
    assert (final / "harvest" / "lsusb_verbose.txt").is_file()
    assert not (final / "harvest" / "dmesg_usb.txt").exists()
    readme = (final / "README.md").read_text()
    assert "Survey eelgrass beds" in readme
    assert "## Connected hardware" in readme
    # manifest updated with crate-relative paths and hashes
    saved = json.loads((final / "mission_record.json").read_text())
    assert saved["bags"][0]["path"] == "bags/rosbag2_0"
    assert saved["ros_graph"]["robot_description"] == \
        "harvest/robot_description.urdf"
    assert saved["calibrations"][0]["archived_path"] == \
        "calibrations/gps0.yaml"
    assert len(saved["calibrations"][0]["sha256"]) == 64
    # spool context cleared
    assert not paths.harvest_json_path().exists()
    assert not paths.mission_context_path().exists()
    # indexed
    rows, total = index.query()
    assert total == 1
    assert rows[0]["operator"] == "Jane Doe"
    assert rows[0]["archive_path"] == str(final)
    assert rows[0]["bag_count"] == 1


def test_ro_crate_document(fair_dirs):
    harvest, context = _spool(fair_dirs)
    record = builder.build(harvest, context)
    final = assembler.assemble(record, harvest)
    doc = json.loads((final / "ro-crate-metadata.json").read_text())

    assert doc["@context"][0] == "https://w3id.org/ro/crate/1.1/context"
    assert doc["@context"][1]["sosa"] == "http://www.w3.org/ns/sosa/"
    by_id = {e["@id"]: e for e in doc["@graph"]}

    root = by_id["./"]
    assert root["name"] == "Survey eelgrass beds"
    assert root["license"] == "https://spdx.org/licenses/CC-BY-4.0"
    assert {"@id": "bags/rosbag2_0/"} in root["hasPart"]
    assert {"@id": "mission_record.json"} in root["hasPart"]
    assert "marine" in root["keywords"]

    assert by_id["#operator"]["name"] == "Jane Doe"
    assert by_id["#operator"]["email"] == "fleet@example.org"
    assert by_id["#place"]["name"] == "Marsh Creek, north bank"
    assert by_id["#robot"]["@type"] == ["Thing", "sosa:Platform"]
    assert by_id["#robot"]["sosa:hosts"] == [{"@id": "#sensor-gps0"}]

    sensor = by_id["#sensor-gps0"]
    assert sensor["sosa:isHostedBy"] == {"@id": "#robot"}
    assert sensor["subjectOf"] == {"@id": "calibrations/gps0.yaml"}
    # PropertyValues are hoisted into @id'd entities and referenced
    assert {"@id": "#sensor-gps0-topic"} in sensor["additionalProperty"]
    assert by_id["#sensor-gps0-topic"]["value"] == "/fix"
    assert by_id["#sensor-gps0-topic"]["@type"] == "PropertyValue"

    mission = by_id["#mission"]
    assert mission["agent"] == {"@id": "#operator"}
    assert {"@id": "#robot"} in mission["instrument"]
    assert {"@id": "#ros2"} in mission["instrument"]
    assert {"@id": "#python-runtime"} in mission["instrument"]
    assert {"@id": "#container-navstack"} in mission["instrument"]
    assert mission["startTime"] == "2026-06-12T14:03:00+00:00"

    py = by_id["#python-runtime"]
    assert py["@type"] == "SoftwareApplication"
    assert py["version"] == "3.12.3 (main)"
    py_props = {by_id[ref["@id"]]["name"]: by_id[ref["@id"]]["value"]
                for ref in py["additionalProperty"]}
    assert py_props["executable"] == "/opt/venv/bin/python3"
    assert py_props["venv_path"] == "/opt/venv"

    bag = by_id["bags/rosbag2_0/"]
    assert bag["encodingFormat"] == "application/x-sqlite3"
    first_var = by_id[bag["variableMeasured"][0]["@id"]]
    assert first_var["name"] == "/fix"
    assert first_var["value"] == 6001

    # bag files are File entities with sha256, referenced by the bag's hasPart,
    # and the digests match mission_record.json's bags[].file_sha256
    recorded = record.bags[0].file_sha256
    assert recorded, "expected per-file bag checksums"
    parts = {p["@id"] for p in bag["hasPart"]}
    for rel, digest in recorded.items():
        file_id = f"bags/rosbag2_0/{rel}"
        assert file_id in parts
        assert by_id[file_id]["@type"] == "File"
        assert by_id[file_id]["sha256"] == digest
    assert by_id["bags/rosbag2_0/rosbag2_0_0.db3"]["encodingFormat"] == \
        "application/x-sqlite3"

    # confidence markers share one hoisted entity, referenced by user fields
    assert by_id["#operator"]["additionalProperty"] == {"@id": "#confidence-user"}
    assert by_id["#confidence-user"]["value"] == "user"
    assert by_id["#ros2"]["version"] == "jazzy"
    assert by_id["#container-navstack"]["identifier"] == \
        "example/navstack@sha256:7be1"
    assert by_id["calibrations/gps0.yaml"]["sha256"]
    assert by_id["ro-crate-metadata.json"]["about"] == {"@id": "./"}


def test_readme_flags_identifying_serials():
    from datetime import datetime, timezone

    from fair_ros.manifest.schema import (
        HardwareDevice,
        Identity,
        Intent,
        MissionRecord,
        Provenance,
        Software,
    )
    rec = MissionRecord(
        identity=Identity(mission_id="m-x",
                          created_at=datetime(2026, 6, 12, tzinfo=timezone.utc),
                          operator_name="Jane"),
        intent=Intent(goal="Survey", location_name="lab"),
        software=Software(fair_ros_version="0.1.0"),
        hardware_devices=[
            HardwareDevice(source_command="lsusb", device_class="usb",
                           product_name="u-blox ZED-F9P",
                           serial_number="3C123"),
            HardwareDevice(source_command="glob:/dev/video*",
                           device_class="video", device_path="/dev/video0")],
        provenance=Provenance(fair_ros_version="0.1.0", schema_version="1.0",
                              hostname="h", kernel="Linux", arch="x86_64"))
    md = assembler._render_readme(rec, [])
    assert "## Connected hardware" in md
    assert "u-blox ZED-F9P" in md
    assert "1 of these record a serial number" in md
    assert "Serial numbers can identify" in md

    # no hardware → no section at all
    rec.hardware_devices = []
    assert "Connected hardware" not in assembler._render_readme(rec, [])


def test_archive_name_collision(fair_dirs):
    harvest, context = _spool(fair_dirs, n_bags=2)
    record = builder.build(harvest, context)
    first = assembler.assemble(record, harvest)

    harvest2, context2 = _spool(fair_dirs)
    context2["identity"]["created_at"] = context["identity"]["created_at"]
    record2 = builder.build(harvest2, context2)
    record2.identity.created_at = record.identity.created_at
    second = assembler.assemble(record2, harvest2)
    assert second.name == first.name + "_2"


def test_failure_before_bag_move_leaves_spool_intact(fair_dirs, monkeypatch):
    harvest, context = _spool(fair_dirs)
    record = builder.build(harvest, context)

    def boom(*a, **kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(assembler.ro_crate, "write", boom)
    with pytest.raises(AssemblyError, match="disk space"):
        assembler.assemble(record, harvest)
    assert (paths.bags_dir() / "rosbag2_0" / "metadata.yaml").is_file()
    assert paths.harvest_json_path().exists()
    assert not any(paths.archive_dir().glob("2026*"))
    assert not list(paths.staging_dir().glob("*"))


def test_bag_move_failure_rolls_back(fair_dirs, monkeypatch):
    harvest, context = _spool(fair_dirs, n_bags=2)
    record = builder.build(harvest, context)

    real_move = assembler._move_bag
    calls = {"n": 0}

    def flaky_move(src, dest, progress):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(5, "Input/output error")
        real_move(src, dest, progress)

    monkeypatch.setattr(assembler, "_move_bag", flaky_move)
    with pytest.raises(AssemblyError, match="back in the spool"):
        assembler.assemble(record, harvest)
    assert (paths.bags_dir() / "rosbag2_0").is_dir()
    assert (paths.bags_dir() / "rosbag2_1").is_dir()
    assert not list(paths.staging_dir().glob("*"))


def test_resume_interrupted_staging(fair_dirs):
    harvest, context = _spool(fair_dirs)
    record = builder.build(harvest, context)
    name = assembler.archive_name(record)
    staging = paths.staging_dir() / name
    staging.mkdir(parents=True)
    record.provenance.assembled_at = record.identity.created_at
    fsio.atomic_write_json(staging / "mission_record.json",
                           record.model_dump(mode="json"))

    found = assembler.find_interrupted_staging()
    assert found == staging
    final = assembler.resume_commit(found)
    assert final.name == name
    rows, total = index.query()
    assert total == 1


def test_index_filters(fair_dirs):
    harvest, context = _spool(fair_dirs)
    record = builder.build(harvest, context)
    assembler.assemble(record, harvest)

    rows, total = index.query(operator="jane")
    assert total == 1
    rows, total = index.query(operator="nobody")
    assert total == 0
    rows, total = index.query(location="marsh")
    assert total == 1
    rows, total = index.query(since="2099-01-01")
    assert total == 0
    rows, total = index.query(limit=0)
    assert rows == [] and total == 1


def test_reindex_rebuilds(fair_dirs):
    harvest, context = _spool(fair_dirs)
    record = builder.build(harvest, context)
    final = assembler.assemble(record, harvest)
    paths.index_db_path().unlink()
    assert index.reindex() == 1
    rows, total = index.query()
    assert rows[0]["archive_path"] == str(final)
