"""Host-level facts: hostname, kernel, arch, ROS distro, installed ROS debs."""

import platform
import socket
import subprocess
from typing import Any

from fair_ros.utils import ros_distro

DPKG_TIMEOUT_S = 10


def _ros_deb_versions() -> dict[str, str]:
    """Installed ros-* Debian packages -> version. Empty dict off Debian."""
    try:
        out = subprocess.run(
            ["dpkg-query", "-W", "-f", "${Package} ${Version}\n", "ros-*"],
            capture_output=True, text=True, timeout=DPKG_TIMEOUT_S,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    if out.returncode != 0:
        return {}
    versions = {}
    for line in out.stdout.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            versions[parts[0]] = parts[1]
    return versions


def harvest() -> dict[str, Any]:
    uname = platform.uname()
    apt_ros_versions = _ros_deb_versions()
    return {
        "hostname": socket.gethostname(),
        "kernel": f"{uname.system} {uname.release}",
        "arch": uname.machine,
        # The watchdog often runs unsourced, so $ROS_DISTRO is empty; fall back
        # to inferring the distro from the installed ros-<distro>-* packages.
        "ros_distro": ros_distro.detect()
        or ros_distro.infer_from_packages(apt_ros_versions),
        "apt_ros_versions": apt_ros_versions,
    }
