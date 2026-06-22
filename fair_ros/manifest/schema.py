"""Pydantic models for the MissionRecord, mirroring specs/data_model.md.

The spec file is authoritative; any field change must land there first.
schema_version "1.0".
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from fair_ros import SCHEMA_VERSION


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Identity(_Model):
    mission_id: str
    created_at: datetime
    operator_name: str = Field(min_length=1, max_length=80)
    operator_contact: str | None = None


class Intent(_Model):
    goal: str = Field(min_length=1, max_length=280)
    location_name: str = Field(min_length=1)
    environment: str | None = None
    notes: str | None = None


class Robot(_Model):
    name: str
    platform: str
    serial_number: str
    owner_organization: str
    owner_contact: str


class Sensor(_Model):
    sensor_id: str
    type: str
    make_model: str
    topic: str
    frame_id: str | None = None
    calibration_ref: str | None = None
    detected_at_start: bool = False


class DockerContainer(_Model):
    name: str
    image: str
    digest: str | None = None
    compose_project: str | None = None
    compose_file: str | None = None


class PythonPackage(_Model):
    name: str
    version: str
    installer: str | None = None
    editable: bool = False
    location: str | None = None


class PythonEnv(_Model):
    executable: str
    version: str
    venv_path: str | None = None
    pip_version: str | None = None
    packages: list[PythonPackage] = Field(default_factory=list)
    fair_ros_editable: bool = False
    sys_path: list[str] = Field(default_factory=list)


class HardwareDevice(_Model):
    device_class: str | None = None
    vendor_id: str | None = None
    product_id: str | None = None
    vendor_name: str | None = None
    product_name: str | None = None
    serial_number: str | None = None
    device_path: str | None = None
    bus_path: str | None = None
    driver: str | None = None
    source_command: str
    udev_properties: dict[str, str] | None = None


class Software(_Model):
    ros_distro: str | None = None
    ros_packages: list[str] = Field(default_factory=list)
    apt_ros_versions: dict[str, str] = Field(default_factory=dict)
    docker_containers: list[DockerContainer] = Field(default_factory=list)
    fair_ros_version: str
    python_env: PythonEnv | None = None


class TopicInfo(_Model):
    name: str
    type: str


class RosGraph(_Model):
    captured_at: datetime | None = None
    nodes: list[str] = Field(default_factory=list)
    topics: list[TopicInfo] = Field(default_factory=list)
    parameters: dict[str, dict] = Field(default_factory=dict)
    robot_description: str | None = None
    tf_static: list[dict] | None = None
    complete: bool = False


class Calibration(_Model):
    name: str
    source_path: str
    archived_path: str | None = None
    sha256: str | None = None
    format: str | None = None


class BagTopic(_Model):
    name: str
    type: str
    message_count: int
    avg_frequency_hz: float | None = None


class HealthWarning(_Model):
    topic: str
    sensor_id: str | None = None
    kind: str  # gap | never_published | low_rate
    start_offset_s: float | None = None
    duration_s: float | None = None
    plain_text: str


class Bag(_Model):
    path: str
    storage_format: str
    size_bytes: int
    start_time: datetime
    end_time: datetime
    duration_s: float
    message_count: int
    topics: list[BagTopic] = Field(default_factory=list)
    health_warnings: list[HealthWarning] = Field(default_factory=list)


class Provenance(_Model):
    fair_ros_version: str
    schema_version: str = SCHEMA_VERSION
    harvested_at: datetime | None = None
    assembled_at: datetime | None = None
    hostname: str = ""
    kernel: str = ""
    arch: str = ""
    field_confidence: dict[str, str] = Field(default_factory=dict)
    harvest_status: dict[str, str] = Field(default_factory=dict)


class MissionRecord(_Model):
    schema_version: str = SCHEMA_VERSION
    identity: Identity
    intent: Intent
    robot: Robot | None = None
    sensors: list[Sensor] = Field(default_factory=list)
    software: Software
    ros_graph: RosGraph = Field(default_factory=RosGraph)
    calibrations: list[Calibration] = Field(default_factory=list)
    bags: list[Bag] = Field(default_factory=list)
    hardware_devices: list[HardwareDevice] = Field(default_factory=list)
    provenance: Provenance
