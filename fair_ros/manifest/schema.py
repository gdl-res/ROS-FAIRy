"""Pydantic models for the MissionRecord, mirroring specs/data_model.md.

The spec file is authoritative; any field change must land there first.
schema_version "1.0".
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from fair_ros import SCHEMA_VERSION


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Identity(_Model):
    mission_id: str
    created_at: datetime
    operator_name: str = Field(min_length=1, max_length=80)
    operator_contact: Optional[str] = None


class Intent(_Model):
    goal: str = Field(min_length=1, max_length=280)
    location_name: str = Field(min_length=1)
    environment: Optional[str] = None
    notes: Optional[str] = None


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
    frame_id: Optional[str] = None
    calibration_ref: Optional[str] = None
    detected_at_start: bool = False


class DockerContainer(_Model):
    name: str
    image: str
    digest: Optional[str] = None
    compose_project: Optional[str] = None
    compose_file: Optional[str] = None


class PythonPackage(_Model):
    name: str
    version: str
    installer: Optional[str] = None
    editable: bool = False
    location: Optional[str] = None


class PythonEnv(_Model):
    executable: str
    version: str
    venv_path: Optional[str] = None
    pip_version: Optional[str] = None
    packages: list[PythonPackage] = Field(default_factory=list)
    fair_ros_editable: bool = False
    sys_path: list[str] = Field(default_factory=list)


class HardwareDevice(_Model):
    device_class: Optional[str] = None
    vendor_id: Optional[str] = None
    product_id: Optional[str] = None
    vendor_name: Optional[str] = None
    product_name: Optional[str] = None
    serial_number: Optional[str] = None
    device_path: Optional[str] = None
    bus_path: Optional[str] = None
    driver: Optional[str] = None
    source_command: str
    udev_properties: Optional[dict[str, str]] = None


class Software(_Model):
    ros_distro: Optional[str] = None
    ros_packages: list[str] = Field(default_factory=list)
    apt_ros_versions: dict[str, str] = Field(default_factory=dict)
    docker_containers: list[DockerContainer] = Field(default_factory=list)
    fair_ros_version: str
    python_env: Optional[PythonEnv] = None


class TopicInfo(_Model):
    name: str
    type: str


class RosGraph(_Model):
    captured_at: Optional[datetime] = None
    nodes: list[str] = Field(default_factory=list)
    topics: list[TopicInfo] = Field(default_factory=list)
    parameters: dict[str, dict] = Field(default_factory=dict)
    robot_description: Optional[str] = None
    tf_static: Optional[list[dict]] = None
    complete: bool = False


class Calibration(_Model):
    name: str
    source_path: str
    archived_path: Optional[str] = None
    sha256: Optional[str] = None
    format: Optional[str] = None


class BagTopic(_Model):
    name: str
    type: str
    message_count: int
    avg_frequency_hz: Optional[float] = None


class HealthWarning(_Model):
    topic: str
    sensor_id: Optional[str] = None
    kind: str  # gap | never_published | low_rate
    start_offset_s: Optional[float] = None
    duration_s: Optional[float] = None
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
    harvested_at: Optional[datetime] = None
    assembled_at: Optional[datetime] = None
    hostname: str = ""
    kernel: str = ""
    arch: str = ""
    field_confidence: dict[str, str] = Field(default_factory=dict)
    harvest_status: dict[str, str] = Field(default_factory=dict)


class MissionRecord(_Model):
    schema_version: str = SCHEMA_VERSION
    identity: Identity
    intent: Intent
    robot: Optional[Robot] = None
    sensors: list[Sensor] = Field(default_factory=list)
    software: Software
    ros_graph: RosGraph = Field(default_factory=RosGraph)
    calibrations: list[Calibration] = Field(default_factory=list)
    bags: list[Bag] = Field(default_factory=list)
    hardware_devices: list[HardwareDevice] = Field(default_factory=list)
    provenance: Provenance
