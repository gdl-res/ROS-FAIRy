"""Canonical filesystem paths for fair-ros.

Every other module must obtain paths from here. The roots are overridable via
environment variables so tests (and unusual deployments) can relocate them:

- ``FAIR_ROS_VAR_DIR``    -> default ``/var/fair-ros``
- ``FAIR_ROS_CONFIG_DIR`` -> default ``/etc/fair-ros``

Paths are resolved lazily (functions, not module constants) so an env change
made by a test fixture takes effect immediately.
"""

import os
from pathlib import Path

DEFAULT_VAR_DIR = "/var/fair-ros"
DEFAULT_CONFIG_DIR = "/etc/fair-ros"


def var_dir() -> Path:
    return Path(os.environ.get("FAIR_ROS_VAR_DIR", DEFAULT_VAR_DIR))


def config_dir() -> Path:
    return Path(os.environ.get("FAIR_ROS_CONFIG_DIR", DEFAULT_CONFIG_DIR))


def robot_identity_path() -> Path:
    return config_dir() / "robot_identity.yaml"


def watchdog_env_path() -> Path:
    """systemd EnvironmentFile holding the ROS 2 environment captured at setup.

    The watchdog runs as a system service with no login shell, so the ROS
    environment the operator had sourced when running ``setup`` is snapshotted
    here and loaded by the unit (``EnvironmentFile=``). Without it ``ros2`` is
    not on PATH and the graph/description harvest fails. The unit references the
    default ``/etc/fair-ros/watchdog.env``; keep them in sync.
    """
    return config_dir() / "watchdog.env"


def spool_dir() -> Path:
    return var_dir() / "spool"


def bags_dir() -> Path:
    return spool_dir() / "bags"


def harvest_json_path() -> Path:
    return spool_dir() / "harvest.json"


def session_env_path() -> Path:
    """ROS environment of the live recording shell, refreshed at mission time.

    Written by ``mission_start`` / ``mission_record`` (which run as the
    operator); the watchdog adopts its DDS *discovery* keys
    (``ros_env.SESSION_ADOPT_KEYS``) at harvest time so the service lands on the
    same partition as the session actually recording, even if the frozen
    ``watchdog.env`` snapshot has drifted. Lives in the spool because that is
    group-writable; ``watchdog.env`` lives in root-owned ``/etc``. Because it is
    group-writable, the watchdog never adopts loader paths from it (privilege
    escalation); base PATH/overlay come only from the trusted ``watchdog.env``.
    """
    return spool_dir() / "session.env"


def mission_context_path() -> Path:
    return spool_dir() / "mission_context.json"


def archive_dir() -> Path:
    return var_dir() / "archive"


def staging_dir() -> Path:
    return archive_dir() / ".staging"


def index_db_path() -> Path:
    return var_dir() / "index.db"


def watchdog_state_path() -> Path:
    return var_dir() / "watchdog.state"
