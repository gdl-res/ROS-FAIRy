from setuptools import find_packages, setup

package_name = "fair_ros"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["tests", "tests.*"]),
    package_data={
        "fair_ros.watchdog": ["fair-ros-watchdog.service"],
    },
    data_files=[
        ("share/ament_index/resource_index/packages",
         ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        ("share/" + package_name + "/systemd",
         ["systemd/fair-ros-watchdog.service"]),
    ],
    install_requires=[
        "setuptools",
        "pydantic>=2.5",
        "rich>=13",
        "PyYAML>=6",
        "inotify_simple>=1.3",
        # MCAP is rosbag2's default storage from Jazzy on; the watchdog needs it
        # to read per-message timestamps for bag duration and topic health.
        # The code still degrades gracefully if it is somehow absent.
        "mcap",
    ],
    extras_require={
        "test": ["pytest", "rocrate"],
        "dev": ["pytest", "rocrate", "ruff", "mypy"],
    },
    zip_safe=False,
    author="fair-ros contributors",
    maintainer="fair-ros contributors",
    maintainer_email="fleet@example.org",
    description="Make ROS 2 field mission data FAIR-compliant with zero "
                "friction: automatic context capture, plain-language "
                "briefings, RO-Crate archives.",
    license="Apache-2.0",
    entry_points={
        "ros2cli.command": [
            "fair = fair_ros.command.fair:FairCommand",
        ],
        "ros2cli.extension_point": [
            "fair.verb = fair_ros.subcommands:VerbExtension",
        ],
        "fair.verb": [
            "setup = fair_ros.subcommands.setup:SetupVerb",
            "mission_start = fair_ros.subcommands.mission_start:"
            "MissionStartVerb",
            "mission_record = fair_ros.subcommands.mission_record:"
            "MissionRecordVerb",
            "mission_close = fair_ros.subcommands.mission_close:"
            "MissionCloseVerb",
            "mission_status = fair_ros.subcommands.mission_status:"
            "MissionStatusVerb",
            "list = fair_ros.subcommands.list_missions:ListVerb",
            "diff = fair_ros.subcommands.mission_diff:DiffVerb",
            "verify = fair_ros.subcommands.verify:VerifyVerb",
            "doctor = fair_ros.subcommands.doctor:DoctorVerb",
            "export = fair_ros.subcommands.export:ExportVerb",
            "repair = fair_ros.subcommands.repair:RepairVerb",
        ],
        "console_scripts": [
            "fair-ros-watchdog = fair_ros.watchdog.watchdog:main",
        ],
    },
)
